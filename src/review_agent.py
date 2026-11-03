"""Review agent — the last stop before a complaint is filed. Reads back a
short summary, applies corrections, and — only after the inmate's explicit
approval — is the only place that actually submits the complaint to the
backend.
"""

from __future__ import annotations

import logging
import textwrap
from typing import Literal

from livekit.agents import ChatContext, RunContext, ToolError, function_tool
from livekit.agents.llm.tool_context import ToolFlag

from agent_tools import reference_readout_hint
from backend_client import BackendError
from base_agent import HalaAgent, MainMenuMixin, StaffHandoffMixin
from session_data import SessionData, normalize_incident_date

logger = logging.getLogger("hala-agent")

CorrectableField = Literal[
    "title", "summary", "subcategory_code", "incident_date", "incident_location",
    "related_name", "witnesses_info", "expected_action",
]


class ReviewAgent(StaffHandoffMixin, MainMenuMixin, HalaAgent):
    stage = "review"

    def __init__(self, *, data: SessionData, chat_ctx: ChatContext | None = None):
        super().__init__(
            data=data,
            chat_ctx=chat_ctx,
            role_instructions=textwrap.dedent("""\
                # Your role
                You handle the final review of the complaint. A short summary
                was just read out to the inmate.
                First: apply any correction they ask for.
                Second: after their explicit approval, submit the complaint
                and give them its number.

                # Complaint types (used only if they correct the type)
                {taxonomy}

                # Rules
                - If they ask to change something, correct that field
                  immediately and read back only the corrected part in one
                  short sentence, then ask again whether to send it.
                - If they say the type or classification is wrong, ask what
                  the complaint is mainly about if it is not already clear,
                  pick the right code yourself and correct it.
                - If they add a new detail, save it with the correction tool.
                - Do not add details of your own.

                # Submission (very important)
                - As soon as the inmate approves (says "ايه" or "نعم" or
                  "أرسلها" or "تمام" or similar), call the submission tool
                  immediately.
                - Never say the complaint was sent or registered, and never
                  give a number, unless the submission tool returned it. The
                  number you say is exactly the one the tool returned.
                - Never submit the complaint twice.
                - If the inmate decides not to file the complaint, cancel it.
                - After a successful submission, ask if they need anything
                  else; if they do, go back to the main assistant.
            """).format(taxonomy=data.taxonomy_prompt()),
        )

    async def enter(self) -> None:
        data = self.session.userdata
        # Full log of the state at the moment of entering review — the only
        # checkpoint before the complaint is submitted.
        logger.info("[FLOW] review summary read to the inmate\n%s", data.summary_log())
        await data.ui.draft_updated(data.review_fields(), stage="review")
        # Ready-made short summary built in Python, spoken without the LLM
        await self.session.say(data.summary_natural_ar())

    @function_tool()
    async def correct_field(
        self,
        context: RunContext[SessionData],
        field_name: CorrectableField,
        new_value: str,
    ) -> str:
        """Corrects or adds a single field of the complaint at the inmate's request.

        Args:
            field_name: The field to correct.
            new_value: The new value. For subcategory_code, the code from the list (e.g. "PRV-03"). For incident_date, a calendar date as YYYY-MM-DD worked out from today's date, or "غير معروفة". Otherwise Arabic text.
        """
        data = context.userdata
        draft = data.draft
        if data.submitted:
            raise ToolError("The complaint was already submitted and can no longer be changed.")

        if field_name == "subcategory_code":
            code = new_value.strip().upper()
            if code not in data.taxonomy:
                raise ToolError(
                    f"'{new_value}' is not a valid complaint type code. "
                    f"Choose one of: {', '.join(data.taxonomy)}."
                )
            draft.subcategory = code
            draft.classification_source = "صححها النزيل"
            draft.ai_confidence = None
            draft.needs_confirmation -= {"category", "subcategory"}
            spoken = f"{data.category_name(code)}، {data.subcategory_name(code)}"
        elif field_name == "incident_date":
            try:
                draft.incident_date = normalize_incident_date(new_value)
            except ValueError as e:
                raise ToolError(str(e)) from None
            # the inmate's earlier wording no longer describes this date
            draft.incident_date_text = None
            draft.needs_confirmation.discard(field_name)
            spoken = draft.incident_date
        else:
            setattr(draft, field_name, new_value.strip())
            if field_name == "incident_location":
                draft.location_confirmed = True
            draft.needs_confirmation.discard(field_name)
            spoken = new_value.strip()

        logger.info("[CORRECT] %s -> %r", field_name, new_value)
        await data.ui.draft_updated(data.review_fields(), stage="review")
        return f"Updated. The new value is: {spoken}. Confirm it briefly and ask whether to send the complaint."

    @function_tool(flags=ToolFlag.IGNORE_ON_ENTER)
    async def submit_complaint(self, context: RunContext[SessionData]) -> str:
        """Files the complaint and returns its number. Use it only once, and
        only after the inmate's explicit approval."""
        context.disallow_interruptions()
        data = context.userdata
        logger.info("[SUBMIT] submit_complaint called (submitted=%s)", data.submitted)

        if data.submitted:
            return (
                f"The complaint is already registered under number {data.complaint_id}. "
                "Do not submit it again."
            )

        missing = data.missing_fields()
        if missing:
            raise ToolError(f"The complaint is incomplete: {', '.join(missing)}. Ask about it first.")

        try:
            result = await data.backend.create_complaint(data.complaint_payload())
        except BackendError:
            logger.exception("[SUBMIT] create_complaint failed")
            raise ToolError(
                "A technical problem occurred and the complaint was NOT sent. "
                "Apologize, tell the inmate it was not sent, and offer to try "
                "again or to call a staff member."
            ) from None

        data.submitted = True
        data.complaint_id = result["complaint_id"]
        logger.info("[SUBMIT] complaint filed: %s\n%s", result, data.summary_log())
        await data.ui.complaint_submitted({**result, "fields": data.review_fields()})

        parts = [
            f"The complaint was registered. {reference_readout_hint(result['complaint_id'], data.input_mode)}",
            f"Tell the inmate a reply is expected within {result['sla_days']} days.",
        ]
        if result.get("is_confidential"):
            parts.append(
                "Tell them this complaint is handled confidentially by a unit of "
                "the Public Prosecution outside their facility, so do not name "
                "the unit."
            )
        else:
            parts.append(f"It was referred to: {result['handling_authority']}.")
        if result.get("is_urgent"):
            parts.append("Tell them it was marked urgent.")
        parts.append("Then ask if they need anything else.")
        return " ".join(parts)

    @function_tool(flags=ToolFlag.IGNORE_ON_ENTER)
    async def cancel_complaint(self, context: RunContext[SessionData]):
        """Discards the complaint without sending it, when the inmate decides
        not to file it, and returns to the main assistant."""
        from concierge_agent import ConciergeAgent

        data = context.userdata
        if data.submitted:
            raise ToolError("The complaint was already submitted and cannot be cancelled here.")
        logger.info("[FLOW] complaint cancelled by the inmate")
        data.start_new_draft()
        await data.ui.draft_updated([], stage="cancelled")
        return ConciergeAgent(data=data, returning=True, chat_ctx=self.handoff_ctx())
