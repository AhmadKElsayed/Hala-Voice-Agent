"""Complaint agent — free-speech complaint intake. The inmate describes the
problem in their own words; the agent classifies it into one of the routing
subcategories, asks only for what is missing (one question at a time), and
then moves to review. It never submits anything itself.
"""

from __future__ import annotations

import logging
import textwrap
from typing import Literal

from livekit.agents import ChatContext, RunContext, ToolError, function_tool
from livekit.agents.llm.tool_context import ToolFlag

from agent_tools import (
    CLASSIFICATION_GUIDE,
    check_classification,
    flush_draft_from_transcript,
)
from backend_client import BackendError, BackendNotFoundError
from base_agent import HalaAgent, MainMenuMixin, StaffHandoffMixin
from session_data import (
    FIELD_QUESTIONS_AR,
    OPEN_CASE,
    UNKNOWN_AR,
    SessionData,
    normalize_incident_date,
)

logger = logging.getLogger("hala-agent")

RelatedType = Literal["نزيل", "مسؤول حكومي", "جهة", "فرد آخر"]
# What the model passes when the incident happened in the inmate's own cell;
# replaced with the actual facility and cell from the profile.
OWN_CELL_WORDS = {"الزنزانة", "زنزانة النزيل", "غرفة النزيل", "الغرفة", "العنبر"}
RequestType = Literal["زيارة", "نقل", "تعيين محامٍ", "إفراج", "استرحام"]


class ComplaintAgent(StaffHandoffMixin, MainMenuMixin, HalaAgent):
    stage = "complaint"

    def __init__(self, *, data: SessionData, chat_ctx: ChatContext | None = None):
        super().__init__(
            data=data,
            chat_ctx=chat_ctx,
            role_instructions=textwrap.dedent("""\
                # Your role
                You take the inmate's complaint. Let them describe it in their
                own words, never offer a menu of categories. Your job is to
                end up with a complaint the authority can act on without
                having to come back and ask the inmate anything. Collect,
                one question at a time:
                First: what exactly happened (the complaint itself).
                Second: when it happened, or since when.
                Third: what the inmate wants done about it.
                And, when the complaint is about what a person did (an
                official, another inmate, or anyone else): who did it, where
                it happened, and whether anyone saw it.
                The details-recording tool tells you after every save what
                is still missing; follow it.
                You decide the complaint type yourself from what they said;
                never ask the inmate to choose a type.

                # Make the complaint specific (very important)
                A vague complaint is useless to whoever receives it. If the
                description does not say concretely what happened, ask one
                short follow-up question to get the facts, for example:
                - Food: what is wrong with it, the quantity, the quality or
                  the timing, and how often.
                - Clothing, basic needs or the canteen: which items are
                  missing, and whether they already asked for them.
                - The cell environment: what exactly (air conditioning,
                  cleanliness, crowding, water) and how long it has lasted.
                - An assault, threat or insult: what was done or said, and
                  whether they were injured. An injury or an ongoing threat
                  makes the complaint urgent.
                - Their case: which procedure is late or was not carried out,
                  and what they were told about it.
                - Visits or calls: who they want to reach and how long since
                  the last contact.
                - A lawyer: whether they have one now and what they need.
                Ask at most two follow-up questions about the description,
                then carry on with what you have. Never ask for something
                the inmate already said.
                Put the concrete facts into the summary: what, how often,
                which items, who. Do not write a summary that only repeats
                the complaint type.

                # Complaint types (pick exactly one code yourself)
                {taxonomy}

                {guide}
                - If the inmate reports being hit, insulted or threatened and
                  it is not clear by whom, ask who did it before choosing
                  the type.

                # Save every answer immediately (very important)
                - After every answer, call the details-recording tool with
                  what you heard before you ask the next question. It only
                  saves; it never ends the conversation.
                - Write the summary yourself in clear formal Arabic, in the
                  third person, starting with "يفيد النزيل" or "يطلب النزيل"
                  or "يعترض النزيل". One to three sentences. Also give it a
                  short title of three to six words. Update the summary
                  whenever the inmate adds a fact.
                - Save the date as a calendar date, YYYY-MM-DD, worked out
                  from today's date, and save the inmate's own words (for
                  example "قبل أسبوعين") separately. For something ongoing,
                  the date is when it started.
                - If the inmate says they do not know or do not remember a
                  detail, save that detail as "{unknown}" and never ask about
                  it again.
                - If they give several details in one sentence, save all of
                  them and do not ask about them again.
                - If it happened in the inmate's own cell, save the place as
                  their cell; otherwise save the place they named.
                - If they mention documents or evidence, save that too; do
                  not ask about documents yourself.

                # A complaint about their legal case
                - If the tool tells you the inmate has more than one open
                  case, ask which one they mean, describing each by its
                  charge, not its number. Then link the case they choose.

                # A complaint about an earlier request
                - If the complaint is about a request that was refused or
                  delayed (a visit, transfer, lawyer, release or clemency
                  request), call the request-lookup tool to find it, tell the
                  inmate which request you found in one short sentence and ask
                  if that is the one. If they say yes, link it.

                # The limits of your role (very important)
                - You only collect. Never read a full summary back, never say
                  the complaint was registered, and never give a complaint
                  number.
                - As soon as the details-recording tool says nothing is
                  missing, call the move-to-review tool. That is the only way
                  to move on.
                - If the inmate changes their mind and does not want to
                  complain, go back to the main assistant.
            """).format(
                taxonomy=data.taxonomy_prompt(), unknown=UNKNOWN_AR, guide=CLASSIFICATION_GUIDE
            ),
        )

    async def enter(self) -> None:
        data = self.session.userdata
        if data.draft.related_tracking_id:
            # Came from the follow-up agent: the complaint is about a request
            # that was just discussed and is already linked to the draft.
            await self.session.generate_reply(
                instructions=(
                    f"The inmate wants to complain about {data.draft.related_request_label}, "
                    "which was just discussed and is already linked to this complaint. "
                    "Call the details-recording tool now with a summary and the complaint "
                    "type based on what is known about that request, then ask the inmate, "
                    "in one short question, why they object to the decision."
                )
            )
            return
        if data.draft.original_statement:
            # The inmate already described the problem to the concierge: save
            # what is known and ask only what is missing.
            await self.session.generate_reply(
                instructions=(
                    "The inmate already described their complaint earlier in this "
                    "conversation. Do not ask them to repeat it. Call the "
                    "details-recording tool now with the summary, the complaint type "
                    "and anything else they already said, then ask the first missing "
                    "detail in one short question."
                )
            )
            return
        await self.session.say(FIELD_QUESTIONS_AR["complaint description"])

    @function_tool()
    async def record_complaint_details(
        self,
        context: RunContext[SessionData],
        title: str | None = None,
        summary: str | None = None,
        subcategory_code: str | None = None,
        incident_date: str | None = None,
        incident_date_text: str | None = None,
        incident_location: str | None = None,
        related_type: RelatedType | None = None,
        related_name: str | None = None,
        has_witnesses: bool | None = None,
        witnesses_info: str | None = None,
        has_documents: bool | None = None,
        expected_action: str | None = None,
        is_urgent: bool | None = None,
    ) -> str:
        """Saves any details the inmate gave about the complaint. A pure save
        tool: it stores data and tells you what is still missing.

        Args:
            title: A short Arabic noun phrase of three to six words naming the complaint, e.g. "نقص الملابس".
            summary: The complaint in one to three formal Arabic sentences, third person, with the concrete facts.
            subcategory_code: The complaint type code you chose from the list, e.g. "LIV-06".
            incident_date: The date it happened or started, as YYYY-MM-DD worked out from today's date (e.g. "2026-09-24"), or "غير معروفة" if the inmate does not know.
            incident_date_text: How the inmate said the time, in their own words (e.g. "قبل أسبوعين").
            incident_location: Where it happened. If it was in the inmate's own cell, pass "الزنزانة".
            related_type: The kind of person or entity complained about, if any.
            related_name: The name or description of that person or entity, or "غير معروفة".
            has_witnesses: Whether anyone saw it. False if the inmate says nobody did.
            witnesses_info: Who the witnesses are, if mentioned.
            has_documents: Whether the inmate has supporting papers, if mentioned.
            expected_action: What the inmate wants done about it, or "غير معروفة".
            is_urgent: True if the matter threatens the inmate's safety or health.
        """
        data = context.userdata
        draft = data.draft

        if incident_date is not None:
            try:
                incident_date = normalize_incident_date(incident_date)
            except ValueError as e:
                raise ToolError(str(e)) from None

        if incident_location:
            draft.location_confirmed = True
            if incident_location.strip() in OWN_CELL_WORDS:
                incident_location = data.inmate.default_location or incident_location

        if subcategory_code is not None:
            code = subcategory_code.strip().upper()
            if code not in data.taxonomy:
                raise ToolError(
                    f"'{subcategory_code}' is not a valid complaint type code. "
                    f"Choose one of: {', '.join(data.taxonomy)}."
                )
            draft.subcategory = code

        saved: list[str] = []
        for key, val in {
            "title": title, "summary": summary, "incident_date": incident_date,
            "incident_date_text": incident_date_text,
            "incident_location": incident_location, "related_type": related_type,
            "related_name": related_name, "has_witnesses": has_witnesses,
            "witnesses_info": witnesses_info, "has_documents": has_documents,
            "expected_action": expected_action, "is_urgent": is_urgent,
        }.items():
            if val is not None and val != "":
                setattr(draft, key, val)
                saved.append(key)

        case_note = await _check_cases(data)

        logger.info("[COMPLAINT] saved fields: %s subcategory=%s", saved or "(none)", draft.subcategory)
        await data.ui.draft_updated(data.review_fields(), stage="collecting")

        missing = data.missing_fields()
        if missing:
            logger.info("[FLOW] complaint details saved, still missing: %s", missing)
            return (
                f"Saved. Still missing, in this order: {', '.join(missing)}. "
                f"Ask about the first one only.{case_note}"
            )
        return (
            "Saved. Nothing required is missing. If the description is still vague, "
            "ask one follow-up for the concrete facts and save it; otherwise call "
            "the move-to-review tool now."
        )

    @function_tool()
    async def link_related_case(self, context: RunContext[SessionData], case_id: str) -> str:
        """Links the complaint to one of the inmate's open cases, after the
        inmate said which case they mean.

        Args:
            case_id: The case_id exactly as listed by the details-recording tool.
        """
        data = context.userdata
        option = next((c for c in data.draft.case_options if c["case_id"] == case_id.strip()), None)
        if option is None:
            raise ToolError(
                "That is not one of the inmate's open cases. Use a case_id from the list: "
                + ", ".join(c["case_id"] for c in data.draft.case_options)
            )
        data.draft.related_case_id = option["case_id"]
        data.draft.related_case_label = option["label"]
        logger.info("[COMPLAINT] linked to case %s", option["case_id"])
        await data.ui.draft_updated(data.review_fields(), stage="collecting")
        missing = data.missing_fields()
        if missing:
            return f"Linked. Still missing: {', '.join(missing)}. Ask about the first one only."
        return "Linked. Nothing required is missing. Call the move-to-review tool now."

    @function_tool()
    async def find_related_request(
        self, context: RunContext[SessionData], request_type: RequestType | None = None
    ) -> str:
        """Looks up the inmate's earlier requests so a complaint about a
        refused or delayed request can be linked to it.

        Args:
            request_type: The kind of request the complaint is about, if known.
        """
        try:
            requests = await context.userdata.backend.list_requests()
        except BackendError:
            logger.exception("find_related_request failed")
            raise ToolError(
                "The requests could not be looked up right now. Carry on with the "
                "complaint without linking a request."
            ) from None

        if request_type:
            requests = [r for r in requests if r["request_type"] == request_type]
        # Refused requests first — those are what complaints are usually about
        requests.sort(key=lambda r: r["current_status"] != "مرفوض")
        requests = requests[:3]
        if not requests:
            return "No matching earlier request was found. Carry on without linking one."

        lines = [
            f"- request_id {r['request_id']}: {r['request_type']} — {r['request_summary']} — "
            f"submitted {r['submission_date']} — status {r['current_status']}"
            + (f" — reason: {r['decision_reason']}" if r.get("decision_reason") else "")
            for r in requests
        ]
        return (
            "Earlier requests (most likely first):\n" + "\n".join(lines) + "\n"
            "Describe the most likely one to the inmate in one short sentence (its "
            "kind, when it was submitted and its status — not its number) and ask "
            "if the complaint is about it. If they say yes, call the link tool "
            "with its request_id."
        )

    @function_tool()
    async def link_related_request(self, context: RunContext[SessionData], request_id: str) -> str:
        """Links the complaint to one of the inmate's earlier requests, after
        the inmate confirmed it is the one they mean.

        Args:
            request_id: The request_id exactly as returned by the request-lookup tool.
        """
        data = context.userdata
        try:
            req = await data.backend.get_request(request_id.strip())
        except BackendNotFoundError:
            raise ToolError(
                "That request was not found for this inmate. Use a request_id "
                "returned by the request-lookup tool."
            ) from None
        except BackendError:
            logger.exception("link_related_request failed")
            raise ToolError("The request could not be linked right now. Carry on without it.") from None

        data.draft.related_tracking_id = req["tracking_id"]
        data.draft.related_request_label = f"طلب {req['request_type']} رقم {req['request_id']}"
        logger.info("[COMPLAINT] linked to request %s (%s)", req["request_id"], req["tracking_id"])
        await data.ui.draft_updated(data.review_fields(), stage="collecting")
        return "Linked. Continue with the remaining details."

    @function_tool(flags=ToolFlag.IGNORE_ON_ENTER)
    async def proceed_to_review(self, context: RunContext[SessionData]):
        """Use once the complaint details are complete, to move to the review stage."""
        from review_agent import ReviewAgent

        data = context.userdata

        # Safety net: recover any field the inmate said but that was never
        # saved through the tool. Must run before the missing-fields check,
        # since it may also recover a required field.
        recovered = await flush_draft_from_transcript(context)
        if recovered:
            logger.warning("[FLUSH] said in the conversation but never saved via the tool: %s", recovered)

        if not data.draft.original_statement:
            data.draft.original_statement = _first_user_statement(context)

        await check_classification(data)

        missing = data.missing_fields()
        if missing:
            logger.info("[FLOW] proceed_to_review blocked, missing: %s", missing)
            raise ToolError(f"Information is still missing: {', '.join(missing)}. Ask about it first.")

        logger.info("[FLOW] ComplaintAgent -> ReviewAgent")
        return ReviewAgent(data=data, chat_ctx=self.handoff_ctx())


async def _check_cases(data: SessionData) -> str:
    """For a complaint about the legal case: finds which case it concerns.

    One open case is linked automatically. With several, they are kept as
    options and returned as a note for the model so it asks the inmate which
    one. Runs once per draft, and never blocks the complaint if the lookup
    fails (the backend then routes by the most recently updated open case).
    """
    draft = data.draft
    if not data.is_case_related() or draft.cases_checked:
        return ""
    draft.cases_checked = True
    try:
        cases = await data.backend.list_cases()
    except BackendError:
        logger.exception("case lookup for the complaint failed (non-fatal)")
        return ""

    options = [
        {
            "case_id": c["case_id"],
            "label": f"القضية رقم {c['case_number']} ({c['charge_description']})",
            "charge": c["charge_description"],
            "stage": c["current_stage"],
        }
        for c in cases
        if c["case_status"] == OPEN_CASE
    ]
    if len(options) == 1:
        draft.related_case_id = options[0]["case_id"]
        draft.related_case_label = options[0]["label"]
        return ""
    if len(options) > 1:
        draft.case_options = options
        lines = "\n".join(
            f"- case_id {o['case_id']}: charge {o['charge']} — stage {o['stage']}" for o in options
        )
        return (
            "\nThe inmate has more than one open case:\n" + lines + "\n"
            "Ask which one the complaint is about, describing each by its charge, "
            "then call the case-link tool with its case_id."
        )
    return ""


def _first_user_statement(context: RunContext[SessionData]) -> str | None:
    """The longest thing the inmate said — kept as original_statement when
    the concierge did not pass one along."""
    texts = [
        item.text_content
        for item in context.session.history.items
        if getattr(item, "role", None) == "user" and getattr(item, "text_content", None)
    ]
    return max(texts, key=len) if texts else None
