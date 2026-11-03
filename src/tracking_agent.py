"""Tracking agent — follow-up on the inmate's own earlier complaints and
requests (visit, transfer, lawyer, release, clemency). Everything it says
comes from the backend, which already filters to prisoner-visible data.
"""

from __future__ import annotations

import logging
import textwrap
from typing import Any

from livekit.agents import ChatContext, RunContext, ToolError, function_tool
from livekit.agents.llm.tool_context import ToolFlag

from backend_client import BackendError, BackendNotFoundError
from base_agent import HalaAgent, MainMenuMixin, StaffHandoffMixin
from session_data import SessionData

logger = logging.getLogger("hala-agent")

CLOSED_COMPLAINT = "مغلقة"
PENDING_REQUEST = "قيد الدراسة"

_MAX_CLOSED_COMPLAINTS = 4
_MAX_REQUESTS = 8


def _complaint_line(c: dict[str, Any]) -> str:
    return (
        f"- complaint_id {c['complaint_id']}: {c.get('subcategory_name_ar') or c['complaint_subcategory']} — "
        f"{c['complaint_summary']} — submitted {c['submission_date']} — status {c['complaint_status']} — "
        f"last update {c['last_update_date']}"
    )


def _request_line(r: dict[str, Any]) -> str:
    line = (
        f"- request_id {r['request_id']}: {r['request_type']} — {r['request_summary']} — "
        f"submitted {r['submission_date']} — status {r['current_status']}"
    )
    if r.get("decision_date"):
        line += f" — decided {r['decision_date']}"
    if r.get("decision_reason"):
        line += f" — reason: {r['decision_reason']}"
    return line


def _split(complaints: list[dict[str, Any]], requests: list[dict[str, Any]]):
    open_c = [c for c in complaints if c["complaint_status"] != CLOSED_COMPLAINT]
    closed_c = [c for c in complaints if c["complaint_status"] == CLOSED_COMPLAINT][:_MAX_CLOSED_COMPLAINTS]
    return open_c, closed_c, requests[:_MAX_REQUESTS]


def _items_text(complaints: list[dict[str, Any]], requests: list[dict[str, Any]]) -> str:
    open_c, closed_c, recent_r = _split(complaints, requests)
    parts = [
        f"Open complaints ({len(open_c)}):",
        *([_complaint_line(c) for c in open_c] or ["- none"]),
        f"Most recent closed complaints (showing {len(closed_c)} of {len(complaints) - len(open_c)}):",
        *([_complaint_line(c) for c in closed_c] or ["- none"]),
        f"Most recent requests (showing {len(recent_r)} of {len(requests)}):",
        *([_request_line(r) for r in recent_r] or ["- none"]),
    ]
    return "\n".join(parts)


def _overview_ar(complaints: list[dict[str, Any]], requests: list[dict[str, Any]]) -> str:
    """Short spoken overview built in Python (no LLM call)."""
    open_c = [c for c in complaints if c["complaint_status"] != CLOSED_COMPLAINT]
    pending_r = [r for r in requests if r["current_status"] == PENDING_REQUEST]
    if not complaints and not requests:
        return "ما لقيت لك شكاوى ولا طلبات مسجّلة. تبي تقدّم شكوى جديدة؟"
    if open_c:
        text = "عندك " + _count_ar(len(open_c), "شكوى مفتوحة", "شكويين مفتوحتين", "شكاوى مفتوحة")
    else:
        text = "ما عندك شكاوى مفتوحة حاليًا"
    if pending_r:
        text += "، و" + ("" if open_c else "عندك ") + _count_ar(
            len(pending_r), "طلب قيد الدراسة", "طلبين قيد الدراسة", "طلبات قيد الدراسة"
        )
    return text + ". فيه شكوى أو طلب معيّن تبي تسأل عنه؟"


_COUNT_WORDS_AR = {3: "ثلاث", 4: "أربع", 5: "خمس", 6: "ست", 7: "سبع", 8: "ثمان", 9: "تسع", 10: "عشر"}


def _count_ar(n: int, singular: str, dual: str, plural: str) -> str:
    if n == 1:
        return singular
    if n == 2:
        return dual
    return f"{_COUNT_WORDS_AR.get(n, str(n))} {plural}"


class TrackingAgent(StaffHandoffMixin, MainMenuMixin, HalaAgent):
    stage = "tracking"

    def __init__(
        self,
        *,
        data: SessionData,
        about: str | None = None,
        chat_ctx: ChatContext | None = None,
    ):
        self._about = about
        super().__init__(
            data=data,
            chat_ctx=chat_ctx,
            role_instructions=textwrap.dedent("""\
                # Your role
                You follow up the inmate's earlier complaints and requests and
                tell them where each one stands.

                # How to do it
                - The inmate will not know reference numbers. Work out which
                  item they mean from how they describe it (its subject, its
                  kind, roughly when), using the list of their items. If two
                  or more could match, ask one short question to tell them
                  apart.
                - For a complaint, get its details with the complaint-status
                  tool before answering. For a request, the list already has
                  its status, decision date and reason.
                - Say, in this order: what it is about, its current status,
                  the latest update, then what happens next. Two or three
                  short sentences.
                - If a request was refused, give the stated reason exactly as
                  recorded, do not soften it and do not argue with it. Then
                  tell them they can file a complaint about it if they think
                  it is unjustified.
                - If they want to complain about a refused or delayed
                  request, hand them to the complaint service with that
                  request.
                - If there is no update, say so plainly with the date of the
                  last update.

                # Rules
                - Only say what the tools returned. Never guess a status, a
                  date or a reason, and never promise an outcome.
                - Do not read out the whole list; mention at most three items
                  at a time.
            """),
        )

    async def enter(self) -> None:
        data = self.session.userdata
        try:
            complaints = await data.backend.list_complaints()
            requests = await data.backend.list_requests()
        except BackendError:
            logger.exception("tracking: loading the inmate's items failed")
            await self.session.say(
                "المعذرة، ما قدرت أوصل لشكاواك وطلباتك الحين. تبي أطلب لك موظف يساعدك؟"
            )
            return

        open_c, closed_c, recent_r = _split(complaints, requests)
        await data.ui.tracking_list(open_c + closed_c, recent_r)
        items = _items_text(complaints, requests)

        if self._about:
            await self.session.generate_reply(
                instructions=(
                    f"The inmate asked about: {self._about}\n\nTheir items:\n{items}\n\n"
                    "Find the item they mean. If it is a complaint, call the "
                    "complaint-status tool for it and then answer. If it is a request, "
                    "answer from the list. If you cannot tell which one they mean, ask "
                    "one short question."
                )
            )
            return

        # Put the list into the conversation so later turns can refer to it,
        # then speak a ready-made overview without an LLM call.
        chat_ctx = self.chat_ctx.copy()
        chat_ctx.add_message(role="system", content=f"The inmate's items (from the backend):\n{items}")
        await self.update_chat_ctx(chat_ctx)
        await self.session.say(_overview_ar(complaints, requests))

    @function_tool()
    async def list_my_items(self, context: RunContext[SessionData]) -> str:
        """Returns the inmate's complaints and requests with their current
        status. Use it if you need the list again or it may have changed."""
        data = context.userdata
        try:
            complaints = await data.backend.list_complaints()
            requests = await data.backend.list_requests()
        except BackendError:
            logger.exception("list_my_items failed")
            raise ToolError(
                "The items could not be loaded right now. Apologize and offer to call a staff member."
            ) from None
        open_c, closed_c, recent_r = _split(complaints, requests)
        await data.ui.tracking_list(open_c + closed_c, recent_r)
        return _items_text(complaints, requests)

    @function_tool()
    async def get_complaint_status(self, context: RunContext[SessionData], complaint_id: str) -> str:
        """Returns the full status and timeline of one of the inmate's complaints.

        Args:
            complaint_id: The complaint_id exactly as it appears in the inmate's items list.
        """
        data = context.userdata
        try:
            c = await data.backend.get_complaint(complaint_id.strip())
        except BackendNotFoundError:
            raise ToolError(
                "No complaint with that id belongs to this inmate. Use an id from the items list."
            ) from None
        except BackendError:
            logger.exception("get_complaint_status failed")
            raise ToolError(
                "The complaint could not be loaded right now. Apologize and offer to try again."
            ) from None

        await data.ui.tracking_detail("complaint", c)
        last = c["timeline"][-1] if c["timeline"] else None
        lines = [
            f"Complaint {c['complaint_id']} ({c.get('subcategory_name_ar')}):",
            f"- about: {c['complaint_summary']}",
            f"- submitted: {c['submission_date']}",
            f"- status: {c['complaint_status']}",
            f"- last update: {c['last_update_date']}",
        ]
        if c.get("is_confidential") != "نعم":
            lines.append(f"- handled by: {c['handling_authority']}")
        if last:
            lines.append(f"- latest step: {last['description']}")
        if c.get("response_to_prisoner"):
            lines.append(f"- reply to the inmate: {c['response_to_prisoner']}")
        if c.get("next_action") and c["next_action"] != "لا يوجد":
            lines.append(f"- what happens next: {c['next_action']}")
        if c.get("next_action_due_date"):
            lines.append(f"- expected by: {c['next_action_due_date']}")
        lines.append(
            "Tell the inmate in this order: what it is about, the status, the latest "
            "step or reply, then what happens next. Short natural sentences, no labels."
        )
        return "\n".join(lines)

    @function_tool()
    async def get_request_status(self, context: RunContext[SessionData], request_id: str) -> str:
        """Returns the status and decision of one of the inmate's requests.

        Args:
            request_id: The request_id exactly as it appears in the inmate's items list.
        """
        data = context.userdata
        try:
            r = await data.backend.get_request(request_id.strip())
        except BackendNotFoundError:
            raise ToolError(
                "No request with that id belongs to this inmate. Use an id from the items list."
            ) from None
        except BackendError:
            logger.exception("get_request_status failed")
            raise ToolError(
                "The request could not be loaded right now. Apologize and offer to try again."
            ) from None

        await data.ui.tracking_detail("request", r)
        return (
            _request_line(r) + "\n"
            "Tell the inmate what the request was, its status, and the decision "
            "date and reason if there is one. If it was refused, mention they can "
            "file a complaint about it."
        )

    @function_tool(flags=ToolFlag.IGNORE_ON_ENTER)
    async def complain_about_request(self, context: RunContext[SessionData], request_id: str):
        """Starts a complaint about one of the inmate's requests (for example a
        refused visit) and hands them to the complaint service.

        Args:
            request_id: The request_id exactly as it appears in the inmate's items list.
        """
        from complaint_agent import ComplaintAgent

        data = context.userdata
        try:
            r = await data.backend.get_request(request_id.strip())
        except BackendNotFoundError:
            raise ToolError(
                "No request with that id belongs to this inmate. Use an id from the items list."
            ) from None
        except BackendError:
            logger.exception("complain_about_request failed")
            raise ToolError("The request could not be loaded right now. Apologize and offer to try again.") from None

        data.start_new_draft()
        data.draft.related_tracking_id = r["tracking_id"]
        data.draft.related_request_label = f"طلب {r['request_type']} رقم {r['request_id']}"
        logger.info("[FLOW] TrackingAgent -> ComplaintAgent (about request %s)", r["request_id"])
        return ComplaintAgent(data=data, chat_ctx=self.handoff_ctx())
