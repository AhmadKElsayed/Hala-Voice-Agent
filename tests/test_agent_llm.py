"""End-to-end conversation tests in text mode: the real conversation model
(OpenAI) drives the real agents against the in-process mock backend.

Skipped unless a real OPENAI_API_KEY is set, since every test makes several
model calls:

    uv run python -m pytest tests/test_agent_llm.py
"""

from __future__ import annotations

from datetime import date, timedelta

import pytest
from livekit.agents import AgentSession

from agent_tools import build_llm
from complaint_agent import ComplaintAgent
from concierge_agent import ConciergeAgent
from conftest import HAS_OPENAI_KEY
from review_agent import ReviewAgent
from staff_agent import StaffAgent
from tracking_agent import TrackingAgent

pytestmark = pytest.mark.skipif(not HAS_OPENAI_KEY, reason="needs a real OPENAI_API_KEY")


def _session(data) -> AgentSession:
    return AgentSession(userdata=data, llm=build_llm(temperature=0))


async def test_complaint_is_classified_and_filed_only_after_approval(data, backend):
    before = len(await backend.list_complaints())
    async with _session(data) as session:
        await session.start(ConciergeAgent(data=data))

        await session.run(
            user_input="عندي شكوى، من أسبوعين ما صرفوا لي ملابس كافية وطلبت أكثر من مرة"
        )
        assert isinstance(session.current_agent, ComplaintAgent | ReviewAgent)
        # the agent must ask for what is missing rather than file a vague complaint
        answers = iter([
            "من أسبوعين تقريبًا",
            "أبيهم يصرفون لي ثوبين وملابس داخلية",
            "بس هذا اللي عندي",
            "ما عندي شي ثاني",
        ])
        while isinstance(session.current_agent, ComplaintAgent):
            await session.run(user_input=next(answers))
        assert isinstance(session.current_agent, ReviewAgent)
        assert data.draft.subcategory in {"LIV-03", "LIV-06"}
        # "two weeks ago" was turned into a real calendar date
        expected = date.today() - timedelta(days=14)
        assert abs((date.fromisoformat(data.draft.incident_date) - expected).days) <= 2
        assert data.draft.expected_action

        # read back, not yet approved: nothing may be filed
        assert not data.submitted
        assert len(await backend.list_complaints()) == before

        await session.run(user_input="ايه صح، أرسلها")
        assert data.submitted
        assert len(await backend.list_complaints()) == before + 1

        # approving again must not file a second complaint
        await session.run(user_input="أرسلها")
        assert len(await backend.list_complaints()) == before + 1


async def test_danger_goes_straight_to_staff_as_urgent(data, store):
    async with _session(data) as session:
        await session.start(ConciergeAgent(data=data))
        await session.run(user_input="فيه نزيل يهددني بسكين الحين في الغرفة")
        assert isinstance(session.current_agent, StaffAgent)
    assert store.staff_requests and store.staff_requests[-1]["urgent"] is True


async def test_follow_up_routes_to_tracking_and_reads_own_items_only(data):
    async with _session(data) as session:
        await session.start(ConciergeAgent(data=data))
        await session.run(user_input="أبي أعرف وش صار على شكواي حقت الملابس")
        assert isinstance(session.current_agent, TrackingAgent)
    paths = [e["payload"]["path"] for e in data.ui.sent if e["type"] == "backend_call"]
    assert paths and all("PR-0001" in p or p.startswith("/api/complaint-taxonomy") for p in paths)


async def test_rights_answer_comes_from_the_guide(data):
    async with _session(data) as session:
        await session.start(ConciergeAgent(data=data))
        await session.run(user_input="وش حقوقي في العلاج داخل التوقيف؟")
    searches = [e for e in data.ui.sent if e["type"] == "rights_results"]
    assert searches and searches[0]["payload"]["topics"][0]["topic_id"] == "health"


# The client's examples from the agreed classification table
CLIENT_EXAMPLES = [
    ("صدر لي أمر إفراج ولم يتم تنفيذه", {"SYS-05"}),
    ("أريد معرفة إجراء الاعتراض على الحكم", {"SYS-06"}),
    ("لدي نقص في الملابس", {"LIV-06"}),
    ("تعرضت للضرب داخل الزنزانة من نزيل آخر", {"LIV-07"}),
    ("أرغب في التواصل مع محامٍ", {"PRV-02"}),
    ("أريد التواصل مع ذويي", {"PRV-03"}),
    ("تعرضت للإهانة من أحد المسؤولين", {"ABU-01"}),
    ("تعرضت للضرب من مسؤول حكومي", {"ABU-02"}),
]


@pytest.mark.parametrize(("statement", "expected"), CLIENT_EXAMPLES)
async def test_classifier_on_client_examples(data, statement, expected):
    from agent_tools import classify_complaint

    result = await classify_complaint(statement, data)
    assert result is not None
    assert result["subcategory_code"] in expected


@pytest.mark.parametrize(("statement", "expected"), CLIENT_EXAMPLES)
async def test_conversation_model_on_client_examples(data, statement, expected):
    async with _session(data) as session:
        await session.start(ConciergeAgent(data=data))
        await session.run(user_input=f"عندي شكوى: {statement}")
        assert isinstance(session.current_agent, ComplaintAgent | ReviewAgent)
        assert data.draft.subcategory in expected


async def test_assault_complaint_is_not_filed_without_who_where_and_witnesses(data, backend):
    before = len(await backend.list_complaints())
    async with _session(data) as session:
        await session.start(ConciergeAgent(data=data))
        await session.run(user_input="أبي أقدم شكوى، أحد العساكر ضربني")
        # one statement is not enough: it must still be collecting details
        assert isinstance(session.current_agent, ComplaintAgent)
        assert data.missing_fields()

        answers = iter([
            "أمس بعد صلاة العصر",
            "ما أعرف اسمه، عسكري طويل في نوبة العصر",
            "في الممر عند باب العنبر",
            "ايه شافني اثنين من النزلاء اللي معي بالغرفة",
            "أبي يحاسبونه وما يتكرر",
            "ضربني على وجهي بيده وما فيه إصابة",
            "بس هذا اللي عندي",
            "لا ما عندي شي ثاني",
        ])
        while isinstance(session.current_agent, ComplaintAgent):
            await session.run(user_input=next(answers))

        assert isinstance(session.current_agent, ReviewAgent)
        d = data.draft
        assert d.subcategory == "ABU-02"
        assert d.related_name and d.location_confirmed and d.expected_action
        assert d.has_witnesses is True or d.witnesses_info
        assert d.incident_date == (date.today() - timedelta(days=1)).isoformat()
        assert len(await backend.list_complaints()) == before
