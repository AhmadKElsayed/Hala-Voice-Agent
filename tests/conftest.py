"""Shared fixtures.

The agent tests run against the real mock backend, in-process (httpx
ASGITransport — no server, no network), with a throwaway runtime folder so
complaints filed by a test never land in mock_backend/data/.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parent.parent / ".env.local")

# config.py and the OpenAI plugin only need a key to exist at import time;
# the tests that really call OpenAI are skipped unless a real one is set.
HAS_OPENAI_KEY = bool(os.environ.get("OPENAI_API_KEY"))
os.environ.setdefault("OPENAI_API_KEY", "sk-test-placeholder")

import mock_backend.app as mock_app  # noqa: E402
from backend_client import BackendClient  # noqa: E402
from mock_backend.store import Store  # noqa: E402
from session_data import Inmate, SessionData  # noqa: E402
from ui_events import UIEvents  # noqa: E402

PRISONER = "PR-0001"
OTHER_PRISONER = "PR-0002"


@pytest.fixture
def store(tmp_path, monkeypatch) -> Store:
    s = Store(runtime_dir=tmp_path)
    monkeypatch.setattr(mock_app, "store", s)
    return s


@pytest.fixture
async def backend(store):
    client = BackendClient(
        PRISONER, "http://mock", transport=httpx.ASGITransport(app=mock_app.app)
    )
    yield client
    await client.aclose()


@pytest.fixture
async def data(backend) -> SessionData:
    profile = await backend.get_profile()
    taxonomy = await backend.get_taxonomy()
    ui = UIEvents()
    # as in agent.py: every backend call is mirrored as a UI debug event
    backend.on_call = ui.backend_call
    return SessionData(
        backend=backend,
        ui=ui,
        inmate=Inmate(
            prisoner_id=PRISONER,
            full_name=profile["full_name_ar"],
            first_name=profile["first_name"],
            facility_name=profile["facility_name"],
            cell_block=profile["cell_block"],
        ),
        taxonomy={t["subcategory_code"]: t for t in taxonomy},
    )


class StubContext:
    """Stands in for RunContext when a tool is called directly in a test:
    the tools only use .userdata, .session.history and
    .disallow_interruptions()."""

    def __init__(self, data: SessionData, history_items: list | None = None):
        self.userdata = data
        self.session = SimpleNamespace(history=SimpleNamespace(items=history_items or []))

    def disallow_interruptions(self) -> None:
        pass


@pytest.fixture
def ctx(data) -> StubContext:
    return StubContext(data)


def user_said(text: str) -> SimpleNamespace:
    return SimpleNamespace(role="user", text_content=text)
