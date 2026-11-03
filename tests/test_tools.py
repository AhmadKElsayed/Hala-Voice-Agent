"""The agents' tools called directly (no LLM, no audio): the guards that must
hold whatever the model decides to do."""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from livekit.agents import ToolError

import agent_tools
import complaint_agent
from complaint_agent import ComplaintAgent
from concierge_agent import ConciergeAgent
from conftest import StubContext, user_said
from review_agent import ReviewAgent
from session_data import UNKNOWN_AR, date_ar, normalize_incident_date
from staff_agent import StaffAgent
from tracking_agent import TrackingAgent
from voice_rules import build_rules

TWO_WEEKS_AGO = (date.today() - timedelta(days=14)).isoformat()
TOMORROW = (date.today() + timedelta(days=1)).isoformat()


def _fill_clothing_draft(data) -> None:
    data.start_new_draft("ما عندي ملابس كافية من أسبوعين")
    data.draft.title = "نقص الملابس"
    data.draft.summary = "يفيد النزيل بنقص الملابس المصروفة له منذ أسبوعين."
    data.draft.subcategory = "LIV-06"
    data.draft.incident_date = TWO_WEEKS_AGO
    data.draft.incident_date_text = "قبل أسبوعين"
    data.draft.expected_action = "صرف ملابس إضافية"


def _fill_abuse_draft(data) -> None:
    _fill_clothing_draft(data)
    data.draft.subcategory = "ABU-02"
    data.draft.related_name = "أحد الحراس"
    data.draft.location_confirmed = True
    data.draft.has_witnesses = False


@pytest.fixture
def no_openai(monkeypatch):
    """Replaces the two OpenAI helpers so proceed_to_review runs offline."""
    calls = {"flush": 0, "classify": 0}

    async def fake_flush(context):
        calls["flush"] += 1
        return []

    async def fake_check(data):
        calls["classify"] += 1

    monkeypatch.setattr(complaint_agent, "flush_draft_from_transcript", fake_flush)
    monkeypatch.setattr(complaint_agent, "check_classification", fake_check)
    return calls


# ---------------------------------------------------------------- intake ----


async def test_record_details_reports_what_is_still_missing(data, ctx):
    data.start_new_draft()
    agent = ComplaintAgent(data=data)

    out = await agent.record_complaint_details(
        ctx, title="نقص الملابس", summary="يفيد النزيل بنقص الملابس.", subcategory_code="liv-06"
    )
    assert data.draft.subcategory == "LIV-06"
    # asked in a fixed order: when first, then what the inmate wants
    assert out.index("incident time") < out.index("what the inmate wants done")

    out = await agent.record_complaint_details(ctx, incident_date=UNKNOWN_AR)
    assert "incident time" not in out and "what the inmate wants done" in out

    out = await agent.record_complaint_details(ctx, expected_action="صرف ملابس إضافية")
    assert "Nothing required is missing" in out
    # the review screen was told about every save
    assert [e["type"] for e in data.ui.sent].count("draft_updated") == 3


async def test_record_details_rejects_unknown_type_code(data, ctx):
    data.start_new_draft()
    with pytest.raises(ToolError):
        await ComplaintAgent(data=data).record_complaint_details(ctx, subcategory_code="FOOD-01")
    assert data.draft.subcategory is None


async def test_location_defaults_to_the_inmates_own_cell(data):
    data.start_new_draft()
    assert data.draft.incident_location == "دار التوقيف بالرياض - عنبر 3 - غرفة 17"


@pytest.mark.parametrize("code", ["ABU-01", "ABU-02", "LIV-07", "PRV-05"])
async def test_complaint_about_a_person_needs_who_where_and_witnesses(data, ctx, code):
    _fill_clothing_draft(data)
    data.draft.subcategory = code
    assert data.missing_fields() == [
        "person or entity complained about", "incident place", "witnesses",
    ]

    agent = ComplaintAgent(data=data)
    await agent.record_complaint_details(ctx, related_name=UNKNOWN_AR)
    # the pre-filled cell does not count until the inmate says where it was
    out = await agent.record_complaint_details(ctx, incident_location="الزنزانة")
    assert data.draft.incident_location == data.inmate.default_location
    assert "witnesses" in out and "incident place" not in out

    out = await agent.record_complaint_details(ctx, has_witnesses=False)
    assert "Nothing required is missing" in out


async def test_ordinary_complaint_does_not_ask_who_or_witnesses(data):
    _fill_clothing_draft(data)
    assert data.missing_fields() == []
    data.draft.expected_action = None
    assert data.missing_fields() == ["what the inmate wants done"]


# ----------------------------------------------------------------- dates ----


def test_incident_date_is_normalized_to_one_format():
    assert normalize_incident_date("2026-09-24", date(2026, 10, 8)) == "2026-09-24"
    assert normalize_incident_date(" ٢٠٢٦/٠٩/٢٤ ", date(2026, 10, 8)) == "2026-09-24"
    assert normalize_incident_date(UNKNOWN_AR) == UNKNOWN_AR
    for bad in ["قبل أسبوعين", "24-09-2026", "2026-13-40", "2026-9-4", ""]:
        with pytest.raises(ValueError):
            normalize_incident_date(bad, date(2026, 10, 8))
    with pytest.raises(ValueError, match="future"):
        normalize_incident_date("2026-10-09", date(2026, 10, 8))


async def test_save_tool_rejects_words_and_future_dates(data, ctx):
    data.start_new_draft()
    agent = ComplaintAgent(data=data)
    for bad in ["قبل أسبوعين", TOMORROW]:
        with pytest.raises(ToolError):
            await agent.record_complaint_details(ctx, incident_date=bad)
    assert data.draft.incident_date is None

    await agent.record_complaint_details(
        ctx, incident_date=TWO_WEEKS_AGO, incident_date_text="قبل أسبوعين"
    )
    shown = next(f for f in data.review_fields() if f["key"] == "incident_date")
    assert shown["value"] == f"{TWO_WEEKS_AGO} (قبل أسبوعين)"


async def test_read_back_speaks_the_date_and_writes_it_in_text_mode(data):
    _fill_clothing_draft(data)
    data.draft.incident_date = "2026-09-24"
    assert "بتاريخ 24 سبتمبر 2026" in data.summary_natural_ar()
    assert date_ar("2026-01-05") == "5 يناير 2026"
    data.input_mode = "text"
    assert "بتاريخ 2026-09-24" in data.summary_natural_ar()


async def test_correcting_the_date_in_review_is_validated(data, ctx):
    _fill_clothing_draft(data)
    agent = ReviewAgent(data=data)
    with pytest.raises(ToolError):
        await agent.correct_field(ctx, "incident_date", "أمس")
    await agent.correct_field(ctx, "incident_date", "2026-09-01")
    assert data.draft.incident_date == "2026-09-01"
    assert data.draft.incident_date_text is None


def test_rules_carry_todays_date():
    rules = build_rules("voice", "ناصر", today=date(2026, 10, 8))
    assert "Today is Thursday, 2026-10-08" in rules
    assert "YYYY-MM-DD" in rules


# ----------------------------------------------------------- case linking ----


async def test_case_complaint_asks_which_case_when_several_are_open(data, ctx, backend):
    data.start_new_draft()
    agent = ComplaintAgent(data=data)
    out = await agent.record_complaint_details(
        ctx, title="تأخر القضية", summary="يفيد النزيل بتأخر إجراءات قضيته.", subcategory_code="SYS-03"
    )
    open_cases = [c for c in await backend.list_cases() if c["case_status"] == "مفتوحة"]
    assert len(open_cases) > 1
    assert "which case the complaint is about" in out
    assert all(c["case_id"] in out for c in open_cases)

    with pytest.raises(ToolError):
        await agent.link_related_case(ctx, "CASE-9999")
    await agent.link_related_case(ctx, open_cases[1]["case_id"])
    assert "which case the complaint is about" not in data.missing_fields()

    data.draft.incident_date = TWO_WEEKS_AGO
    data.draft.expected_action = "إفادتي بما تم"
    result = await backend.create_complaint(data.complaint_payload())
    # routed to the authority holding the case the inmate chose
    assert result["handling_authority"] == open_cases[1]["current_authority"]


async def test_single_open_case_is_linked_without_asking(data, ctx, monkeypatch):
    cases = [c for c in await data.backend.list_cases() if c["case_status"] == "مفتوحة"][:1]

    async def one_case():
        return cases

    monkeypatch.setattr(data.backend, "list_cases", one_case)
    data.start_new_draft()
    out = await ComplaintAgent(data=data).record_complaint_details(
        ctx, summary="يفيد النزيل بتأخر إجراءات قضيته.", subcategory_code="SYS-03"
    )
    assert data.draft.related_case_id == cases[0]["case_id"]
    assert "which case" not in out


async def test_proceed_to_review_blocked_while_details_missing(data, ctx, no_openai):
    data.start_new_draft()
    with pytest.raises(ToolError):
        await ComplaintAgent(data=data).proceed_to_review(ctx)
    assert no_openai["flush"] == 1


async def test_proceed_to_review_hands_off_when_complete(data, no_openai):
    _fill_clothing_draft(data)
    data.draft.original_statement = None
    ctx = StubContext(data, [user_said("ايه"), user_said("ما عندي ملابس كافية من أسبوعين")])
    nxt = await ComplaintAgent(data=data).proceed_to_review(ctx)
    assert isinstance(nxt, ReviewAgent)
    assert data.draft.original_statement == "ما عندي ملابس كافية من أسبوعين"
    assert no_openai == {"flush": 1, "classify": 1}


async def test_link_related_request_only_accepts_own_requests(data, ctx, backend):
    data.start_new_draft()
    agent = ComplaintAgent(data=data)
    mine = (await backend.list_requests())[0]
    await agent.link_related_request(ctx, mine["request_id"])
    assert data.draft.related_tracking_id == mine["tracking_id"]

    with pytest.raises(ToolError):
        await agent.link_related_request(ctx, "VIS-00-99999")


# ------------------------------------------------- classification check ----


async def test_classifier_agreement_stores_confidence(data, monkeypatch):
    _fill_clothing_draft(data)

    async def agree(text, d):
        return {"subcategory_code": "LIV-06", "confidence": 0.91}

    monkeypatch.setattr(agent_tools, "classify_complaint", agree)
    await agent_tools.check_classification(data)
    assert data.draft.ai_confidence == 0.91
    assert not data.draft.needs_confirmation


async def test_classifier_disagreement_flags_but_keeps_conversation_choice(data, monkeypatch):
    _fill_clothing_draft(data)

    async def disagree(text, d):
        return {"subcategory_code": "LIV-03", "confidence": 0.8}

    monkeypatch.setattr(agent_tools, "classify_complaint", disagree)
    await agent_tools.check_classification(data)
    assert data.draft.subcategory == "LIV-06"
    assert data.draft.needs_confirmation == {"category", "subcategory"}
    statuses = {f["key"]: f["status"] for f in data.review_fields()}
    assert statuses["subcategory"] == "ask"
    assert statuses["summary"] == "ok"


async def test_classifier_fills_missing_type_and_failure_is_harmless(data, monkeypatch):
    _fill_clothing_draft(data)
    data.draft.subcategory = None

    async def failed(text, d):
        return None

    monkeypatch.setattr(agent_tools, "classify_complaint", failed)
    await agent_tools.check_classification(data)
    assert data.draft.subcategory is None

    async def answer(text, d):
        return {"subcategory_code": "LIV-06", "confidence": 0.4}

    monkeypatch.setattr(agent_tools, "classify_complaint", answer)
    await agent_tools.check_classification(data)
    assert data.draft.subcategory == "LIV-06"
    # low confidence: adopted, but shown with a question mark
    assert "subcategory" in data.draft.needs_confirmation


# ---------------------------------------------------------------- review ----


async def test_submit_files_once_and_only_once(data, ctx, backend):
    _fill_clothing_draft(data)
    agent = ReviewAgent(data=data)
    before = len(await backend.list_complaints())

    out = await agent.submit_complaint(ctx)
    assert data.submitted and data.complaint_id in out
    assert "within 5 days" in out
    filed = await backend.get_complaint(data.complaint_id)
    assert filed["incident_date"] == TWO_WEEKS_AGO
    assert filed["incident_date_text"] == "قبل أسبوعين"

    again = await agent.submit_complaint(ctx)
    assert "already registered" in again
    assert len(await backend.list_complaints()) == before + 1

    stored = await backend.get_complaint(data.complaint_id)
    assert stored["input_channel"] == "صوت"
    assert stored["original_statement"] == "ما عندي ملابس كافية من أسبوعين"
    assert [e["type"] for e in data.ui.sent].count("complaint_submitted") == 1


async def test_text_mode_complaint_is_stored_as_text_channel(data, ctx, backend):
    data.input_mode = "text"
    _fill_clothing_draft(data)
    out = await ReviewAgent(data=data).submit_complaint(ctx)
    assert f"exactly as '{data.complaint_id}'" in out
    assert (await backend.get_complaint(data.complaint_id))["input_channel"] == "نص"


async def test_submit_refuses_incomplete_draft(data, ctx, backend):
    data.start_new_draft()
    before = len(await backend.list_complaints())
    with pytest.raises(ToolError):
        await ReviewAgent(data=data).submit_complaint(ctx)
    assert not data.submitted
    assert len(await backend.list_complaints()) == before


async def test_submit_failure_is_reported_as_not_sent(data, ctx, monkeypatch):
    from backend_client import BackendError

    _fill_clothing_draft(data)

    async def boom(payload):
        raise BackendError("down")

    monkeypatch.setattr(data.backend, "create_complaint", boom)
    with pytest.raises(ToolError, match="NOT sent"):
        await ReviewAgent(data=data).submit_complaint(ctx)
    assert not data.submitted and data.complaint_id is None


async def test_confidential_complaint_does_not_name_the_unit(data, ctx):
    _fill_abuse_draft(data)
    out = await ReviewAgent(data=data).submit_complaint(ctx)
    assert "confidentially" in out and "urgent" in out
    assert "وحدة شكاوى" not in out


async def test_correcting_the_type_marks_it_as_corrected_by_the_inmate(data, ctx):
    _fill_clothing_draft(data)
    data.draft.needs_confirmation = {"category", "subcategory"}
    agent = ReviewAgent(data=data)

    await agent.correct_field(ctx, "subcategory_code", "liv-03")
    assert data.draft.subcategory == "LIV-03"
    assert data.draft.classification_source == "صححها النزيل"
    assert not data.draft.needs_confirmation

    with pytest.raises(ToolError):
        await agent.correct_field(ctx, "subcategory_code", "nope")

    await agent.submit_complaint(ctx)
    with pytest.raises(ToolError):
        await agent.correct_field(ctx, "summary", "شيء آخر")


async def test_cancel_discards_the_draft(data, ctx, backend):
    _fill_clothing_draft(data)
    before = len(await backend.list_complaints())
    nxt = await ReviewAgent(data=data).cancel_complaint(ctx)
    assert isinstance(nxt, ConciergeAgent)
    assert data.draft.summary is None
    assert len(await backend.list_complaints()) == before


# -------------------------------------------------------------- tracking ----


async def test_tracking_cannot_read_another_inmates_complaint(data, ctx, store):
    foreign = next(c for c in store.complaints if c["prisoner_id"] != data.inmate.prisoner_id)
    with pytest.raises(ToolError):
        await TrackingAgent(data=data).get_complaint_status(ctx, foreign["complaint_id"])


async def test_tracking_status_readout_has_no_internal_text(data, ctx, backend, store):
    mine = (await backend.list_complaints())[0]
    out = await TrackingAgent(data=data).get_complaint_status(ctx, mine["complaint_id"])
    assert mine["complaint_status"] in out
    internal = {
        a["action_description_internal"]
        for a in store.actions
        if a["complaint_id"] == mine["complaint_id"]
    }
    assert not any(text in out for text in internal)


async def test_complain_about_request_starts_a_linked_draft(data, ctx, backend):
    req = (await backend.list_requests())[0]
    nxt = await TrackingAgent(data=data).complain_about_request(ctx, req["request_id"])
    assert isinstance(nxt, ComplaintAgent)
    assert data.draft.related_tracking_id == req["tracking_id"]


# ------------------------------------------------------- handoffs / rules ----


async def test_staff_handoff_carries_reason_and_urgency(data, ctx):
    nxt = await ConciergeAgent(data=data).request_staff_help(ctx, reason="تهديد من نزيل", urgent=True)
    assert isinstance(nxt, StaffAgent)
    assert nxt._urgent and nxt._reason == "تهديد من نزيل"


def test_rules_differ_between_voice_and_text():
    voice, text = build_rules("voice", "ناصر"), build_rules("text", "ناصر")
    assert "converted to speech" in voice and "converted to speech" not in text
    assert "in digits" in text
    assert "ناصر" in voice


async def test_backend_client_never_sends_a_model_supplied_prisoner_id(data, backend, store):
    payload = {**_payload(data), "prisoner_id": "PR-0002"}
    created = await backend.create_complaint(payload)
    stored = next(c for c in store.complaints if c["complaint_id"] == created["complaint_id"])
    assert stored["prisoner_id"] == "PR-0001"


def _payload(data) -> dict:
    _fill_clothing_draft(data)
    return data.complaint_payload()
