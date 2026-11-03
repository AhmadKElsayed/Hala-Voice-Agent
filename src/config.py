"""Loads config.yaml (project root) once and exposes the model/service
settings as module-level constants, so agent.py / agent_tools.py /
staff_agent.py all read model names from one place instead of hardcoding
them or scattering them across os.environ.get() calls.

A few env vars override the file for quick ops tuning — see the header of
config.yaml. This module reads os.environ at import time, so .env.local must
be loaded BEFORE it is imported (agent.py does that).
"""

import os
from pathlib import Path

import yaml

_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"

with _CONFIG_PATH.open(encoding="utf-8") as f:
    _RAW = yaml.safe_load(f)

AGENT_NAME: str = os.environ.get("HALA_AGENT_NAME", _RAW["agent"]["name"])
DEFAULT_PRISONER_ID: str = os.environ.get(
    "HALA_DEFAULT_PRISONER_ID", _RAW["agent"]["default_prisoner_id"]
)

BACKEND_BASE_URL: str = os.environ.get("BACKEND_BASE_URL", _RAW["backend"]["base_url"])
BACKEND_TIMEOUT_S: float = float(_RAW["backend"]["timeout_s"])
BACKEND_API_KEY: str | None = os.environ.get("BACKEND_API_KEY") or None

LLM_MODEL: str = os.environ.get("HALA_LLM_MODEL", _RAW["llm"]["model"])
LLM_TEMPERATURE: float = float(_RAW["llm"]["temperature"])

STT_PROVIDER: str = _RAW["stt"]["provider"]
STT_LANGUAGE: str = _RAW["stt"]["language"]
STT_INCLUDE_PARTIALS: bool = _RAW["stt"]["include_partials"]

TTS_PROVIDER: str = os.environ.get("HALA_TTS_PROVIDER", _RAW["tts"]["provider"])
TTS_MODEL: str = _RAW["tts"]["main"]["model"]
TTS_VOICE: str = _RAW["tts"]["main"]["voice"]
TTS_INSTRUCTIONS: str = _RAW["tts"]["main"]["instructions"]

URGENT_TTS_MODEL: str = _RAW["tts"]["urgent"]["model"]
URGENT_TTS_VOICE: str = _RAW["tts"]["urgent"]["voice"]
URGENT_TTS_INSTRUCTIONS: str = _RAW["tts"]["urgent"]["instructions"]

CARTESIA_TTS_MODEL: str = _RAW["tts"]["cartesia"]["model"]
CARTESIA_TTS_VOICE: str = _RAW["tts"]["cartesia"]["voice"]
CARTESIA_TTS_LANGUAGE: str = _RAW["tts"]["cartesia"]["language"]

TTS_RETRY_MAX_RETRY: int = _RAW["tts_retry"]["max_retry"]
TTS_RETRY_BASE_INTERVAL_S: float = _RAW["tts_retry"]["base_interval_s"]

NOISE_CANCELLATION_ENABLED: bool = _RAW["noise_cancellation"].get("enabled", True)
NOISE_CANCELLATION_MODEL: str = _RAW["noise_cancellation"]["model"]

EXTRACT_MODEL: str = os.environ.get("HALA_EXTRACT_MODEL", _RAW["extraction"]["model"])
EXTRACT_TIMEOUT_S: float = float(
    os.environ.get("HALA_EXTRACT_TIMEOUT", _RAW["extraction"]["timeout_s"])
)

CLASSIFY_MODEL: str = _RAW["classification"]["model"]
CLASSIFY_TIMEOUT_S: float = float(_RAW["classification"]["timeout_s"])
CLASSIFY_MIN_CONFIDENCE: float = float(_RAW["classification"]["min_confidence"])
CLASSIFY_RETRY_MAX_RETRY: int = _RAW["classification"]["retry"]["max_retry"]
CLASSIFY_RETRY_BASE_INTERVAL_S: float = _RAW["classification"]["retry"]["base_interval_s"]
