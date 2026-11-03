"""Session state shared between all agents (Inmate/ComplaintDraft/SessionData).
No Agent is defined here — the agents import from this module, not the other
way around.

The inmate is already authenticated by the kiosk before the session starts,
so unlike a phone line there is no "who is calling" step: SessionData is
created with the inmate's profile loaded, and the BackendClient inside it is
bound to that one prisoner_id for the whole session.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING, Any, Literal

if TYPE_CHECKING:
    from backend_client import BackendClient
    from ui_events import UIEvents

InputMode = Literal["voice", "text"]

UNKNOWN_AR = "غير معروفة"

# Complaint types that are about what a person did (abuse of authority, a
# fight or assault between inmates, assault by an individual). These cannot be
# acted on without who, where and whether anyone saw it.
PERSON_RELATED_TYPES = ("ABU-", "LIV-07", "PRV-05")

OPEN_CASE = "مفتوحة"

MONTHS_AR = (
    "يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو",
    "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر",
)
WEEKDAYS_EN = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

_DIGIT_MAP = {ord(a): str(i) for i, a in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGIT_MAP.update({ord(a): str(i) for i, a in enumerate("۰۱۲۳۴۵۶۷۸۹")})
_ISO_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def normalize_incident_date(raw: str, today: date | None = None) -> str:
    """Returns the incident date as 'YYYY-MM-DD', or UNKNOWN_AR.

    Dates are stored in one format only, so the review screen, the backend
    and the reports all agree. Raises ValueError (with a message meant for
    the model) for anything else, including a date in the future.
    """
    value = (raw or "").strip().translate(_DIGIT_MAP).replace("/", "-")
    if value == UNKNOWN_AR:
        return UNKNOWN_AR
    if not _ISO_DATE.match(value):
        raise ValueError(
            f"incident_date must be a calendar date written as YYYY-MM-DD, or '{UNKNOWN_AR}'. "
            f"'{raw}' is neither. Work the date out from today's date and what the inmate said."
        )
    try:
        parsed = date.fromisoformat(value)
    except ValueError:
        raise ValueError(f"'{raw}' is not a real calendar date. Use YYYY-MM-DD.") from None
    if parsed > (today or date.today()):
        raise ValueError(f"'{raw}' is in the future. The incident date cannot be after today.")
    return value


def date_ar(iso: str) -> str:
    """'2026-09-24' -> '24 سبتمبر 2026' (for speech; the screen keeps ISO)."""
    d = date.fromisoformat(iso)
    return f"{d.day} {MONTHS_AR[d.month - 1]} {d.year}"

# Ready-made questions for missing fields (spoken as-is from on_enter, without
# going through the LLM). Keys are the labels returned by missing_fields().
FIELD_QUESTIONS_AR = {
    "complaint description": "تفضّل، قل لي وش اللي صار معك بالضبط؟",
    "complaint type": "عشان أوجّه شكواك للجهة الصح، وش أكثر شي مضايقك في الموضوع؟",
    "which case the complaint is about": "أي قضية من قضاياك تقصد؟",
    "person or entity complained about": "مين الشخص أو الجهة اللي تشتكي منها؟ وإذا ما تعرف الاسم، قل لي وصفه.",
    "incident time": "متى صار هالشي تقريبًا؟",
    "incident place": "وين صار هالشي بالضبط؟",
    "witnesses": "فيه أحد شاف اللي صار؟",
    "what the inmate wants done": "وش الإجراء اللي تبيه يتّخذ في شكواك؟",
}

# What each draft field is called on screen (test page / kiosk review screen).
FIELD_LABELS_AR = {
    "title": "اسم الشكوى",
    "summary": "تفاصيل الشكوى",
    "category": "التصنيف الرئيسي",
    "subcategory": "التصنيف الفرعي",
    "incident_date": "تاريخ الواقعة",
    "incident_location": "مكان الواقعة",
    "related_name": "الجهة أو الشخص المشتكى منه",
    "witnesses_info": "الشهود",
    "expected_action": "الإجراء المطلوب",
    "related_request": "الطلب المرتبط",
    "related_case": "القضية المرتبطة",
}


@dataclass
class Inmate:
    prisoner_id: str
    full_name: str = ""
    first_name: str = ""
    facility_name: str = ""
    cell_block: str = ""

    @property
    def default_location(self) -> str:
        return " - ".join(p for p in (self.facility_name, self.cell_block) if p)


@dataclass
class ComplaintDraft:
    original_statement: str | None = None
    subcategory: str | None = None          # routing code, e.g. "LIV-06"
    title: str | None = None                # short noun phrase, e.g. "نقص الملابس"
    summary: str | None = None
    incident_date: str | None = None        # "YYYY-MM-DD", or UNKNOWN_AR
    incident_date_text: str | None = None   # the inmate's own words, e.g. "قبل أسبوعين"
    incident_location: str | None = None
    location_confirmed: bool = False        # False while it is only the pre-filled cell
    related_type: str | None = None         # نزيل / مسؤول حكومي / جهة / فرد آخر
    related_name: str | None = None
    has_witnesses: bool | None = None
    witnesses_info: str | None = None
    has_documents: bool | None = None
    expected_action: str | None = None
    related_tracking_id: str | None = None
    related_request_label: str | None = None
    related_case_id: str | None = None
    related_case_label: str | None = None
    # Open cases to choose from, when the inmate has more than one (SYS only)
    case_options: list[dict[str, Any]] = field(default_factory=list)
    cases_checked: bool = False
    is_urgent: bool = False
    ai_confidence: float | None = None
    classification_source: str = "المساعد الذكي"
    # fields the classifier disagreed on / needs the inmate to confirm
    needs_confirmation: set[str] = field(default_factory=set)


@dataclass
class SessionData:
    backend: BackendClient
    ui: UIEvents
    inmate: Inmate
    taxonomy: dict[str, dict[str, Any]] = field(default_factory=dict)
    input_mode: InputMode = "voice"
    draft: ComplaintDraft = field(default_factory=ComplaintDraft)
    submitted: bool = False
    complaint_id: str | None = None
    staff_request_id: str | None = None
    notes: list[str] = field(default_factory=list)

    # ---------------- taxonomy helpers ----------------------------------------

    def category_name(self, code: str | None) -> str | None:
        t = self.taxonomy.get(code or "")
        return t["category_name_ar"] if t else None

    def subcategory_name(self, code: str | None) -> str | None:
        t = self.taxonomy.get(code or "")
        return t["subcategory_name_ar"] if t else None

    def is_abuse(self) -> bool:
        return (self.draft.subcategory or "").startswith("ABU")

    def is_person_related(self) -> bool:
        return (self.draft.subcategory or "").startswith(PERSON_RELATED_TYPES)

    def is_case_related(self) -> bool:
        return (self.draft.subcategory or "").startswith("SYS")

    def taxonomy_prompt(self) -> str:
        """The 22 subcategories as compact lines for an LLM prompt."""
        lines = []
        for code, t in self.taxonomy.items():
            lines.append(f"- {code}: {t['category_name_ar']} / {t['subcategory_name_ar']}")
        return "\n".join(lines)

    # ---------------- draft state ---------------------------------------------

    def start_new_draft(self, statement: str | None = None) -> None:
        self.draft = ComplaintDraft(
            original_statement=statement,
            incident_location=self.inmate.default_location or None,
        )
        self.submitted = False
        self.complaint_id = None

    def missing_fields(self) -> list[str]:
        d = self.draft
        missing = []
        if not d.summary:
            missing.append("complaint description")
        if not d.subcategory:
            missing.append("complaint type")
        # A complaint about the legal case must say which case, when the
        # inmate has more than one open
        if self.is_case_related() and d.case_options and not d.related_case_id:
            missing.append("which case the complaint is about")
        if self.is_person_related() and not d.related_name:
            missing.append("person or entity complained about")
        if not d.incident_date:
            missing.append("incident time")
        if self.is_person_related():
            if not d.location_confirmed:
                missing.append("incident place")
            if d.has_witnesses is None and not d.witnesses_info:
                missing.append("witnesses")
        if not d.expected_action:
            missing.append("what the inmate wants done")
        return missing

    def incident_date_display(self) -> str | None:
        """What the review screen shows: the date, plus the inmate's words."""
        d = self.draft
        if not d.incident_date:
            return None
        if d.incident_date_text and d.incident_date_text != d.incident_date:
            return f"{d.incident_date} ({d.incident_date_text})"
        return d.incident_date

    def review_fields(self) -> list[dict[str, Any]]:
        """The draft as numbered fields for the review screen (Demo6 `fields`
        block): value + status 'ok' | 'ask' | 'empty'."""
        d = self.draft
        values = {
            "title": d.title,
            "summary": d.summary,
            "category": self.category_name(d.subcategory),
            "subcategory": self.subcategory_name(d.subcategory),
            "incident_date": self.incident_date_display(),
            "incident_location": d.incident_location,
            "related_name": d.related_name,
            "witnesses_info": d.witnesses_info or (None if d.has_witnesses is None else ("نعم" if d.has_witnesses else "لا يوجد")),
            "expected_action": d.expected_action,
            "related_request": d.related_request_label,
            "related_case": d.related_case_label,
        }
        always = (
            "title", "summary", "category", "subcategory", "incident_date",
            "incident_location", "expected_action",
        )
        out = []
        for key, value in values.items():
            if value is None and key not in always:
                continue
            status = "empty" if not value else ("ask" if key in d.needs_confirmation else "ok")
            out.append({
                "num": len(out) + 1,
                "key": key,
                "label": FIELD_LABELS_AR[key],
                "value": value,
                "status": status,
            })
        return out

    def summary_log(self) -> str:
        """Raw multi-line summary — for the log only, not for speech."""
        return "\n".join(f"{f['label']}: {f['value'] or 'غير محدد'}" for f in self.review_fields())

    def summary_natural_ar(self) -> str:
        """Short read-back built in plain Python (fast and consistent, no LLM)."""
        d = self.draft
        topic = d.title or self.subcategory_name(d.subcategory) or "موضوع لم يُحدَّد"
        parts = [f"شكواك عن {_shorten(topic, 80)}"]
        if d.related_name:
            parts.append(f"بحق {_shorten(d.related_name, 60)}")
        if d.incident_date and d.incident_date != UNKNOWN_AR:
            # spoken as "24 سبتمبر 2026"; written exactly as stored
            shown = d.incident_date if self.input_mode == "text" else date_ar(d.incident_date)
            parts.append(f"بتاريخ {shown}")
        if d.expected_action and d.expected_action != UNKNOWN_AR:
            parts.append(f"وطلبك {_shorten(d.expected_action, 80)}")
        sub = self.subcategory_name(d.subcategory)
        if sub:
            parts.append(f"وصنّفتها {self.category_name(d.subcategory)}، {sub}")
        if d.related_request_label:
            parts.append(f"ومرتبطة بـ{d.related_request_label}")
        return "، ".join(parts) + ". أرسلها كذا، ولا تبي تعدّل شي؟"

    def complaint_payload(self) -> dict[str, Any]:
        """Body of POST /api/complaints (see docs/API_CONTRACT.md)."""
        d = self.draft
        return {
            "prisoner_id": self.inmate.prisoner_id,
            "complaint_subcategory": d.subcategory,
            "complaint_title": d.title,
            "complaint_summary": d.summary,
            "confirmed_by_prisoner": True,
            "original_statement": d.original_statement,
            "input_channel": "نص" if self.input_mode == "text" else "صوت",
            "incident_date": d.incident_date,
            "incident_date_text": d.incident_date_text,
            "incident_location": d.incident_location,
            "incident_details": d.summary,
            "related_type": d.related_type,
            "related_name": d.related_name,
            "has_witnesses": _yn(d.has_witnesses),
            "witnesses_info": d.witnesses_info,
            "has_supporting_documents": _yn(d.has_documents),
            "expected_action": d.expected_action,
            "additional_notes": " | ".join(self.notes) or None,
            "related_tracking_id": d.related_tracking_id,
            "related_case_id": d.related_case_id,
            "is_urgent": d.is_urgent,
            "classification_source": d.classification_source,
            "ai_confidence": d.ai_confidence,
        }


def _yn(v: bool | None) -> str | None:
    return None if v is None else ("نعم" if v else "لا")


def _shorten(text: str, max_chars: int = 140) -> str:
    """Shortens a long text at the nearest word boundary, so a long sentence
    never slows down the spoken review summary."""
    text = text.strip().rstrip(".")
    if len(text) <= max_chars:
        return text
    return text[:max_chars].rsplit(" ", 1)[0] + "..."
