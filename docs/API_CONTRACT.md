# Hala voice agent: backend contract

This is everything the voice agent needs from the backend. Implement these endpoints, set `BACKEND_BASE_URL` in the agent's `.env.local` to your server, and the agent works against real data with no code change.

A working reference implementation is in `mock_backend/` (FastAPI, reads the POC Excel files). Run it and open `http://localhost:8000/docs` for the live OpenAPI schema and to try each call. `tests/test_mock_backend.py` holds the behaviour checks; they are a good acceptance suite for your implementation.

## How the pieces connect

```
Kiosk frontend ──(1) POST /api/voice/session──► Backend
      │                                            │ creates a LiveKit token that
      │◄──────── server_url + token ───────────────┘ dispatches the agent
      │
      └──(2) joins the LiveKit room ◄──► Voice agent ──(3) HTTP calls below──► Backend
```

1. The kiosk authenticates the inmate (fingerprint or ID), then asks the backend for a voice session.
2. The kiosk joins the LiveKit room with the returned token. LiveKit starts the agent in that room.
3. During the conversation the agent calls the backend over HTTP.

The agent never asks the inmate who they are. It trusts the `prisoner_id` in the dispatch metadata, and every call it makes is for that one inmate.

## General rules

- JSON, UTF-8. Field names follow the POC Excel column names.
- Dates are ISO strings: `YYYY-MM-DD`, or `YYYY-MM-DDTHH:MM` where a time exists.
- Yes/no columns stay as the Arabic strings `نعم` / `لا` in reads, as in the Excel data. Flags the agent sends or acts on (`is_urgent`, `is_confidential`, `urgent`, `confirmed_by_prisoner`, `default_urgent`, `is_confidential_default`, `has_article`) are JSON booleans.
- Errors: `404` when a record does not exist **or belongs to another inmate** (do not distinguish the two), `422` for an invalid body, `5xx` otherwise. The agent treats any non-2xx as "could not do it" and tells the inmate so.
- Auth: if `BACKEND_API_KEY` is set on the agent, it sends `Authorization: Bearer <key>` on every call. The mock does not check it.
- The agent's timeout is 6 seconds per call (`config.yaml`, `backend.timeout_s`). Lookups happen while the inmate is waiting for a spoken reply, so aim for well under one second.

## Visibility rules the backend must enforce

The agent reads out whatever these endpoints return. Filtering is the backend's job:

- Complaint timeline: only actions with `visible_to_prisoner = نعم`, and only the prisoner-facing description.
- Case procedures: only rows with `disclosable_to_prisoner = نعم`; never `internal_next_step`.
- Never return `action_description_internal`, `ai_confidence`, `classification_source`, or the national ID.
- For confidential complaints (`is_confidential = نعم`), do not put the handling unit's name in prisoner-facing timeline text.

## Endpoints

### 1. `POST /api/voice/session`

Called by the kiosk frontend, not by the agent.

Request:
```json
{ "prisoner_id": "PR-0001", "input_mode": "voice" }
```
`input_mode` is `"voice"` (default) or `"text"` (text-only: no mic, no spoken replies).

Response `200`:
```json
{
  "server_url": "wss://<project>.livekit.cloud",
  "room_name": "hala-PR-0001-3f9a1c2e",
  "participant_identity": "kiosk-PR-0001",
  "participant_token": "<JWT>"
}
```

The token must:
- grant `roomJoin` for a new, unique room;
- carry a room configuration with one agent dispatch: `agent_name = "hala-agent"` and `metadata` set to the JSON string `{"prisoner_id": "...", "input_mode": "..."}`.

See `create_voice_session` in `mock_backend/app.py` for the exact LiveKit server SDK calls. `503` if LiveKit credentials are missing.

### 2. `GET /api/prisoners/{prisoner_id}/profile`

Called once at session start.
```json
{
  "prisoner_id": "PR-0001",
  "full_name_ar": "ناصر سعد فهد القحطاني",
  "first_name": "ناصر",
  "facility_name": "دار التوقيف بالرياض",
  "cell_block": "عنبر 3 - غرفة 17",
  "preferred_language": "العربية",
  "gender": "ذكر", "nationality": "سعودي", "detention_type": "توقيف احتياطي",
  "currently_has_lawyer": "نعم", "weekly_visit_allowance": 1,
  "current_incarceration_status": "موقوف"
}
```
The agent uses `first_name` (greeting), and `facility_name` + `cell_block` (default incident location). The rest is informational.

### 3. `GET /api/complaint-taxonomy`

Called once at session start. The agent classifies complaints into these codes.
```json
{ "items": [
  { "subcategory_code": "LIV-06", "category_code": "LIV",
    "category_name_ar": "شكوى معيشية", "subcategory_name_ar": "نقص الملابس",
    "default_urgent": false, "is_confidential_default": false, "sla_days": 5 }
] }
```

### 4. `GET /api/prisoners/{prisoner_id}/complaints`

Newest first.
```json
{ "items": [
  { "complaint_id": "C26-00025", "complaint_category": "LIV", "complaint_subcategory": "LIV-07",
    "category_name_ar": "شكوى معيشية", "subcategory_name_ar": "المشاكل أو الاعتداءات بين النزلاء داخل الزنزانة",
    "complaint_title": null, "complaint_summary": "يفيد النزيل بتعرضه لتهديد متكرر…",
    "complaint_status": "بانتظار معلومات", "submission_date": "2026-09-27", "last_update_date": "2026-10-02",
    "handling_authority": "إدارة الأمن والسلامة - دار التوقيف بالرياض",
    "is_urgent": "نعم", "is_confidential": "لا", "related_tracking_id": null }
] }
```
The agent treats `complaint_status = مغلقة` as closed and everything else as open.

### 5. `GET /api/prisoners/{prisoner_id}/complaints/{complaint_id}`

Everything in the list item, plus:
```json
{
  "original_statement": "…", "input_channel": "صوت",
  "incident_date": null, "incident_location": null, "incident_details": null,
  "related_type": null, "related_name": null, "expected_action": "…",
  "response_to_prisoner": null, "response_date": null, "closed_date": null, "closure_reason": null,
  "current_action": "طلب معلومات", "next_action": "استكمال الدراسة بعد ورود المعلومات",
  "next_action_due_date": "2025-09-07",
  "timeline": [
    { "action_date": "2025-08-23T17:12", "action_type": "استلام",
      "description": "تم استلام شكواك وتسجيلها.", "next_action": "إحالة الشكوى للجهة المختصة" }
  ]
}
```
`timeline` is oldest first. The agent reads the last entry as "the latest step".

### 6. `POST /api/complaints`

Called once per complaint, only after the inmate has heard or read the summary and approved it. Returns `201`.

Request (only the first four are required):
```json
{
  "prisoner_id": "PR-0001",
  "complaint_subcategory": "LIV-06",
  "complaint_summary": "يفيد النزيل بنقص الملابس المصروفة له منذ أسبوعين.",
  "confirmed_by_prisoner": true,
  "complaint_title": "نقص الملابس",
  "original_statement": "ما عندي ملابس كافية من أسبوعين",
  "input_channel": "صوت",
  "incident_date": "2026-09-24",
  "incident_date_text": "قبل أسبوعين",
  "incident_location": "دار التوقيف بالرياض - عنبر 3 - غرفة 17",
  "incident_details": "…",
  "related_type": null, "related_name": null,
  "has_witnesses": null, "witnesses_info": null, "has_supporting_documents": null,
  "expected_action": null, "additional_notes": null,
  "related_tracking_id": null, "related_case_id": null,
  "is_urgent": false,
  "classification_source": "المساعد الذكي",
  "ai_confidence": 0.91
}
```
Notes:
- `incident_date` is always `YYYY-MM-DD` (Gregorian, never in the future), or the literal `"غير معروفة"` when the inmate does not know. The agent works the date out from what the inmate said; for something ongoing it is the start date. Reject anything else with `422`.
- `incident_date_text` is how the inmate actually said it (`"قبل أسبوعين"`), kept for reference. It may be `null`.
- `expected_action` is always asked for; it is `"غير معروفة"` if the inmate has no specific request.
- `related_case_id` is set for case complaints (SYS) when the agent could tell which case is meant: automatically when the inmate has one open case, otherwise the one the inmate chose. Route by it when present.
- For complaints about what a person did (ABU, LIV-07, PRV-05) the agent always asks who, where, and whether there were witnesses, so `related_name`, `incident_location` and `has_witnesses` (or `witnesses_info`) are filled; `related_name` may be `"غير معروفة"`.
- `input_channel` is `صوت` or `نص`.
- `has_witnesses` / `has_supporting_documents` are `نعم`, `لا` or `null` (never asked).
- `classification_source` is `المساعد الذكي`, or `صححها النزيل` when the inmate corrected the type during review.
- `ai_confidence` is `null` when the two classifiers disagreed or the inmate corrected the type.
- `related_tracking_id` links to one of the inmate's own requests (`07_all_requests_tracking.tracking_id`).

Response:
```json
{
  "complaint_id": "C26-00521", "complaint_status": "محالة", "submission_date": "2026-10-07",
  "handling_authority": "إدارة دار التوقيف بالرياض", "sla_days": 5,
  "is_urgent": false, "is_confidential": false,
  "category_name_ar": "شكوى معيشية", "subcategory_name_ar": "نقص الملابس"
}
```
The agent reads `complaint_id` and `sla_days` to the inmate, and `handling_authority` unless `is_confidential` is true.

Backend responsibilities on create (see `Store.create_complaint`):
- Reject with `422` if `confirmed_by_prisoner` is not `true`, the subcategory is unknown, the summary is empty, or `related_tracking_id` is not one of this inmate's requests.
- Apply the routing table: resolve the handling authority (case authority for SYS, the inmate's facility for LIV/PRV-03, fixed units otherwise), `sla_days`, and `is_urgent = request.is_urgent OR default_urgent`, `is_confidential = is_confidential_default`.
- Record the first two actions (`استلام`, `إحالة`) so the complaint shows a timeline immediately.

The agent guards against double submission within a session, but the endpoint is not idempotent. Add an idempotency key if you need protection against client retries.

### 7. `GET /api/prisoners/{prisoner_id}/requests`

Visit, transfer, lawyer, release and clemency requests, newest first.
```json
{ "items": [
  { "tracking_id": "TRK-00018", "request_id": "REL-26-00003", "request_type": "إفراج",
    "request_summary": "طلب إفراج بكفالة في القضية رقم …", "submission_date": "2026-09-18",
    "current_status": "مقبول", "decision_date": "2026-09-21",
    "decision_reason": "الموافقة على الإفراج بكفالة حضورية" }
] }
```
`request_type` is one of `زيارة`, `نقل`, `تعيين محامٍ`, `إفراج`, `استرحام`. `current_status` is one of `مقبول`, `مرفوض`, `قيد الدراسة`.

### 8. `GET /api/prisoners/{prisoner_id}/requests/{id}`

One request, same shape. `{id}` may be either the `request_id` or the `tracking_id`.

### 9. `GET /api/prisoners/{prisoner_id}/cases`

Open cases first, then most recently updated.
```json
{ "items": [
  { "case_id": "CASE-0002", "case_number": "1447/ر/173810",
    "charge_description": "…", "case_status": "مفتوحة", "current_stage": "المحاكمة",
    "current_authority": "المحكمة الجزائية بالرياض",
    "filing_date": "2025-07-19", "last_update_date": "2026-09-10", "next_session_date": "2026-11-11",
    "judgment_date": null, "objection_deadline": null,
    "release_order_date": null, "release_order_status": null,
    "current_procedure": {
      "current_procedure": "الجلسة الأولى", "last_registered_action": "عُقدت الجلسة الأولى",
      "prisoner_facing_next_step": "جلسة سماع البينات",
      "last_update_date": "2026-09-10", "expected_completion_date": "2026-11-11" }
  }
] }
```
`current_procedure` is the latest disclosable procedure of the case, or `null`.

### 10. `GET /api/rights/search?q=<text>&limit=3`

Keyword search over the rights guide, most relevant first, empty list if nothing matches.
```json
{ "items": [
  { "topic_id": "health", "title": "الرعاية الصحية", "summary": "فحوص دورية وعلاج…",
    "keywords": ["علاج", "طبيب"],
    "article": { "lead": "يوفّر النظام رعاية صحية كاملة للنزيل…", "bullets": ["…"] },
    "source": "نظام السجن والتوقيف ولائحته التنفيذية" }
] }
```
`article` is `null` when a topic has no approved detailed text. The agent then says only the title and summary and refers the inmate to staff. It never answers rights questions from its own knowledge, so answer quality depends entirely on this content.

**Content gap:** the mock has a full article for one topic only (`health`); the other nine have a title and one line, taken from the Demo6 screens. Approved text for all ten is needed from the client.

### 11. `GET /api/rights/topics`

```json
{ "items": [ { "topic_id": "health", "title": "الرعاية الصحية", "summary": "…", "has_article": true } ] }
```
`GET /api/rights/topics/{topic_id}` returns one full topic (same shape as a search item). The agent does not call it today; it is there for the kiosk screens.

### 12. `POST /api/staff-assistance`

Called when the inmate asks for a person, or immediately when they report danger. Returns `201`.
```json
{ "prisoner_id": "PR-0001", "reason": "تهديد من نزيل آخر في الغرفة", "urgent": true }
```
```json
{ "assistance_id": "AST-00001", "status": "بانتظار الموظف", "urgent": true, "estimated_wait_minutes": 1 }
```
`urgent: true` means a threat to someone's safety or health right now. It should page the duty officer, not sit in a queue. If this call fails, the agent tells the inmate to call the nearest staff member directly and logs at CRITICAL.

### 13. `POST /api/staff-assistance/{assistance_id}/notes`

Extra detail the inmate adds while waiting. Returns `201`.
```json
{ "prisoner_id": "PR-0001", "note": "في العنبر الثالث عند الباب" }
```

## Agent → kiosk screen events

The agent tells the frontend what to show by publishing JSON on the LiveKit data topic `hala.ui` (reliable). Each message is `{"type": "...", "payload": {...}}`. `mock_backend/static/test.html` renders all of them and is the reference consumer.

| `type` | `payload` | Show |
|---|---|---|
| `session_started` | `inmate`, `input_mode` | session is live |
| `agent_changed` | `agent`: `concierge` / `complaint` / `review` / `tracking` / `info` / `staff` | which service screen is active |
| `draft_updated` | `stage`: `collecting` / `review` / `cancelled`; `fields[]`: `num`, `key`, `label`, `value`, `status` (`ok` / `ask` / `empty`) | complaint fields (Demo6 screens `cc030`, `s10442`) |
| `complaint_submitted` | the `POST /api/complaints` response + `fields[]` | confirmation and reference number (`cc050`) |
| `tracking_list` | `complaints[]`, `requests[]` | follow-up list (`s10813`) |
| `tracking_detail` | `kind`: `complaint` / `request`; `item` | status and timeline (`s10920`, `s11165`) |
| `rights_results` | `query`, `topics[]` | rights article or results (`s21574`, `s21639`) |
| `case_status` | `cases[]` | case status |
| `staff_requested` | the `POST /api/staff-assistance` response + `reason` | staff called (`s17882`) |
| `session_ended` | none | the inmate is done; close the session |
| `backend_call` | `method`, `path`, `status`, `ms` | debug only |
| `error` | `message` | the agent could not start |

Other things the frontend gets from LiveKit directly:
- **Transcripts** of both sides: text streams on topic `lk.transcription`.
- **Typed input**: send the inmate's text on topic `lk.chat`; it is handled as a normal turn.
- **Agent state** (`listening` / `thinking` / `speaking`): the agent participant's `lk.agent.state` attribute.
- **Switch voice ↔ text-only mid-session**: RPC to the agent participant, method `hala.set_mode`, payload `{"input_mode": "voice" | "text"}`.

## Not part of the contract

`/api/mock/*` and `/` (the test page) exist only in the mock.
