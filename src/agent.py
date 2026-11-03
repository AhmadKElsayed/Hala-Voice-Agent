"""Main entry point — assembles all the agents (Concierge/Complaint/Review/
Tracking/Info/Staff, each in its own file) and starts the session, opening
with ConciergeAgent.

Agent handoff map (each agent lives in its own file, see there for details):
    ConciergeAgent (concierge_agent.py)
        --start_complaint------> ComplaintAgent (complaint_agent.py)
        --follow_up_requests---> TrackingAgent (tracking_agent.py)
        --answer_question------> InfoAgent (info_agent.py)
        --request_staff_help---> StaffAgent (staff_agent.py)
    ComplaintAgent
        --proceed_to_review----> ReviewAgent (review_agent.py)
    ReviewAgent
        --cancel_complaint-----> ConciergeAgent
    TrackingAgent
        --complain_about_request--> ComplaintAgent
    InfoAgent
        --start_complaint------> ComplaintAgent
    Every agent except the concierge: --back_to_main_menu--> ConciergeAgent
    Every agent except the staff agent: --request_staff_help--> StaffAgent

Shared state (SessionData/Inmate/ComplaintDraft) lives in session_data.py,
shared speaking rules in voice_rules.py, shared helpers (transcript flush,
classification check) in agent_tools.py, backend access in
backend_client.py, kiosk-screen events in ui_events.py, and model/service
config in config.py.

The same session serves spoken and typed turns. In text-only mode the mic
and the spoken replies are switched off and the speaking rules are swapped
for writing rules; nothing else changes.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv
from livekit import rtc
from livekit.agents import (
    AgentServer,
    AgentSession,
    APIConnectOptions,
    JobContext,
    TurnHandlingOptions,
    cli,
    inference,
    room_io,
)
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.plugins import ai_coustics, openai, silero, speechmatics  # noqa: F401

# Must run BEFORE the local imports below: config.py and agent_tools.py read
# os.environ at module scope, so anything imported before the .env.local
# values are in the environment captures empty values for the whole process
# lifetime.
#
# The path is resolved off this file rather than passed as the bare relative
# ".env.local", which only finds the file when the process happens to be
# launched from the project root.
load_dotenv(Path(__file__).resolve().parent.parent / ".env.local")

import config  # noqa: E402
from agent_tools import build_llm  # noqa: E402
from backend_client import BackendClient, BackendError  # noqa: E402
from base_agent import HalaAgent  # noqa: E402
from complaint_agent import ComplaintAgent  # noqa: E402
from concierge_agent import ConciergeAgent  # noqa: E402
from info_agent import InfoAgent  # noqa: E402
from log_setup import setup_run_logging  # noqa: E402
from review_agent import ReviewAgent  # noqa: E402
from session_data import Inmate, InputMode, SessionData  # noqa: E402
from staff_agent import StaffAgent  # noqa: E402
from tracking_agent import TrackingAgent  # noqa: E402
from ui_events import UIEvents  # noqa: E402

# Importing all six here (even though only ConciergeAgent is called directly
# below) collects them in one visible place, and makes sure every agent file
# loads correctly at startup (instead of discovering an import error in some
# agent only after a conversation actually reaches it).
_AGENTS = (ConciergeAgent, ComplaintAgent, ReviewAgent, TrackingAgent, InfoAgent, StaffAgent)

# silero itself is unused directly, but speechmatics.STT lazily imports it for
# its VAD, and livekit-agents only allows plugin registration on the main
# thread. Importing it here (module load, main thread) registers it during
# worker startup so it's already registered by the time a job needs it.

logger = logging.getLogger("hala-agent")

SET_MODE_RPC = "hala.set_mode"


@dataclass(frozen=True)
class _ExponentialAPIConnectOptions(APIConnectOptions):
    """APIConnectOptions whose retry wait grows exponentially instead of the
    base class's fixed interval, so a rate-limited TTS gets room to recover
    within a reasonable number of attempts.
    """

    def _interval_for_retry(self, num_retries: int) -> float:
        return self.retry_interval * (2**num_retries)


_TTS_CONN_OPTIONS = _ExponentialAPIConnectOptions(
    max_retry=config.TTS_RETRY_MAX_RETRY,
    retry_interval=config.TTS_RETRY_BASE_INTERVAL_S,
)


def _build_tts():
    if config.TTS_PROVIDER == "cartesia":
        return inference.TTS(
            model=config.CARTESIA_TTS_MODEL,
            voice=config.CARTESIA_TTS_VOICE,
            language=config.CARTESIA_TTS_LANGUAGE,
        )
    return openai.TTS(
        model=config.TTS_MODEL,
        voice=config.TTS_VOICE,
        instructions=config.TTS_INSTRUCTIONS,
    )


def _read_dispatch(ctx: JobContext) -> tuple[str, InputMode]:
    """The kiosk authenticates the inmate before the session; the backend
    passes who it is in the agent-dispatch metadata (see POST
    /api/voice/session in docs/API_CONTRACT.md). Console mode has no
    metadata, so it falls back to the configured test inmate."""
    meta: dict = {}
    raw = getattr(ctx.job, "metadata", "") or ""
    if raw:
        try:
            meta = json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("dispatch metadata is not JSON: %r", raw)
    prisoner_id = meta.get("prisoner_id") or config.DEFAULT_PRISONER_ID
    input_mode: InputMode = "text" if meta.get("input_mode") == "text" else "voice"
    return prisoner_id, input_mode


def _apply_input_mode(session: AgentSession, mode: InputMode) -> None:
    voice = mode == "voice"
    session.input.set_audio_enabled(voice)
    session.output.set_audio_enabled(voice)


server = AgentServer()


@server.rtc_session(agent_name=config.AGENT_NAME)
async def entrypoint(ctx: JobContext):
    # Must be called first so it captures every log line from this run from the start
    log_path = setup_run_logging(ctx)
    logger.info("=== start logs %s ===", log_path)

    ctx.log_context_fields = {"room": ctx.room.name}

    prisoner_id, input_mode = _read_dispatch(ctx)
    logger.info("session for prisoner=%s input_mode=%s", prisoner_id, input_mode)

    # Connect first so the room is live while session.start() spins up its
    # (slower) STT/TTS/VAD/turn-detector models, instead of leaving the room
    # unconnected during that setup.
    await ctx.connect()

    ui = UIEvents(ctx.room)
    backend = BackendClient(
        prisoner_id,
        config.BACKEND_BASE_URL,
        timeout_s=config.BACKEND_TIMEOUT_S,
        api_key=config.BACKEND_API_KEY,
    )
    backend.on_call = ui.backend_call
    ctx.add_shutdown_callback(backend.aclose)

    try:
        profile = await backend.get_profile()
        taxonomy = await backend.get_taxonomy()
    except BackendError as e:
        logger.critical("cannot start: backend unavailable at %s (%s)", config.BACKEND_BASE_URL, e)
        await ui.publish("error", {"message": "backend_unavailable"})
        return

    data = SessionData(
        backend=backend,
        ui=ui,
        inmate=Inmate(
            prisoner_id=prisoner_id,
            full_name=profile.get("full_name_ar") or "",
            first_name=profile.get("first_name") or "",
            facility_name=profile.get("facility_name") or "",
            cell_block=profile.get("cell_block") or "",
        ),
        taxonomy={t["subcategory_code"]: t for t in taxonomy},
        input_mode=input_mode,
    )

    session = AgentSession[SessionData](
        userdata=data,
        llm=build_llm(),
        stt=speechmatics.STT(
            language=config.STT_LANGUAGE,
            include_partials=config.STT_INCLUDE_PARTIALS,
        ),
        tts=_build_tts(),
        turn_handling=TurnHandlingOptions(
            turn_detection=inference.TurnDetector(),
            # Preemptive response generation while waiting for the user's turn to end
            preemptive_generation={"enabled": True},
        ),
        conn_options=SessionConnectOptions(tts_conn_options=_TTS_CONN_OPTIONS),
    )

    # Text-only sessions start with the mic and the spoken replies off. Set
    # before start() so the opening greeting is not synthesized either.
    _apply_input_mode(session, input_mode)

    async def _set_mode(rpc: rtc.RpcInvocationData) -> str:
        """Lets the kiosk flip between voice and text-only mid-session.
        Payload: {"input_mode": "voice" | "text"}."""
        try:
            mode: InputMode = "text" if json.loads(rpc.payload).get("input_mode") == "text" else "voice"
        except json.JSONDecodeError:
            mode = "voice"
        data.input_mode = mode
        _apply_input_mode(session, mode)
        agent = session.current_agent
        if isinstance(agent, HalaAgent):
            await agent.refresh_instructions()
        logger.info("[MODE] input mode switched to %s", mode)
        return json.dumps({"input_mode": mode})

    try:
        ctx.room.local_participant.register_rpc_method(SET_MODE_RPC, _set_mode)
    except Exception:
        # Console mode runs against a mock room with no RPC support
        logger.debug("RPC registration skipped", exc_info=True)

    await ui.session_started(
        {
            "prisoner_id": data.inmate.prisoner_id,
            "first_name": data.inmate.first_name,
            "full_name": data.inmate.full_name,
            "facility_name": data.inmate.facility_name,
            "cell_block": data.inmate.cell_block,
        },
        input_mode,
    )

    await session.start(
        agent=ConciergeAgent(data=data),
        room=ctx.room,
        room_options=room_io.RoomOptions(
            audio_input=room_io.AudioInputOptions(
                noise_cancellation=ai_coustics.audio_enhancement(
                    model=getattr(
                        ai_coustics.EnhancerModel, config.NOISE_CANCELLATION_MODEL
                    )
                )
                if config.NOISE_CANCELLATION_ENABLED
                else None,
            ),
        ),
    )


if __name__ == "__main__":
    cli.run_app(server)
