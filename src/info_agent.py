"""Information agent — answers questions about inmate rights (from the
rights guide only) and about the inmate's own case (from disclosable case
data only). It never gives legal advice and never answers from its own
knowledge.
"""

from __future__ import annotations

import logging
import textwrap
from typing import Any, Literal

from livekit.agents import ChatContext, RunContext, ToolError, function_tool
from livekit.agents.llm.tool_context import ToolFlag

from backend_client import BackendError
from base_agent import HalaAgent, MainMenuMixin, StaffHandoffMixin
from session_data import SessionData

logger = logging.getLogger("hala-agent")

OPEN_CASE = "مفتوحة"


def _case_lines(c: dict[str, Any]) -> list[str]:
    lines = [
        f"Case number {c['case_number']} ({c['case_status']}):",
        f"- charge: {c['charge_description']}",
        f"- stage: {c['current_stage']}",
        f"- currently with: {c['current_authority']}",
        f"- last update: {c['last_update_date']}",
    ]
    proc = c.get("current_procedure")
    if proc:
        lines.append(f"- latest registered action: {proc['last_registered_action']}")
        if proc.get("prisoner_facing_next_step"):
            lines.append(f"- next step: {proc['prisoner_facing_next_step']}")
    for key, label in (
        ("next_session_date", "next court session"),
        ("judgment_date", "judgment date"),
        ("objection_deadline", "deadline to object to the judgment"),
        ("release_order_date", "release order date"),
        ("release_order_status", "release order status"),
    ):
        if c.get(key):
            lines.append(f"- {label}: {c[key]}")
    return lines


class InfoAgent(StaffHandoffMixin, MainMenuMixin, HalaAgent):
    stage = "info"

    def __init__(
        self,
        *,
        data: SessionData,
        topic: Literal["rights", "case"] = "rights",
        question: str | None = None,
        chat_ctx: ChatContext | None = None,
    ):
        self._topic = topic
        self._question = question
        super().__init__(
            data=data,
            chat_ctx=chat_ctx,
            role_instructions=textwrap.dedent("""\
                # Your role
                You answer two kinds of questions: about inmate rights, and
                about the inmate's own case.

                # Rights questions
                - Always call the rights-search tool first and answer only
                  from what it returns. Never answer from your own knowledge
                  of law or regulations.
                - If the tool returns an article, explain it in two or three
                  short sentences in simple words.
                - If the tool returns only a topic title and a one-line
                  description, say just that, and say the full details of
                  this topic are available from the staff. Do not add
                  anything of your own.
                - If nothing matches, say you do not have information on that
                  and offer to call a staff member.
                - If the inmate says a right is not being respected, tell
                  them they can file a complaint and offer to start one.

                # Case questions
                - Call the case tool and answer only from what it returns:
                  the stage, who has the case now, the latest registered
                  action, the next step and the next session date if there
                  is one.
                - If the inmate has more than one open case and did not say
                  which, answer about the most recently updated open case and
                  mention briefly that there is another one.
                - Never explain what a charge means for them, never estimate
                  a sentence or a release date, and never say whether a
                  decision was right. Say that is for their lawyer or the
                  competent authority.
                - If the information they want is not in what the tool
                  returned, say it is not available to you.

                # Rules
                - Do not read out case numbers unless the inmate asks for
                  them.
                - If they want to file a complaint, hand them to the complaint
                  service. For anything else, go back to the main assistant.
            """),
        )

    async def enter(self) -> None:
        if self._question:
            # The inmate already asked: answer straight away using the tools
            await self.session.generate_reply(
                instructions=(
                    f"The inmate asked: {self._question}\n"
                    "Use the right tool now and answer that question."
                )
            )
            return
        if self._topic == "case":
            await self.session.say("أبشر. وش تبي تعرف عن قضيتك؟")
        else:
            await self.session.say("أبشر. أي حق من حقوقك تبي تعرف عنه؟")

    @function_tool()
    async def search_rights(self, context: RunContext[SessionData], query: str) -> str:
        """Searches the inmate-rights guide.

        Args:
            query: What the inmate wants to know, as a few Arabic keywords (e.g. "الرعاية الصحية العلاج").
        """
        data = context.userdata
        try:
            topics = await data.backend.search_rights(query)
        except BackendError:
            logger.exception("search_rights failed")
            raise ToolError(
                "The rights guide could not be reached right now. Apologize and offer to call a staff member."
            ) from None

        await data.ui.rights_results(query, topics)
        if not topics:
            try:
                all_topics = await data.backend.list_rights_topics()
            except BackendError:
                all_topics = []
            titles = "، ".join(t["title"] for t in all_topics)
            return (
                "Nothing in the guide matched. Tell the inmate you have no information "
                "on that. The guide covers these topics, you may name the two or three "
                f"closest ones: {titles}"
            )

        blocks = []
        for t in topics:
            art = t.get("article")
            if art:
                bullets = "\n".join(f"  - {b}" for b in art.get("bullets", []))
                blocks.append(f"Topic: {t['title']}\nArticle: {art['lead']}\n{bullets}\nSource: {t.get('source')}")
            else:
                blocks.append(
                    f"Topic: {t['title']}\nOne-line description only (no detailed text available): {t['summary']}"
                )
        return (
            "Guide results, most relevant first:\n\n" + "\n\n".join(blocks) + "\n\n"
            "Answer from the most relevant topic only, in two or three short sentences."
        )

    @function_tool()
    async def get_my_case_status(self, context: RunContext[SessionData]) -> str:
        """Returns the inmate's cases with their current stage, the latest
        registered action, the next step and the next session date."""
        data = context.userdata
        try:
            cases = await data.backend.list_cases()
        except BackendError:
            logger.exception("get_my_case_status failed")
            raise ToolError(
                "The case information could not be loaded right now. Apologize and offer to try again."
            ) from None

        if not cases:
            return "No case is registered for this inmate. Say so and offer to call a staff member."

        open_cases = [c for c in cases if c["case_status"] == OPEN_CASE]
        await data.ui.case_status(open_cases or cases[:1])
        if not open_cases:
            latest = cases[0]
            return (
                "The inmate has no open case. The most recent one:\n"
                + "\n".join(_case_lines(latest))
                + "\nSay there is no open case and give the status of this one briefly."
            )
        text = "\n\n".join("\n".join(_case_lines(c)) for c in open_cases)
        return (
            f"Open cases ({len(open_cases)}), most recently updated first:\n\n{text}\n\n"
            "Answer only what the inmate asked, from this data, in short sentences."
        )

    @function_tool(flags=ToolFlag.IGNORE_ON_ENTER)
    async def start_complaint(
        self, context: RunContext[SessionData], first_statement: str | None = None
    ):
        """Starts a complaint and hands the inmate to the complaint service,
        when they say a right is not being respected or they want to complain.

        Args:
            first_statement: What the inmate said about the problem, in their own words, if they described it.
        """
        from complaint_agent import ComplaintAgent

        context.userdata.start_new_draft(first_statement)
        logger.info("[FLOW] InfoAgent -> ComplaintAgent")
        return ComplaintAgent(data=context.userdata, chat_ctx=self.handoff_ctx())
