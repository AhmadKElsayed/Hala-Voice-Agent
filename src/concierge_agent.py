"""Concierge agent — the first voice the inmate hears. Greets them by name,
works out which of the four services they need, and routes.
"""

from __future__ import annotations

import logging
import textwrap
from typing import Literal

from livekit.agents import ChatContext, RunContext, function_tool
from livekit.agents.llm.tool_context import ToolFlag

from base_agent import HalaAgent, StaffHandoffMixin
from session_data import SessionData

logger = logging.getLogger("hala-agent")


class ConciergeAgent(StaffHandoffMixin, HalaAgent):
    stage = "concierge"

    def __init__(
        self,
        *,
        data: SessionData,
        returning: bool = False,
        chat_ctx: ChatContext | None = None,
    ):
        self._returning = returning
        super().__init__(
            data=data,
            chat_ctx=chat_ctx,
            role_instructions=textwrap.dedent("""\
                # Your role
                You are the main assistant. Work out what the inmate needs and
                route them to the right service. You do not handle the
                services yourself.

                # Services
                - A complaint or a problem they want to report: start a
                  complaint.
                - Asking what happened to an earlier complaint or request
                  (visit, transfer, lawyer, release, clemency): follow-up.
                - Asking about their rights, or about their own case (its
                  stage, the next court session, what comes next):
                  information.
                - Asking for a person or an employee, or anything you cannot
                  help with: call a staff member.

                # Rules
                - If the need is clear from the first sentence, route
                  immediately without asking anything.
                - If the inmate already started describing a problem, that is
                  a complaint: start it and pass along what they said, do not
                  ask them to repeat it.
                - If the inmate says they have a complaint or want to file
                  one ("شكوى"), always start a complaint, even when it is
                  worded as wanting to know something.
                - Route to exactly one service per turn.
                - If what they said is unclear, ask exactly one short
                  clarifying question.
                - Do not collect complaint details and do not answer questions
                  about rights, cases or statuses yourself; the service you
                  route to does that.
                - When the inmate says they are finished and need nothing
                  else, end the session.
            """),
        )

    async def enter(self) -> None:
        # Fixed lines with no LLM call, so the first response is instant
        if self._returning:
            await self.session.say("أبشر. وش أقدر أخدمك فيه بعد؟")
            return
        name = self.session.userdata.inmate.first_name
        hello = f"هلا {name}" if name else "هلا بك"
        await self.session.say(
            f"{hello}، أنا المساعد الذكي لخدمات النزلاء. "
            "تبي تقدّم شكوى، تتابع شكوى أو طلب سابق، ولا عندك سؤال عن حقوقك أو قضيتك؟"
        )

    @function_tool(flags=ToolFlag.IGNORE_ON_ENTER)
    async def start_complaint(
        self, context: RunContext[SessionData], first_statement: str | None = None
    ):
        """Starts a new complaint and hands the inmate to the complaint service.

        Args:
            first_statement: What the inmate already said about the problem, in their own words. Leave empty if they only said they want to file a complaint.
        """
        from complaint_agent import ComplaintAgent

        context.userdata.start_new_draft(first_statement)
        logger.info("[FLOW] ConciergeAgent -> ComplaintAgent (statement=%r)", first_statement)
        return ComplaintAgent(data=context.userdata, chat_ctx=self.handoff_ctx())

    @function_tool(flags=ToolFlag.IGNORE_ON_ENTER)
    async def follow_up_requests(
        self, context: RunContext[SessionData], about: str | None = None
    ):
        """Hands the inmate to the follow-up service for earlier complaints and requests.

        Args:
            about: Which complaint or request they asked about, in their own words, if they named one (e.g. "طلب الزيارة"). Leave empty otherwise.
        """
        from tracking_agent import TrackingAgent

        logger.info("[FLOW] ConciergeAgent -> TrackingAgent (about=%r)", about)
        return TrackingAgent(data=context.userdata, about=about, chat_ctx=self.handoff_ctx())

    @function_tool(flags=ToolFlag.IGNORE_ON_ENTER)
    async def answer_question(
        self,
        context: RunContext[SessionData],
        topic: Literal["rights", "case"],
        question: str | None = None,
    ):
        """Hands the inmate to the information service for questions about inmate rights or about their own case.

        Args:
            topic: "rights" for inmate rights and procedures, "case" for the status of their own case.
            question: The question as the inmate asked it, if they already asked one.
        """
        from info_agent import InfoAgent

        logger.info("[FLOW] ConciergeAgent -> InfoAgent (topic=%s)", topic)
        return InfoAgent(
            data=context.userdata, topic=topic, question=question, chat_ctx=self.handoff_ctx()
        )

    @function_tool(flags=ToolFlag.IGNORE_ON_ENTER)
    async def end_session(self, context: RunContext[SessionData]) -> str:
        """Ends the session once the inmate confirms they need nothing else."""
        await context.userdata.ui.session_ended()
        return (
            "The kiosk will close the session now. Say goodbye in one short, "
            "kind sentence and do not ask any further question."
        )
