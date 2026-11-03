"""The ONLY module that talks to the backend.

Every call the agent makes goes through BackendClient, against the HTTP
contract in docs/API_CONTRACT.md. Today that contract is served by
mock_backend/; pointing BACKEND_BASE_URL at the real backend is the whole
integration.

A BackendClient instance is bound to one prisoner_id at construction: the
agents never pass an inmate id around, so a tool call cannot be talked into
reading another inmate's data.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

logger = logging.getLogger("hala-agent")

CallObserver = Callable[[dict[str, Any]], Awaitable[None]]


class BackendError(Exception):
    """The backend could not be reached or answered with an error."""

    def __init__(self, message: str, status_code: int | None = None):
        super().__init__(message)
        self.status_code = status_code


class BackendNotFoundError(BackendError):
    """404 — the record does not exist or does not belong to this inmate."""


class BackendClient:
    def __init__(
        self,
        prisoner_id: str,
        base_url: str,
        *,
        timeout_s: float = 6.0,
        api_key: str | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        self.prisoner_id = prisoner_id
        headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
        self._http = httpx.AsyncClient(
            base_url=base_url, timeout=timeout_s, headers=headers, transport=transport
        )
        # Set by the entrypoint so the test page can show every backend call
        self.on_call: CallObserver | None = None

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        started = time.perf_counter()
        status: int | None = None
        try:
            resp = await self._http.request(method, path, **kwargs)
            status = resp.status_code
            if status == 404:
                raise BackendNotFoundError(f"{method} {path} -> 404", status)
            if status >= 400:
                raise BackendError(f"{method} {path} -> {status}: {resp.text[:300]}", status)
            return resp.json()
        except httpx.HTTPError as e:
            raise BackendError(f"{method} {path} failed: {e!r}") from e
        finally:
            ms = round((time.perf_counter() - started) * 1000)
            logger.info("[BACKEND] %s %s -> %s (%d ms)", method, path, status, ms)
            if self.on_call is not None:
                try:
                    await self.on_call({"method": method, "path": path, "status": status, "ms": ms})
                except Exception:
                    logger.debug("backend call observer failed", exc_info=True)

    @property
    def _me(self) -> str:
        return f"/api/prisoners/{self.prisoner_id}"

    # ---------------- reads ---------------------------------------------------

    async def get_profile(self) -> dict[str, Any]:
        return await self._request("GET", f"{self._me}/profile")

    async def get_taxonomy(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/api/complaint-taxonomy"))["items"]

    async def list_complaints(self) -> list[dict[str, Any]]:
        return (await self._request("GET", f"{self._me}/complaints"))["items"]

    async def get_complaint(self, complaint_id: str) -> dict[str, Any]:
        return await self._request("GET", f"{self._me}/complaints/{complaint_id}")

    async def list_requests(self) -> list[dict[str, Any]]:
        return (await self._request("GET", f"{self._me}/requests"))["items"]

    async def get_request(self, request_id: str) -> dict[str, Any]:
        return await self._request("GET", f"{self._me}/requests/{request_id}")

    async def list_cases(self) -> list[dict[str, Any]]:
        return (await self._request("GET", f"{self._me}/cases"))["items"]

    async def list_rights_topics(self) -> list[dict[str, Any]]:
        return (await self._request("GET", "/api/rights/topics"))["items"]

    async def search_rights(self, query: str, limit: int = 3) -> list[dict[str, Any]]:
        return (
            await self._request("GET", "/api/rights/search", params={"q": query, "limit": limit})
        )["items"]

    # ---------------- writes --------------------------------------------------

    async def create_complaint(self, payload: dict[str, Any]) -> dict[str, Any]:
        # The id in the body always comes from the session, never from the model
        body = {**payload, "prisoner_id": self.prisoner_id}
        return await self._request("POST", "/api/complaints", json=body)

    async def request_staff(self, reason: str | None, urgent: bool) -> dict[str, Any]:
        body = {"prisoner_id": self.prisoner_id, "reason": reason, "urgent": urgent}
        return await self._request("POST", "/api/staff-assistance", json=body)

    async def add_staff_note(self, assistance_id: str, note: str) -> dict[str, Any]:
        body = {"prisoner_id": self.prisoner_id, "note": note}
        return await self._request("POST", f"/api/staff-assistance/{assistance_id}/notes", json=body)
