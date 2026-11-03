"""In-memory data store for the mock backend.

Loads the seven POC Excel files once and answers the queries behind the
endpoints in docs/API_CONTRACT.md. Everything the voice agent is allowed to
see goes through here, so the prisoner-visibility rules live in one place:

- complaint actions with visible_to_prisoner = "لا" are never returned
- procedures with disclosable_to_prisoner = "لا" are never returned
- internal-only columns (action_description_internal, internal_next_step,
  ai_confidence, classification_source) are never returned

No FastAPI imports here — app.py is the HTTP layer, this is plain Python so
the tests can exercise it directly.
"""

from __future__ import annotations

import json
import os
import re
import threading
from datetime import date, datetime
from pathlib import Path
from typing import Any

import openpyxl

_ROOT = Path(__file__).resolve().parent
_DEFAULT_DATA_DIR = _ROOT.parent.parent / "PP POC Data"
_RIGHTS_PATH = _ROOT / "rights_guide.json"
_RUNTIME_DIR = _ROOT / "data"

YES = "نعم"
NO = "لا"

# Placeholders in 06_complaint_routing.handling_authority that have to be
# resolved per inmate when a complaint is routed.
_AUTH_CASE = "الجهة المختصة بالقضية"
_AUTH_FACILITY = "إدارة مرفق التوقيف الذي يقيم فيه النزيل"
_AUTH_SECURITY = "إدارة الأمن والسلامة بمرفق التوقيف"

_ARABIC_DIACRITICS = re.compile(r"[ً-ْـ]")


class NotFoundError(Exception):
    """The record does not exist, or does not belong to this prisoner."""


class ValidationError(Exception):
    """The submitted payload is not acceptable."""


def _iso(v: Any) -> Any:
    if isinstance(v, datetime):
        if v.hour == 0 and v.minute == 0 and v.second == 0:
            return v.date().isoformat()
        return v.isoformat(timespec="minutes")
    if isinstance(v, date):
        return v.isoformat()
    return v


def _load_sheet(path: Path) -> list[dict[str, Any]]:
    wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        rows = list(wb.worksheets[0].iter_rows(values_only=True))
    finally:
        wb.close()
    header = rows[0]
    return [
        {k: _iso(v) for k, v in zip(header, r, strict=False)}
        for r in rows[1:]
        if any(v is not None for v in r)
    ]


def normalize_ar(text: str) -> str:
    """Light Arabic normalisation for keyword search (not for display)."""
    text = _ARABIC_DIACRITICS.sub("", text or "")
    text = re.sub("[إأآا]", "ا", text)
    text = text.replace("ى", "ي").replace("ة", "ه")
    return text.lower()


def _pick(row: dict[str, Any], keys: tuple[str, ...]) -> dict[str, Any]:
    return {k: row.get(k) for k in keys}


_PROFILE_FIELDS = (
    "prisoner_id", "full_name_ar", "gender", "nationality", "facility_name",
    "cell_block", "detention_type", "currently_has_lawyer", "weekly_visit_allowance",
    "preferred_language", "current_incarceration_status",
)
_COMPLAINT_LIST_FIELDS = (
    "complaint_id", "complaint_category", "complaint_subcategory", "complaint_title", "complaint_summary",
    "complaint_status", "submission_date", "last_update_date", "handling_authority",
    "is_urgent", "is_confidential", "related_tracking_id",
)
_COMPLAINT_DETAIL_FIELDS = (
    *_COMPLAINT_LIST_FIELDS,
    "original_statement", "input_channel", "incident_date", "incident_date_text", "incident_location",
    "incident_details", "related_type", "related_name", "expected_action",
    "response_to_prisoner", "response_date", "closed_date", "closure_reason",
    "current_action", "next_action", "next_action_due_date",
)
_REQUEST_FIELDS = (
    "tracking_id", "request_id", "request_type", "request_summary", "submission_date",
    "current_status", "decision_date", "decision_reason",
)
_CASE_FIELDS = (
    "case_id", "case_number", "charge_description", "case_status", "current_stage",
    "current_authority", "filing_date", "last_update_date", "next_session_date",
    "judgment_date", "objection_deadline", "release_order_date", "release_order_status",
)
_PROCEDURE_FIELDS = (
    "current_procedure", "last_registered_action", "prisoner_facing_next_step",
    "last_update_date", "expected_completion_date",
)


class Store:
    def __init__(self, data_dir: Path | None = None, runtime_dir: Path | None = None):
        data_dir = Path(data_dir or os.environ.get("POC_DATA_DIR") or _DEFAULT_DATA_DIR)
        if not data_dir.is_dir():
            raise FileNotFoundError(f"POC data folder not found: {data_dir}")
        self._runtime_dir = Path(runtime_dir or _RUNTIME_DIR)
        self._lock = threading.Lock()

        self.prisoners = {r["prisoner_id"]: r for r in _load_sheet(data_dir / "01_prisoners.xlsx")}
        self.cases = _load_sheet(data_dir / "02_case_status.xlsx")
        self.procedures = _load_sheet(data_dir / "03_current_procedures.xlsx")
        self.complaints = _load_sheet(data_dir / "04_complaints.xlsx")
        self.actions = _load_sheet(data_dir / "05_complaint_actions.xlsx")
        self.routing = {r["subcategory_code"]: r for r in _load_sheet(data_dir / "06_complaint_routing.xlsx")}
        self.requests = _load_sheet(data_dir / "07_all_requests_tracking.xlsx")
        self.rights = json.loads(_RIGHTS_PATH.read_text(encoding="utf-8"))
        self.staff_requests: list[dict[str, Any]] = []

        self._load_runtime()

    # ---------------- runtime persistence (complaints filed via the agent) ----

    @property
    def _runtime_file(self) -> Path:
        return self._runtime_dir / "new_complaints.json"

    def _load_runtime(self) -> None:
        if not self._runtime_file.exists():
            return
        saved = json.loads(self._runtime_file.read_text(encoding="utf-8"))
        self.complaints.extend(saved.get("complaints", []))
        self.actions.extend(saved.get("actions", []))

    def _save_runtime(self, complaint: dict[str, Any], actions: list[dict[str, Any]]) -> None:
        self._runtime_dir.mkdir(parents=True, exist_ok=True)
        saved = {"complaints": [], "actions": []}
        if self._runtime_file.exists():
            saved = json.loads(self._runtime_file.read_text(encoding="utf-8"))
        saved["complaints"].append(complaint)
        saved["actions"].extend(actions)
        self._runtime_file.write_text(
            json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8"
        )

    # ---------------- prisoners ------------------------------------------------

    def _prisoner(self, prisoner_id: str) -> dict[str, Any]:
        p = self.prisoners.get(prisoner_id)
        if p is None:
            raise NotFoundError(f"prisoner {prisoner_id} not found")
        return p

    def list_prisoners(self) -> list[dict[str, Any]]:
        """Mock-only helper for the test page's inmate picker."""
        return [
            _pick(p, ("prisoner_id", "full_name_ar", "facility_name", "preferred_language"))
            for p in self.prisoners.values()
        ]

    def profile(self, prisoner_id: str) -> dict[str, Any]:
        p = self._prisoner(prisoner_id)
        out = _pick(p, _PROFILE_FIELDS)
        out["first_name"] = (p.get("full_name_ar") or "").split(" ")[0]
        return out

    # ---------------- taxonomy -------------------------------------------------

    def taxonomy(self) -> list[dict[str, Any]]:
        return [
            {
                "subcategory_code": r["subcategory_code"],
                "category_code": r["category_code"],
                "category_name_ar": r["category_name_ar"],
                "subcategory_name_ar": r["subcategory_name_ar"],
                "default_urgent": r["default_urgent"] == YES,
                "is_confidential_default": r["is_confidential_default"] == YES,
                "sla_days": r["sla_days"],
            }
            for r in self.routing.values()
        ]

    def _names(self, sub_code: str | None) -> dict[str, Any]:
        r = self.routing.get(sub_code or "")
        return {
            "category_name_ar": r["category_name_ar"] if r else None,
            "subcategory_name_ar": r["subcategory_name_ar"] if r else None,
        }

    # ---------------- complaints -----------------------------------------------

    def list_complaints(self, prisoner_id: str) -> list[dict[str, Any]]:
        self._prisoner(prisoner_id)
        rows = [c for c in self.complaints if c["prisoner_id"] == prisoner_id]
        rows.sort(key=lambda c: str(c.get("submission_date") or ""), reverse=True)
        return [{**_pick(c, _COMPLAINT_LIST_FIELDS), **self._names(c["complaint_subcategory"])} for c in rows]

    def get_complaint(self, prisoner_id: str, complaint_id: str) -> dict[str, Any]:
        self._prisoner(prisoner_id)
        c = next(
            (
                c for c in self.complaints
                if c["complaint_id"] == complaint_id and c["prisoner_id"] == prisoner_id
            ),
            None,
        )
        if c is None:
            raise NotFoundError(f"complaint {complaint_id} not found for {prisoner_id}")
        timeline = [
            {
                "action_date": a["action_date"],
                "action_type": a["action_type"],
                "description": a["action_description_prisoner"],
                "next_action": a["next_action"],
            }
            for a in self.actions
            if a["complaint_id"] == complaint_id and a["visible_to_prisoner"] == YES
        ]
        timeline.sort(key=lambda a: str(a["action_date"]))
        return {
            **_pick(c, _COMPLAINT_DETAIL_FIELDS),
            **self._names(c["complaint_subcategory"]),
            "timeline": timeline,
        }

    def _resolve_authority(self, route: dict[str, Any], prisoner: dict[str, Any], case_id: str | None) -> str:
        raw = route["handling_authority"] or ""
        facility = prisoner.get("facility_name") or ""
        if raw.startswith(_AUTH_CASE):
            mine = [c for c in self.cases if c["prisoner_id"] == prisoner["prisoner_id"]]
            case = next((c for c in mine if c["case_id"] == case_id), None)
            if case is None:
                open_cases = [c for c in mine if c["case_status"] == "مفتوحة"]
                open_cases.sort(key=lambda c: str(c.get("last_update_date") or ""), reverse=True)
                case = open_cases[0] if open_cases else None
            return case["current_authority"] if case else "النيابة العامة"
        if raw == _AUTH_FACILITY:
            return f"إدارة {facility}"
        if raw == _AUTH_SECURITY:
            return f"إدارة الأمن والسلامة - {facility}"
        return raw

    def create_complaint(self, payload: dict[str, Any]) -> dict[str, Any]:
        prisoner = self._prisoner(payload.get("prisoner_id", ""))
        sub = payload.get("complaint_subcategory")
        route = self.routing.get(sub or "")
        if route is None:
            raise ValidationError(f"unknown complaint_subcategory: {sub!r}")
        summary = (payload.get("complaint_summary") or "").strip()
        if not summary:
            raise ValidationError("complaint_summary is required")
        if payload.get("confirmed_by_prisoner") is not True:
            raise ValidationError("confirmed_by_prisoner must be true")

        incident_date = payload.get("incident_date")
        if incident_date not in (None, "غير معروفة"):
            try:
                if date.fromisoformat(incident_date) > date.today():
                    raise ValidationError("incident_date cannot be in the future")
            except ValueError:
                raise ValidationError(
                    'incident_date must be YYYY-MM-DD or "غير معروفة"'
                ) from None

        related_tracking_id = payload.get("related_tracking_id")
        if related_tracking_id and not any(
            r["tracking_id"] == related_tracking_id and r["prisoner_id"] == prisoner["prisoner_id"]
            for r in self.requests
        ):
            raise ValidationError(f"related_tracking_id {related_tracking_id!r} not found for this prisoner")

        now = datetime.now()
        today = now.date().isoformat()
        stamp = now.isoformat(timespec="minutes")
        is_urgent = bool(payload.get("is_urgent")) or route["default_urgent"] == YES
        is_confidential = route["is_confidential_default"] == YES
        authority = self._resolve_authority(route, prisoner, payload.get("related_case_id"))

        with self._lock:
            year = now.strftime("%y")
            seq = 1 + max(
                (int(c["complaint_id"].split("-")[1]) for c in self.complaints),
                default=0,
            )
            complaint_id = f"C{year}-{seq:05d}"
            complaint = {
                "complaint_id": complaint_id,
                "prisoner_id": prisoner["prisoner_id"],
                "complaint_category": route["category_code"],
                "complaint_subcategory": sub,
                "complaint_title": payload.get("complaint_title"),
                "complaint_summary": summary,
                "original_statement": payload.get("original_statement"),
                "input_channel": payload.get("input_channel") or "صوت",
                "related_to_specific_person_or_entity": YES if payload.get("related_name") else NO,
                "related_type": payload.get("related_type"),
                "related_name": payload.get("related_name"),
                "incident_date": incident_date,
                "incident_date_text": payload.get("incident_date_text"),
                "incident_location": payload.get("incident_location"),
                "incident_details": payload.get("incident_details") or summary,
                "has_witnesses": payload.get("has_witnesses"),
                "witnesses_info": payload.get("witnesses_info"),
                "has_supporting_documents": payload.get("has_supporting_documents"),
                "expected_action": payload.get("expected_action"),
                "additional_notes": payload.get("additional_notes"),
                "confirmation_status": "مؤكدة",
                "confirmed_at": stamp,
                "submission_date": today,
                "complaint_status": "محالة",
                "handling_authority": authority,
                "last_update_date": today,
                "related_tracking_id": related_tracking_id,
                "related_case_id": payload.get("related_case_id"),
                "classification_source": payload.get("classification_source") or "المساعد الذكي",
                "ai_confidence": payload.get("ai_confidence"),
                "is_urgent": YES if is_urgent else NO,
                "is_confidential": YES if is_confidential else NO,
                "response_to_prisoner": None,
                "current_action": "إحالة",
                "next_action": "إسناد الشكوى لموظف مختص",
                "next_action_due_date": None,
            }
            base = len(self.actions)
            actions = [
                {
                    "action_id": f"ACT-{base + 1:06d}",
                    "complaint_id": complaint_id,
                    "action_date": stamp,
                    "action_type": "استلام",
                    "performed_by_unit": "المساعد الذكي",
                    "action_description_internal": "استلام آلي عبر المساعد الذكي بعد تأكيد النزيل",
                    "action_description_prisoner": "تم استلام شكواك وتسجيلها.",
                    "visible_to_prisoner": YES,
                    "next_action": "إحالة الشكوى للجهة المختصة",
                },
                {
                    "action_id": f"ACT-{base + 2:06d}",
                    "complaint_id": complaint_id,
                    "action_date": stamp,
                    "action_type": "إحالة",
                    "performed_by_unit": "نظام التوجيه الآلي",
                    "action_description_internal": f"إحالة آلية حسب جدول التوجيه ({sub})",
                    "action_description_prisoner": (
                        "تمت إحالة شكواك إلى الجهة المختصة."
                        if is_confidential
                        else f"تمت إحالة شكواك إلى الجهة المختصة: {authority}."
                    ),
                    "visible_to_prisoner": YES,
                    "next_action": "إسناد الشكوى لموظف مختص",
                },
            ]
            self.complaints.append(complaint)
            self.actions.extend(actions)
            self._save_runtime(complaint, actions)

        return {
            "complaint_id": complaint_id,
            "complaint_status": complaint["complaint_status"],
            "submission_date": today,
            "handling_authority": authority,
            "sla_days": route["sla_days"],
            "is_urgent": is_urgent,
            "is_confidential": is_confidential,
            **self._names(sub),
        }

    # ---------------- requests (visit / transfer / lawyer / release / clemency)

    def list_requests(self, prisoner_id: str) -> list[dict[str, Any]]:
        self._prisoner(prisoner_id)
        rows = [r for r in self.requests if r["prisoner_id"] == prisoner_id]
        rows.sort(key=lambda r: str(r.get("submission_date") or ""), reverse=True)
        return [_pick(r, _REQUEST_FIELDS) for r in rows]

    def get_request(self, prisoner_id: str, request_id: str) -> dict[str, Any]:
        self._prisoner(prisoner_id)
        r = next(
            (
                r for r in self.requests
                if r["prisoner_id"] == prisoner_id and request_id in (r["request_id"], r["tracking_id"])
            ),
            None,
        )
        if r is None:
            raise NotFoundError(f"request {request_id} not found for {prisoner_id}")
        return _pick(r, _REQUEST_FIELDS)

    # ---------------- cases ----------------------------------------------------

    def list_cases(self, prisoner_id: str) -> list[dict[str, Any]]:
        self._prisoner(prisoner_id)
        out = []
        for c in self.cases:
            if c["prisoner_id"] != prisoner_id:
                continue
            procs = [
                p for p in self.procedures
                if p["case_id"] == c["case_id"] and p["disclosable_to_prisoner"] == YES
            ]
            procs.sort(key=lambda p: (str(p.get("procedure_start_date") or ""), p["procedure_id"]))
            out.append({
                **_pick(c, _CASE_FIELDS),
                "current_procedure": _pick(procs[-1], _PROCEDURE_FIELDS) if procs else None,
            })
        # open cases first, most recently updated first
        out.sort(key=lambda c: (c["case_status"] != "مفتوحة", -_date_key(c["last_update_date"])))
        return out

    # ---------------- rights guide ---------------------------------------------

    def rights_topics(self) -> list[dict[str, Any]]:
        return [
            {"topic_id": t["topic_id"], "title": t["title"], "summary": t["summary"], "has_article": bool(t.get("article"))}
            for t in self.rights["topics"]
        ]

    def rights_topic(self, topic_id: str) -> dict[str, Any]:
        t = next((t for t in self.rights["topics"] if t["topic_id"] == topic_id), None)
        if t is None:
            raise NotFoundError(f"rights topic {topic_id} not found")
        return {**t, "source": self.rights["source"]}

    def rights_search(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        words = [
            w for w in normalize_ar(query).split()
            if len(_stem(w)) >= 3 and _stem(w) not in _SEARCH_STOPWORDS
        ]
        scored = []
        for t in self.rights["topics"]:
            art = t.get("article") or {}
            hay_title = normalize_ar(t["title"] + " " + " ".join(t.get("keywords", [])))
            hay_body = normalize_ar(t["summary"] + " " + art.get("lead", "") + " " + " ".join(art.get("bullets", [])))
            score = sum(3 for w in words if _stem(w) in hay_title) + sum(1 for w in words if _stem(w) in hay_body)
            if score:
                scored.append((score, t))
        scored.sort(key=lambda s: -s[0])
        return [{**t, "source": self.rights["source"]} for _, t in scored[:limit]]

    # ---------------- staff assistance -----------------------------------------

    def create_staff_request(self, payload: dict[str, Any]) -> dict[str, Any]:
        prisoner = self._prisoner(payload.get("prisoner_id", ""))
        urgent = bool(payload.get("urgent"))
        with self._lock:
            rec = {
                "assistance_id": f"AST-{len(self.staff_requests) + 1:05d}",
                "prisoner_id": prisoner["prisoner_id"],
                "facility_name": prisoner.get("facility_name"),
                "cell_block": prisoner.get("cell_block"),
                "reason": payload.get("reason"),
                "urgent": urgent,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "status": "بانتظار الموظف",
                "notes": [],
            }
            self.staff_requests.append(rec)
        return {
            "assistance_id": rec["assistance_id"],
            "status": rec["status"],
            "urgent": urgent,
            "estimated_wait_minutes": 1 if urgent else 5,
        }

    def add_staff_note(self, prisoner_id: str, assistance_id: str, note: str) -> dict[str, Any]:
        rec = next(
            (
                r for r in self.staff_requests
                if r["assistance_id"] == assistance_id and r["prisoner_id"] == prisoner_id
            ),
            None,
        )
        if rec is None:
            raise NotFoundError(f"staff request {assistance_id} not found for {prisoner_id}")
        if not (note or "").strip():
            raise ValidationError("note is required")
        rec["notes"].append(note.strip())
        return {"assistance_id": assistance_id, "status": rec["status"], "notes_count": len(rec["notes"])}


# Normalised (see normalize_ar) filler words that carry no topic meaning
_SEARCH_STOPWORDS = {
    "ابي", "ابغي", "ابغا", "اريد", "اعرف", "اسال", "حقي", "حقوقي", "حقوق", "حق",
    "عندي", "داخل", "السجن", "سجن", "التوقيف", "هذا", "هذي", "ممكن", "كيف", "ايش",
    "عني", "عنها", "لها",
}


def _stem(word: str) -> str:
    """Strips the Arabic definite article and common one-letter prefixes so
    'العلاج' and 'للعلاج' and 'علاج' all match the same text."""
    for prefix in ("وال", "بال", "لل", "ال", "و", "ب", "ل"):
        if word.startswith(prefix) and len(word) - len(prefix) >= 3:
            return word[len(prefix):]
    return word


def _date_key(v: Any) -> int:
    digits = re.sub(r"\D", "", str(v or ""))[:8]
    return int(digits) if digits else 0
