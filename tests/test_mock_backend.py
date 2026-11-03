"""The mock backend against docs/API_CONTRACT.md — these double as the
acceptance checks for the real backend's implementation of the contract."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

import mock_backend.app as mock_app
from conftest import OTHER_PRISONER, PRISONER

VALID_COMPLAINT = {
    "prisoner_id": PRISONER,
    "complaint_subcategory": "LIV-06",
    "complaint_summary": "يفيد النزيل بنقص الملابس المصروفة له منذ أسبوعين.",
    "confirmed_by_prisoner": True,
}


@pytest.fixture
def client(store) -> TestClient:
    return TestClient(mock_app.app)


def test_profile_has_no_national_id(client):
    body = client.get(f"/api/prisoners/{PRISONER}/profile").json()
    assert body["first_name"] == "ناصر"
    assert body["facility_name"]
    assert "national_id_or_iqama" not in body


def test_unknown_prisoner_is_404(client):
    assert client.get("/api/prisoners/PR-9999/profile").status_code == 404


def test_taxonomy_lists_all_routing_subcategories(client):
    items = client.get("/api/complaint-taxonomy").json()["items"]
    assert len(items) == 22
    assert {i["category_code"] for i in items} == {"SYS", "LIV", "PRV", "ABU"}
    abuse = next(i for i in items if i["subcategory_code"] == "ABU-02")
    assert abuse["is_confidential_default"] is True
    assert abuse["default_urgent"] is True


def test_complaints_are_scoped_to_the_prisoner(client):
    mine = client.get(f"/api/prisoners/{PRISONER}/complaints").json()["items"]
    assert mine
    cid = mine[0]["complaint_id"]
    assert client.get(f"/api/prisoners/{PRISONER}/complaints/{cid}").status_code == 200
    # the same complaint through another inmate's path must not be readable
    assert client.get(f"/api/prisoners/{OTHER_PRISONER}/complaints/{cid}").status_code == 404


def test_complaint_detail_hides_internal_fields_and_hidden_actions(client, store):
    hidden = next(a for a in store.actions if a["visible_to_prisoner"] == "لا")
    complaint = next(c for c in store.complaints if c["complaint_id"] == hidden["complaint_id"])
    body = client.get(
        f"/api/prisoners/{complaint['prisoner_id']}/complaints/{complaint['complaint_id']}"
    ).json()

    visible = [
        a for a in store.actions
        if a["complaint_id"] == complaint["complaint_id"] and a["visible_to_prisoner"] == "نعم"
    ]
    assert len(body["timeline"]) == len(visible)
    for step in body["timeline"]:
        assert "action_description_internal" not in step
    assert "ai_confidence" not in body
    assert "classification_source" not in body


def test_cases_only_expose_disclosable_procedures(client, store):
    hidden = next(p for p in store.procedures if p["disclosable_to_prisoner"] == "لا")
    cases = client.get(f"/api/prisoners/{hidden['prisoner_id']}/cases").json()["items"]
    case = next(c for c in cases if c["case_id"] == hidden["case_id"])
    proc = case["current_procedure"]
    assert "internal_next_step" not in (proc or {})
    # the hidden procedure row itself is never the one returned
    if proc:
        shown = [
            p for p in store.procedures
            if p["case_id"] == hidden["case_id"] and p["disclosable_to_prisoner"] == "نعم"
        ]
        assert proc["current_procedure"] in {p["current_procedure"] for p in shown}


def test_create_complaint_routes_and_appears_in_tracking(client):
    resp = client.post("/api/complaints", json=VALID_COMPLAINT)
    assert resp.status_code == 201
    created = resp.json()
    assert created["complaint_id"].startswith("C")
    assert created["handling_authority"] == "إدارة دار التوقيف بالرياض"
    assert created["sla_days"] == 5
    assert created["is_urgent"] is False

    detail = client.get(
        f"/api/prisoners/{PRISONER}/complaints/{created['complaint_id']}"
    ).json()
    assert detail["complaint_status"] == "محالة"
    assert [s["action_type"] for s in detail["timeline"]] == ["استلام", "إحالة"]
    listed = client.get(f"/api/prisoners/{PRISONER}/complaints").json()["items"]
    assert listed[0]["complaint_id"] == created["complaint_id"]


def test_abuse_complaint_is_urgent_confidential_and_authority_not_shown_in_timeline(client):
    body = {**VALID_COMPLAINT, "complaint_subcategory": "ABU-02"}
    created = client.post("/api/complaints", json=body).json()
    assert created["is_urgent"] is True
    assert created["is_confidential"] is True
    detail = client.get(
        f"/api/prisoners/{PRISONER}/complaints/{created['complaint_id']}"
    ).json()
    assert created["handling_authority"] not in detail["timeline"][-1]["description"]


def test_case_complaint_goes_to_the_authority_holding_the_case(client):
    body = {**VALID_COMPLAINT, "complaint_subcategory": "SYS-03"}
    created = client.post("/api/complaints", json=body).json()
    cases = client.get(f"/api/prisoners/{PRISONER}/cases").json()["items"]
    assert created["handling_authority"] == cases[0]["current_authority"]


@pytest.mark.parametrize(
    "patch",
    [
        {"confirmed_by_prisoner": False},
        {"complaint_subcategory": "XXX-99"},
        {"complaint_summary": "   "},
        {"related_tracking_id": "TRK-99999"},
        {"incident_date": "قبل أسبوعين"},
        {"incident_date": "24/09/2026"},
        {"incident_date": "2999-01-01"},
    ],
)
def test_create_complaint_rejects_bad_payloads(client, patch):
    resp = client.post("/api/complaints", json={**VALID_COMPLAINT, **patch})
    assert resp.status_code == 422


@pytest.mark.parametrize("value", ["2026-09-24", "غير معروفة", None])
def test_create_complaint_accepts_iso_or_unknown_incident_date(client, value):
    resp = client.post("/api/complaints", json={**VALID_COMPLAINT, "incident_date": value})
    assert resp.status_code == 201


def test_requests_lookup_by_request_or_tracking_id(client):
    items = client.get(f"/api/prisoners/{PRISONER}/requests").json()["items"]
    first = items[0]
    by_request = client.get(f"/api/prisoners/{PRISONER}/requests/{first['request_id']}").json()
    by_tracking = client.get(f"/api/prisoners/{PRISONER}/requests/{first['tracking_id']}").json()
    assert by_request == by_tracking
    assert (
        client.get(f"/api/prisoners/{OTHER_PRISONER}/requests/{first['request_id']}").status_code
        == 404
    )


def test_rights_search_finds_healthcare_article(client):
    items = client.get("/api/rights/search", params={"q": "أبي أعرف حقي في العلاج"}).json()["items"]
    assert items[0]["topic_id"] == "health"
    assert items[0]["article"]["lead"]
    assert client.get("/api/rights/search", params={"q": "كرة القدم"}).json()["items"] == []


def test_rights_topics_flag_which_have_an_article(client):
    items = client.get("/api/rights/topics").json()["items"]
    assert len(items) == 10
    assert [i["topic_id"] for i in items if i["has_article"]] == ["health"]


def test_staff_assistance_and_notes(client):
    created = client.post(
        "/api/staff-assistance", json={"prisoner_id": PRISONER, "reason": "تهديد", "urgent": True}
    ).json()
    assert created["urgent"] is True
    note = client.post(
        f"/api/staff-assistance/{created['assistance_id']}/notes",
        json={"prisoner_id": PRISONER, "note": "في العنبر"},
    )
    assert note.status_code == 201
    other = client.post(
        f"/api/staff-assistance/{created['assistance_id']}/notes",
        json={"prisoner_id": OTHER_PRISONER, "note": "x"},
    )
    assert other.status_code == 404


def test_voice_session_without_livekit_keys_is_503(client, monkeypatch):
    for key in ("LIVEKIT_URL", "LIVEKIT_API_KEY", "LIVEKIT_API_SECRET"):
        monkeypatch.delenv(key, raising=False)
    resp = client.post("/api/voice/session", json={"prisoner_id": PRISONER})
    assert resp.status_code == 503


def test_voice_session_token_carries_agent_dispatch(client, monkeypatch):
    import json

    from livekit import api

    monkeypatch.setenv("LIVEKIT_URL", "wss://example.livekit.cloud")
    monkeypatch.setenv("LIVEKIT_API_KEY", "APItestkey")
    monkeypatch.setenv("LIVEKIT_API_SECRET", "s" * 40)
    body = client.post(
        "/api/voice/session", json={"prisoner_id": PRISONER, "input_mode": "text"}
    ).json()
    assert body["server_url"] == "wss://example.livekit.cloud"

    claims = api.TokenVerifier("APItestkey", "s" * 40).verify(body["participant_token"])
    assert claims.video.room == body["room_name"]
    dispatch = claims.room_config.agents[0]
    assert dispatch.agent_name == mock_app.AGENT_NAME
    assert json.loads(dispatch.metadata) == {"prisoner_id": PRISONER, "input_mode": "text"}
