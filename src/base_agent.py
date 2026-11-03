"""Base class and shared handoff tools for all Hala agents.

Every agent's instructions are: the shared speaking rules (voice_rules.py,
which depend on voice vs text-only mode and the inmate's first name) + that
agent's own role text. Keeping the role text on the instance lets the
instructions be rebuilt when the kiosk flips between voice and text-only in
the middle of a session (see agent.py, RPC "hala.set_mode").

Note on imports: handoffs import the target agent locally inside the method
(not at the top of the file) because the agents hand off to each other in
both directions, which would otherwise be a circular import.
"""

from __future__ import annotations

import logging

from livekit.agents import NOT_GIVEN, Agent, ChatContext, RunContext, function_tool
from livekit.agents.llm.tool_context import ToolFlag

from session_data import SessionData
from voice_rules import build_rules

logger = logging.getLogger("hala-agent")


class HalaAgent(Agent):
    # Shown on the test page / kiosk: which stage the conversation is in
    stage = "concierge"

    def __init__(
        self,
        *,
        data: SessionData,
        role_instructions: str,
        chat_ctx: ChatContext | None = None,
        tts=NOT_GIVEN,
    ):
        self._role_instructions = role_instructions
        super().__init__(
            instructions=self._compose(data),
            chat_ctx=chat_ctx,
            tts=tts,
        )

    def _compose(self, data: SessionData) -> str:
        return build_rules(data.input_mode, data.inmate.first_name) + "\n" + self._role_instructions

    async def refresh_instructions(self) -> None:
        """Rebuilds the instructions after the input mode changed mid-session."""
        await self.update_instructions(self._compose(self.session.userdata))

    def handoff_ctx(self) -> ChatContext:
        return self.chat_ctx.copy(exclude_instructions=True)

    async def on_enter(self) -> None:
        logger.info("[FLOW] entered %s", type(self).__name__)
        await self.session.userdata.ui.agent_changed(self.stage)
        await self.enter()

    async def enter(self) -> None:
        """What this agent says/does first. Overridden by each agent."""


class StaffHandoffMixin:
    """Gives an agent the 'call a human' handoff."""

    @function_tool(flags=ToolFlag.IGNORE_ON_ENTER)
    async def request_staff_help(self, context: RunContext[SessionData], reason: str, urgent: bool):
        """Calls a human staff member for the inmate.

        Use it when the inmate asks to talk to a person or an employee, or
        when you cannot help with what they need. Use it IMMEDIATELY with
        urgent=true, before any other question, if the inmate is in danger
        right now: being assaulted or threatened, injured or seriously ill,
        or thinking of harming themselves or someone else.

        Args:
            reason: Why the inmate needs a staff member, in one short Arabic sentence.
            urgent: True only for immediate danger to someone's safety or health.
        """
        from staff_agent import StaffAgent

        logger.info("[FLOW] %s -> StaffAgent (urgent=%s)", type(self).__name__, urgent)
        return StaffAgent(
            data=context.userdata, reason=reason, urgent=urgent, chat_ctx=self.handoff_ctx()
        )


class MainMenuMixin:
    """Gives an agent the way back to the concierge."""

    @function_tool(flags=ToolFlag.IGNORE_ON_ENTER)
    async def back_to_main_menu(self, context: RunContext[SessionData]):
        """Returns to the main assistant. Use it when the inmate is done with
        the current service, or asks for a different service than the one
        you handle (filing a complaint, following up a complaint or request,
        asking about rights or their case)."""
        from concierge_agent import ConciergeAgent

        logger.info("[FLOW] %s -> ConciergeAgent", type(self).__name__)
        return ConciergeAgent(data=context.userdata, returning=True, chat_ctx=self.handoff_ctx())
