"""Staff agent — calls a human staff member for the inmate.

The request is sent to the backend in on_enter, in plain Python, before the
model says anything: when an inmate is in danger, raising the alert must not
depend on the model deciding to call a tool. For urgent cases this agent
also has its own more serious-sounding TTS voice.
"""

from __future__ import annotations

import logging
import textwrap

from livekit.agents import NOT_GIVEN, ChatContext, RunContext, function_tool

import config
from backend_client import BackendError
from base_agent import HalaAgent, MainMenuMixin
from session_data import SessionData

logger = logging.getLogger("hala-agent")


def _urgent_tts():
    """The urgent voice. Only the OpenAI provider has a separate one; with any
    other provider the session's normal voice is kept."""
    if config.TTS_PROVIDER != "openai":
        return NOT_GIVEN
    from livekit.plugins import openai

    return openai.TTS(
        model=config.URGENT_TTS_MODEL,
        voice=config.URGENT_TTS_VOICE,
        instructions=config.URGENT_TTS_INSTRUCTIONS,
    )


class StaffAgent(MainMenuMixin, HalaAgent):
    stage = "staff"

    def __init__(
        self,
        *,
        data: SessionData,
        reason: str | None = None,
        urgent: bool = False,
        chat_ctx: ChatContext | None = None,
    ):
        self._reason = reason
        self._urgent = urgent
        super().__init__(
            data=data,
            chat_ctx=chat_ctx,
            tts=_urgent_tts() if urgent and data.input_mode == "voice" else NOT_GIVEN,
            role_instructions=textwrap.dedent("""\
                # Your role
                A human staff member has just been called for the inmate, and
                the inmate has been told. You stay with them until the staff
                member arrives.

                # Rules
                - Never say a staff member was called unless you were told so
                  above in this conversation. If you were told the call
                  failed, say so honestly.
                - If this is an urgent case: keep every reply to one very
                  short, calm sentence. Tell the inmate to stay near the
                  kiosk if it is safe, and to move away from the source of
                  danger. Ask nothing unless it helps the staff reach them.
                  If the inmate adds an important detail, pass it on with the
                  note tool.
                - Never promise how fast the staff member will arrive beyond
                  what you were told.
                - Do not try to solve the problem yourself and do not give
                  medical or legal advice.
                - If the inmate wants to use another service while waiting,
                  go back to the main assistant.
            """),
        )

    async def enter(self) -> None:
        data = self.session.userdata
        try:
            result = await data.backend.request_staff(self._reason, self._urgent)
        except BackendError:
            # Never stay silent about a failed alert
            logger.critical(
                "STAFF REQUEST NOT DELIVERED prisoner=%s urgent=%s reason=%r",
                data.inmate.prisoner_id, self._urgent, self._reason,
            )
            await self.session.say(
                "المعذرة، ما قدرت أوصّل طلبك للموظف من هنا. نادِ أقرب موظف عندك الحين."
                if self._urgent
                else "المعذرة، ما قدرت أوصّل طلبك للموظف الحين. جرّب بعد شوي أو اطلب من أقرب موظف."
            )
            return

        data.staff_request_id = result["assistance_id"]
        logger.info("[STAFF] request created: %s", result)
        await data.ui.staff_requested({**result, "reason": self._reason})

        if self._urgent:
            await self.session.say(
                "بلّغت الموظف المناوب الحين وهو جايك. خلّك قريب من الجهاز، وابعد عن أي خطر."
            )
        else:
            await self.session.say(
                "أبشر، طلبت لك موظف مختص وبيجيك خلال دقايق. تبي شي ثاني وإنت تنتظر؟"
            )

    @function_tool()
    async def add_note_for_staff(self, context: RunContext[SessionData], note: str) -> str:
        """Passes an extra detail to the staff member who was called (for
        example where exactly the inmate is, or what is happening now).

        Args:
            note: The detail, in one short Arabic sentence.
        """
        data = context.userdata
        if not data.staff_request_id:
            return "No staff request is open. Tell the inmate to tell the staff member directly."
        try:
            await data.backend.add_staff_note(data.staff_request_id, note)
        except BackendError:
            logger.exception("add_note_for_staff failed")
            return "The note could not be delivered. Tell the inmate to tell the staff member directly."
        return "The note was passed to the staff."
