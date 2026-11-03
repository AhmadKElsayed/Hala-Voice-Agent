"""Helpers shared by more than one agent: converting the conversation to
text, the transcript safety-net extraction, the second-opinion complaint
classifier, and small formatting helpers for spoken output.

This file has no Agent class defined in it.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import date
from typing import Any

from livekit.agents import ChatContext, RunContext

import config
from session_data import WEEKDAYS_EN, SessionData, normalize_incident_date

logger = logging.getLogger("hala-agent")

# Map for converting Arabic-Indic and Persian digits to Latin digits
_DIGIT_MAP = {ord(a): str(i) for i, a in enumerate("٠١٢٣٤٥٦٧٨٩")}
_DIGIT_MAP.update({ord(a): str(i) for i, a in enumerate("۰۱۲۳۴۵۶۷۸۹")})


def build_llm(temperature: float | None = None):
    """The conversation model, built the same way for the live agent and the
    tests.

    parallel_tool_calls is off on purpose: with it on, the model sometimes
    calls two handoff tools in one turn (for example "start a complaint" and
    "answer a question" for a sentence that could be either), and the session
    then refuses both handoffs and the inmate is left where they were.
    """
    from livekit.plugins import openai

    return openai.LLM(
        model=config.LLM_MODEL,
        temperature=config.LLM_TEMPERATURE if temperature is None else temperature,
        parallel_tool_calls=False,
    )


def normalize_digits(raw: str) -> str:
    """'٢٠٢٦' -> '2026' (leaves every non-digit character untouched)."""
    return raw.translate(_DIGIT_MAP)


def reference_readout_hint(reference: str, input_mode: str) -> str:
    """How the model should present a complaint/request number."""
    if input_mode == "text":
        return f"Write the number exactly as '{reference}', once."
    return (
        f"The number is '{reference}'. Say it slowly: the Latin letter(s) by "
        "their name, then every digit one by one as an Arabic word, skipping "
        "the dash. Then repeat it once."
    )


def conversation_text(chat_ctx: ChatContext) -> str:
    """Converts a chat context (the correct source is context.session.history,
    which holds the full conversation across all agents) into readable
    Inmate/Assistant lines for the extraction and classification models."""
    lines: list[str] = []
    for item in getattr(chat_ctx, "items", []):
        role = getattr(item, "role", None)
        if role not in ("user", "assistant"):
            continue
        text = getattr(item, "text_content", None)
        if not text:
            continue
        lines.append(f"{'Inmate' if role == 'user' else 'Assistant'}: {text}")
    return "\n".join(lines)


# How to choose the complaint type. Shared by the conversation model
# (complaint_agent.py) and the second-opinion classifier below, so both read
# the same definitions. The examples are the client's own, from the agreed
# classification table.
CLASSIFICATION_GUIDE = """How to choose:
- SYS (شكوى نظامية): about the inmate's legal case — investigation
  procedures, the trial, case status, a release order, carrying out a
  release order, objecting to a judgment, related judicial procedures.
  Examples: "صدر لي أمر إفراج ولم يتم تنفيذه" is SYS-05.
  "أريد معرفة إجراء الاعتراض على الحكم" is SYS-06.
- LIV (شكوى معيشية): living conditions in the facility — the environment
  inside the place of detention, food, clothing, basic needs, the canteen,
  a shortage of clothing, and problems or assaults between inmates inside
  the cell.
  Examples: "لدي نقص في الملابس" is LIV-06.
  "تعرضت للضرب داخل الزنزانة من نزيل آخر" is LIV-07.
- PRV (شكوى خاصة): the inmate's private matters — updating identity and
  personal data, getting a lawyer, contact with their family, personal
  affairs, and assault by one individual on another.
  Examples: "أرغب في التواصل مع محامٍ" is PRV-02.
  "أريد التواصل مع ذويي" is PRV-03.
- ABU (شكوى إساءة استعمال السلطة): a government official or employee
  insulted, assaulted or hit the inmate, mistreated them, or did anything
  that is an abuse of their powers.
  Examples: "تعرضت للإهانة من أحد المسؤولين" is ABU-01.
  "تعرضت للضرب من مسؤول حكومي" is ABU-02.
- Who did it decides the type of an assault or insult: another inmate in
  the cell is LIV-07, a government official or employee is ABU, any other
  individual is PRV-05.
"""


# ----------------------------------------------------------
# OpenAI client (extraction + classification)
# ----------------------------------------------------------

_openai_client = None


def _get_openai_client():
    """One client per process (creating it on every call adds needless latency)."""
    global _openai_client
    if _openai_client is None:
        from openai import AsyncOpenAI

        _openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"), max_retries=0)
    return _openai_client


async def _json_completion(
    *, model: str, system: str, user: str, schema_name: str, schema: dict[str, Any], timeout_s: float
) -> dict[str, Any]:
    client = _get_openai_client()
    resp = await asyncio.wait_for(
        client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": schema_name, "strict": True, "schema": schema},
            },
            temperature=0,
        ),
        timeout=timeout_s,
    )
    return json.loads(resp.choices[0].message.content or "{}")


def _nullable(json_type: str) -> dict[str, Any]:
    return {"type": [json_type, "null"]}


# ----------------------------------------------------------
# Extracting complaint details from the conversation (safety net before review)
# ----------------------------------------------------------
#
# Why does this exist at all?
# The conversation model asks a question, hears the answer, and sometimes
# moves on to the next question without calling the save tool — so the
# information is silently lost. This re-reads the whole conversation once,
# when moving to review, and fills in only the fields that are still empty.
# It never overwrites a value that was already saved.

_EXTRACT_SYSTEM = """\
You extract data from a conversation between a smart assistant on a
detention-facility kiosk and an inmate filing a complaint. The conversation
is in Arabic (Saudi dialect). Return JSON only, relying exclusively on what
the inmate said.

Today is {weekday}, {today} (Gregorian, year-month-day).

All text values must be written in clear formal Arabic, never in English.

Strict rules:
- If a field was not mentioned in the conversation, return null. Never guess
  and never invent anything.
- title: a short Arabic noun phrase of three to six words naming the
  complaint, e.g. "نقص الملابس والاحتياجات الأساسية".
- summary: one to three sentences describing the complaint in the third
  person, starting with "يفيد النزيل" or "يطلب النزيل" or "يعترض النزيل", and
  including the concrete facts the inmate gave (what, which items, how
  often, who).
- incident_date: the date it happened or started, as YYYY-MM-DD with Latin
  digits, worked out from today's date and what the inmate said ("قبل
  أسبوعين" is fourteen days before today). If the inmate said they do not
  know or do not remember, return "غير معروفة". If no time was mentioned at
  all, return null. Never return words in this field.
- incident_date_text: how the inmate said the time, in their own words (e.g.
  "قبل أسبوعين").
- incident_location: where it happened, only if the inmate said where. If
  they said it was in their own cell or room, return "الزنزانة".
- related_type: one of "نزيل", "مسؤول حكومي", "جهة", "فرد آخر" if the complaint
  is about a specific person or entity, else null.
- related_name: the name or description of that person or entity.
- has_witnesses: true if they said there are witnesses, false if they said
  no, null if never discussed.
- witnesses_info: who the witnesses are, if mentioned.
- has_documents: true if they said they have papers or evidence, false if
  they said no, null if never discussed.
- expected_action: what the inmate wants done about it, if they said it.
"""

_EXTRACT_SCHEMA = {
    "type": "object",
    "properties": {
        "title": _nullable("string"),
        "summary": _nullable("string"),
        "incident_date": _nullable("string"),
        "incident_date_text": _nullable("string"),
        "incident_location": _nullable("string"),
        "related_type": _nullable("string"),
        "related_name": _nullable("string"),
        "has_witnesses": _nullable("boolean"),
        "witnesses_info": _nullable("string"),
        "has_documents": _nullable("boolean"),
        "expected_action": _nullable("string"),
    },
    "required": [
        "title", "summary", "incident_date", "incident_date_text",
        "incident_location", "related_type",
        "related_name", "has_witnesses", "witnesses_info", "has_documents",
        "expected_action",
    ],
    "additionalProperties": False,
}

# Fields the flush is allowed to fill in. The classification is deliberately
# not here — check_classification below owns it.
_FLUSHABLE_FIELDS = tuple(_EXTRACT_SCHEMA["properties"])


async def extract_draft_fields(transcript: str) -> dict[str, Any]:
    return await _json_completion(
        model=config.EXTRACT_MODEL,
        system=_EXTRACT_SYSTEM.format(
            today=date.today().isoformat(), weekday=WEEKDAYS_EN[date.today().weekday()]
        ),
        user=transcript,
        schema_name="complaint_fields",
        schema=_EXTRACT_SCHEMA,
        timeout_s=config.EXTRACT_TIMEOUT_S,
    )


async def flush_draft_from_transcript(context: RunContext[SessionData]) -> list[str]:
    """Reads the full conversation and fills in any draft field still empty.
    Returns the names of the fields that were recovered.

    Never fatal: any failure (timeout, missing key, malformed JSON) is logged
    and the conversation continues normally.
    """
    draft = context.userdata.draft

    transcript = conversation_text(context.session.history)
    if not transcript:
        return []

    try:
        extracted = await extract_draft_fields(transcript)
    except asyncio.TimeoutError:
        logger.warning("[FLUSH] timed out (%.1fs) — skipping extraction", config.EXTRACT_TIMEOUT_S)
        return []
    except Exception:
        logger.exception("[FLUSH] extracting details from the conversation failed (non-fatal)")
        return []

    recovered: list[str] = []
    for key in _FLUSHABLE_FIELDS:
        current = getattr(draft, key)
        # incident_location is pre-filled with the inmate's own cell, so a
        # different place named in the conversation is allowed to replace it
        prefilled = key == "incident_location" and current == context.userdata.inmate.default_location
        if current not in (None, "") and not prefilled:
            continue
        val = extracted.get(key)
        if val in (None, ""):
            continue
        if key == "incident_date":
            # same rule as the save tool: a real date or "unknown", never words
            try:
                val = normalize_incident_date(val)
            except ValueError:
                logger.warning("[FLUSH] ignoring unusable incident_date %r", val)
                continue
        if key == "incident_location":
            draft.location_confirmed = True
            if val == "الزنزانة":
                val = context.userdata.inmate.default_location or val
        setattr(draft, key, val)
        recovered.append(key)
        logger.info("[FLUSH] %s recovered from the conversation -> %r", key, val)

    if not recovered:
        logger.info("[FLUSH] no missing fields were recovered from the conversation")
    return recovered


# ----------------------------------------------------------
# Second-opinion classification
# ----------------------------------------------------------

_CLASSIFY_SYSTEM = """\
You classify an inmate's complaint, written in Arabic, into exactly one
subcategory from the list below. Return JSON only.

Subcategories (code: main category / subcategory):
{taxonomy}

{guide}- confidence is your probability, from 0 to 1, that the code is correct.
"""


def _classify_schema(codes: list[str]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "subcategory_code": {"type": "string", "enum": codes},
            "confidence": {"type": "number"},
        },
        "required": ["subcategory_code", "confidence"],
        "additionalProperties": False,
    }


async def classify_complaint(text: str, data: SessionData) -> dict[str, Any] | None:
    """Returns {"subcategory_code", "confidence"}, or None if it could not be
    classified after the configured retries."""
    system = _CLASSIFY_SYSTEM.format(taxonomy=data.taxonomy_prompt(), guide=CLASSIFICATION_GUIDE)
    schema = _classify_schema(list(data.taxonomy))
    for attempt in range(config.CLASSIFY_RETRY_MAX_RETRY + 1):
        try:
            return await _json_completion(
                model=config.CLASSIFY_MODEL,
                system=system,
                user=text,
                schema_name="complaint_classification",
                schema=schema,
                timeout_s=config.CLASSIFY_TIMEOUT_S,
            )
        except Exception as e:
            logger.warning("[CLASSIFY] attempt %d failed: %r", attempt + 1, e)
            if attempt < config.CLASSIFY_RETRY_MAX_RETRY:
                await asyncio.sleep(config.CLASSIFY_RETRY_BASE_INTERVAL_S * (2**attempt))
    return None


async def check_classification(data: SessionData) -> None:
    """Runs the second-opinion classifier on the draft and records the result.

    - No subcategory saved yet: adopt the classifier's answer.
    - Both agree: store the confidence.
    - They disagree, or the confidence is low: keep what the conversation
      produced but flag the classification as "needs confirmation" so the
      review screen shows it with a question mark.

    Never fatal — on failure the draft is left exactly as it was.
    """
    draft = data.draft
    text = "\n".join(t for t in (draft.original_statement, draft.summary) if t)
    if not text or not data.taxonomy:
        return

    result = await classify_complaint(text, data)
    if result is None:
        return

    code, confidence = result["subcategory_code"], round(float(result["confidence"]), 2)
    logger.info("[CLASSIFY] draft=%s classifier=%s confidence=%.2f", draft.subcategory, code, confidence)

    if not draft.subcategory:
        draft.subcategory = code
        draft.ai_confidence = confidence
    elif draft.subcategory == code:
        draft.ai_confidence = confidence
    else:
        draft.ai_confidence = None
        draft.needs_confirmation.update({"category", "subcategory"})
        return

    if confidence < config.CLASSIFY_MIN_CONFIDENCE:
        draft.needs_confirmation.update({"category", "subcategory"})
