# Hala voice agent

Voice and text assistant for the Public Prosecution inmate-services kiosk (`PP_AI_Assistant_Demo6`), built on LiveKit Agents. This folder holds the agent, a mock backend that serves the POC Excel data, and a test page. It does not hold the real backend or the kiosk frontend.

The agent covers four services, in Saudi Arabic:

- **File a complaint**: the inmate describes the problem freely; the agent classifies it into one of the 22 routing subcategories, asks only for what is missing, reads it back, and files it after explicit approval.
- **Follow up** earlier complaints and requests (visit, transfer, lawyer, release, clemency).
- **Rights and case questions**, answered only from the rights guide and the inmate's disclosable case data.
- **Call a staff member**, immediately and as urgent when the inmate reports danger.

The inmate can speak or type in the same session, or use text-only mode with no audio.

## Run it

All commands run from this folder, using [`uv`](https://docs.astral.sh/uv/).

1. Install:
   ```bash
   uv sync
   ```
2. Copy `.env.example` to `.env.local` and fill in `LIVEKIT_URL`, `LIVEKIT_API_KEY`, `LIVEKIT_API_SECRET`, `OPENAI_API_KEY`, `SPEECHMATICS_API_KEY`.
3. Start the mock backend (terminal 1):
   ```bash
   uv run python -m uvicorn mock_backend.app:app --port 8000
   ```
4. Start the agent (terminal 2):
   ```bash
   uv run python src/agent.py dev
   ```
5. Open <http://localhost:8000>, pick an inmate, press «بدء الجلسة», and talk or type.

The left side of the test page is the conversation. The right side shows what the agent tells the kiosk to display, and under it a log of every backend call and handoff.

Quick check without the page (terminal only, as inmate `PR-0001`; the mock backend must be running):
```bash
uv run python src/agent.py console
```

## Things to try

As `PR-0001`:

- "ما صرفوا لي ملابس كافية من أسبوعين": expect a living-conditions complaint (clothing), one or two questions, a read-back, then a `C26-…` number after you approve.
- "وش صار على طلب الزيارة؟": expect the status of a visit request (this inmate has several, some refused with a reason); for a refused one, an offer to complain about it.
- "وش حقوقي في العلاج؟": expect an answer from the healthcare article.
- "متى جلستي الجاية؟": expect the next session date of the open case.
- "أبي أكلم موظف", and separately "فيه نزيل يهددني الحين": expect a staff request, urgent in the second case.
- Switch to «كتابة فقط» and file a complaint by typing: no audio, digits shown as digits.

Complaints filed through the agent are kept in `mock_backend/data/new_complaints.json`; delete that file to reset.

## Tests

```bash
uv run python -m pytest
```

- `tests/test_mock_backend.py`: the backend contract (scoping, visibility rules, routing).
- `tests/test_tools.py`: the agents' guards, called directly with no model: nothing filed without a complete draft, never filed twice, failures reported as not sent, no access to another inmate's records.
- `tests/test_agent_llm.py`: full conversations through the real model. Skipped unless a real `OPENAI_API_KEY` is in the environment.

## Layout

```
config.yaml              models, voices, timeouts
docs/API_CONTRACT.md     what the backend must implement (start here if you are the backend engineer)
src/
  agent.py               entrypoint: wires STT / LLM / TTS and starts the session
  base_agent.py          shared base class and the two common handoffs
  concierge_agent.py     greets and routes
  complaint_agent.py     complaint intake
  review_agent.py        read-back, corrections, submission
  tracking_agent.py      follow-up
  info_agent.py          rights and case questions
  staff_agent.py         human handoff
  session_data.py        shared session state and the complaint draft
  voice_rules.py         speaking rules (voice and text-only variants)
  agent_tools.py         transcript safety net and classification check
  backend_client.py      the only module that calls the backend
  ui_events.py           agent -> kiosk screen events
mock_backend/
  app.py, store.py       FastAPI mock of the contract, on the POC Excel files
  rights_guide.json      rights guide content
  static/test.html       test page
```

## Models

Set in `config.yaml`:

| Role | Default |
|---|---|
| Conversation | OpenAI `gpt-4.1` |
| Text-to-speech | OpenAI `gpt-4o-mini-tts`, voice `coral`, with Saudi-dialect style instructions |
| Extraction and classification check | OpenAI `gpt-4.1-mini` |
| Speech-to-text | Speechmatics, Arabic |
| Turn detection, noise cancellation | LiveKit turn detector, ai-coustics `QUAIL_VF_S` |

If the OpenAI voice does not sound right in Saudi Arabic, set `tts.provider: cartesia` (or `HALA_TTS_PROVIDER=cartesia`) to use Cartesia `sonic-3` through LiveKit Inference instead.

## Integrating the real backend

Implement the endpoints in `docs/API_CONTRACT.md`, then set `BACKEND_BASE_URL` in `.env.local`. `backend_client.py` is the only place the agent touches the backend.

## Known gaps

- **Rights content**: only the healthcare topic has a full article. The other nine topics have a title and one line, so the agent can only say that much about them. Client-approved text is needed.
- **Arabic only**: the kiosk offers other languages; the agent does not yet.
- **PRV-05** (assault by an individual) has no handling authority in the routing table yet; the mock passes the placeholder text through.
- **Session timer**: the kiosk's two-minute inactivity timer is a frontend concern and is not implemented here.
