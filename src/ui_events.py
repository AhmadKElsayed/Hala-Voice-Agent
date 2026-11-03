"""Agent -> kiosk screen events.

The agent does not render anything; it tells the frontend what to show by
publishing small JSON messages on one LiveKit data topic ("hala.ui"). The
test page listens to them today, and the real kiosk frontend can listen to
exactly the same messages (they are listed in docs/API_CONTRACT.md).

    {"type": "<event type>", "payload": {...}}

Publishing is always best-effort: a UI event that fails to send must never
break the conversation, so every error is swallowed and logged.
"""

from __future__ import annotations

import json
import logging
from typing import Any

logger = logging.getLogger("hala-agent")

UI_TOPIC = "hala.ui"


class UIEvents:
    """Publishes UI events to the room. With room=None (console mode, tests)
    events are only recorded in `sent`, which is what the tests assert on."""

    def __init__(self, room: Any | None = None):
        self._room = room
        self.sent: list[dict[str, Any]] = []

    async def publish(self, event_type: str, payload: dict[str, Any] | None = None) -> None:
        event = {"type": event_type, "payload": payload or {}}
        self.sent.append(event)
        if self._room is None:
            return
        try:
            await self._room.local_participant.publish_data(
                json.dumps(event, ensure_ascii=False).encode("utf-8"),
                reliable=True,
                topic=UI_TOPIC,
            )
        except Exception:
            logger.debug("failed to publish UI event %s", event_type, exc_info=True)

    # ---- typed helpers, one per event type -----------------------------------

    async def session_started(self, inmate: dict[str, Any], input_mode: str) -> None:
        await self.publish("session_started", {"inmate": inmate, "input_mode": input_mode})

    async def agent_changed(self, agent: str) -> None:
        """Which stage the conversation is in: concierge | complaint | review |
        tracking | info | staff."""
        await self.publish("agent_changed", {"agent": agent})

    async def backend_call(self, call: dict[str, Any]) -> None:
        await self.publish("backend_call", call)

    async def draft_updated(self, fields: list[dict[str, Any]], stage: str) -> None:
        """stage: 'collecting' while questions are being asked, 'review' once
        the summary has been read back."""
        await self.publish("draft_updated", {"stage": stage, "fields": fields})

    async def complaint_submitted(self, result: dict[str, Any]) -> None:
        await self.publish("complaint_submitted", result)

    async def tracking_list(self, complaints: list[dict[str, Any]], requests: list[dict[str, Any]]) -> None:
        await self.publish("tracking_list", {"complaints": complaints, "requests": requests})

    async def tracking_detail(self, kind: str, item: dict[str, Any]) -> None:
        """kind: 'complaint' | 'request'."""
        await self.publish("tracking_detail", {"kind": kind, "item": item})

    async def rights_results(self, query: str, topics: list[dict[str, Any]]) -> None:
        await self.publish("rights_results", {"query": query, "topics": topics})

    async def case_status(self, cases: list[dict[str, Any]]) -> None:
        await self.publish("case_status", {"cases": cases})

    async def staff_requested(self, result: dict[str, Any]) -> None:
        await self.publish("staff_requested", result)

    async def session_ended(self) -> None:
        await self.publish("session_ended", {})
