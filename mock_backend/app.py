"""Mock backend for the Hala voice agent — implements docs/API_CONTRACT.md on
top of the POC Excel files (see store.py).

Run from the voice-agent folder:

    uv run python -m uvicorn mock_backend.app:app --reload --port 8000

Then open http://localhost:8000 for the test page.

Everything under /api/ is the contract the real backend has to implement.
Everything under /api/mock/ exists only for the test page and is NOT part of
the contract.
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from pathlib import Path
from typing import Any, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from mock_backend.store import NotFoundError, Store, ValidationError

_ROOT = Path(__file__).resolve().parent
load_dotenv(_ROOT.parent / ".env.local")

logger = logging.getLogger("hala-mock-backend")

AGENT_NAME = os.environ.get("HALA_AGENT_NAME", "hala-agent")

app = FastAPI(title="Hala mock backend", version="0.1.0")
store = Store()


# ---------------------------------------------------------------- models ----


class VoiceSessionRequest(BaseModel):
    prisoner_id: str
    input_mode: Literal["voice", "text"] = "voice"


class VoiceSessionResponse(BaseModel):
    server_url: str
    room_name: str
    participant_identity: str
    participant_token: str


class ComplaintCreate(BaseModel):
    prisoner_id: str
    complaint_subcategory: str = Field(description="One of the subcategory codes from /api/complaint-taxonomy, e.g. LIV-06")
    complaint_summary: str
    complaint_title: str | None = None
    confirmed_by_prisoner: bool = Field(description="Must be true: the inmate heard/read the summary and approved it")
    original_statement: str | None = None
    input_channel: Literal["صوت", "نص"] = "صوت"
    incident_date: str | None = Field(
        default=None, description='YYYY-MM-DD, or "غير معروفة" when the inmate does not know'
    )
    incident_date_text: str | None = Field(
        default=None, description="How the inmate said the time, in their own words"
    )
    incident_location: str | None = None
    incident_details: str | None = None
    related_type: str | None = None
    related_name: str | None = None
    has_witnesses: str | None = None
    witnesses_info: str | None = None
    has_supporting_documents: str | None = None
    expected_action: str | None = None
    additional_notes: str | None = None
    related_tracking_id: str | None = None
    related_case_id: str | None = None
    is_urgent: bool = False
    classification_source: str | None = None
    ai_confidence: float | None = None


class StaffAssistanceCreate(BaseModel):
    prisoner_id: str
    reason: str | None = None
    urgent: bool = False


class StaffNoteCreate(BaseModel):
    prisoner_id: str
    note: str


def _guard(fn, *args, **kwargs) -> Any:
    try:
        return fn(*args, **kwargs)
    except NotFoundError as e:
        raise HTTPException(status_code=404, detail=str(e)) from e
    except ValidationError as e:
        raise HTTPException(status_code=422, detail=str(e)) from e


# ------------------------------------------------------- session / token ----


@app.post("/api/voice/session", response_model=VoiceSessionResponse)
def create_voice_session(body: VoiceSessionRequest) -> VoiceSessionResponse:
    """Called by the kiosk frontend AFTER the inmate is authenticated. Creates
    a one-off LiveKit room, dispatches the agent into it with the inmate's id
    in the dispatch metadata, and returns a join token for the kiosk."""
    from livekit import api

    _guard(store.profile, body.prisoner_id)

    url = os.environ.get("LIVEKIT_URL")
    key = os.environ.get("LIVEKIT_API_KEY")
    secret = os.environ.get("LIVEKIT_API_SECRET")
    if not (url and key and secret):
        raise HTTPException(
            status_code=503,
            detail="LIVEKIT_URL / LIVEKIT_API_KEY / LIVEKIT_API_SECRET are not set in .env.local",
        )

    room_name = f"hala-{body.prisoner_id}-{uuid.uuid4().hex[:8]}"
    identity = f"kiosk-{body.prisoner_id}"
    metadata = json.dumps({"prisoner_id": body.prisoner_id, "input_mode": body.input_mode})

    token = (
        api.AccessToken(key, secret)
        .with_identity(identity)
        .with_name(body.prisoner_id)
        .with_grants(api.VideoGrants(room_join=True, room=room_name))
        .with_room_config(
            api.RoomConfiguration(
                agents=[api.RoomAgentDispatch(agent_name=AGENT_NAME, metadata=metadata)]
            )
        )
        .to_jwt()
    )
    return VoiceSessionResponse(
        server_url=url, room_name=room_name, participant_identity=identity, participant_token=token
    )


# ----------------------------------------------------------------- reads ----


@app.get("/api/prisoners/{prisoner_id}/profile")
def get_profile(prisoner_id: str) -> dict[str, Any]:
    return _guard(store.profile, prisoner_id)


@app.get("/api/complaint-taxonomy")
def get_taxonomy() -> dict[str, Any]:
    return {"items": store.taxonomy()}


@app.get("/api/prisoners/{prisoner_id}/complaints")
def list_complaints(prisoner_id: str) -> dict[str, Any]:
    return {"items": _guard(store.list_complaints, prisoner_id)}


@app.get("/api/prisoners/{prisoner_id}/complaints/{complaint_id}")
def get_complaint(prisoner_id: str, complaint_id: str) -> dict[str, Any]:
    return _guard(store.get_complaint, prisoner_id, complaint_id)


@app.get("/api/prisoners/{prisoner_id}/requests")
def list_requests(prisoner_id: str) -> dict[str, Any]:
    return {"items": _guard(store.list_requests, prisoner_id)}


@app.get("/api/prisoners/{prisoner_id}/requests/{request_id}")
def get_request(prisoner_id: str, request_id: str) -> dict[str, Any]:
    return _guard(store.get_request, prisoner_id, request_id)


@app.get("/api/prisoners/{prisoner_id}/cases")
def list_cases(prisoner_id: str) -> dict[str, Any]:
    return {"items": _guard(store.list_cases, prisoner_id)}


@app.get("/api/rights/topics")
def rights_topics() -> dict[str, Any]:
    return {"items": store.rights_topics()}


@app.get("/api/rights/topics/{topic_id}")
def rights_topic(topic_id: str) -> dict[str, Any]:
    return _guard(store.rights_topic, topic_id)


@app.get("/api/rights/search")
def rights_search(q: str, limit: int = 3) -> dict[str, Any]:
    return {"items": store.rights_search(q, limit=limit)}


# ---------------------------------------------------------------- writes ----


@app.post("/api/complaints", status_code=201)
def create_complaint(body: ComplaintCreate) -> dict[str, Any]:
    return _guard(store.create_complaint, body.model_dump())


@app.post("/api/staff-assistance", status_code=201)
def create_staff_assistance(body: StaffAssistanceCreate) -> dict[str, Any]:
    return _guard(store.create_staff_request, body.model_dump())


@app.post("/api/staff-assistance/{assistance_id}/notes", status_code=201)
def add_staff_note(assistance_id: str, body: StaffNoteCreate) -> dict[str, Any]:
    return _guard(store.add_staff_note, body.prisoner_id, assistance_id, body.note)


# ------------------------------------------- mock-only (not in contract) ----


@app.get("/api/mock/prisoners")
def mock_prisoners() -> dict[str, Any]:
    return {"items": store.list_prisoners()}


@app.get("/api/mock/staff-requests")
def mock_staff_requests() -> dict[str, Any]:
    return {"items": store.staff_requests}


@app.get("/")
def test_page() -> FileResponse:
    return FileResponse(_ROOT / "static" / "test.html")


app.mount("/static", StaticFiles(directory=_ROOT / "static"), name="static")
