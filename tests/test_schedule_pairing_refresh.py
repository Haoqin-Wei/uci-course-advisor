"""Server-owned pairing refresh survives schedule restore without stale card proof."""
import asyncio
from copy import deepcopy
from threading import Event

import pytest

from app.data import db, sessions, websoc_workflow
from app.recommendations.pairing import pairing_for_primary
from app.routers import chat
from tests.test_recommendation_real_sources import source_pipeline
from tests.test_websoc_pairing_producer import _parse


TERM = "2026 Fall"
COURSE = "SOCIOL 1"


def pending_session(envelope=None):
    sid = sessions.create_session("demo_001", title="Pairing refresh source test")
    sessions.update_session_state("demo_001", sid, {"pending_schedule": [
        {"course_id": COURSE, "term": TERM, "section": "B", "status": "pending"},
        {"course_id": COURSE, "term": TERM, "section": "B1", "status": "pending"},
    ]})
    if envelope:
        sessions.append_turn("demo_001", sid, "assistant", "Official source candidate", cards=[{
            "course_id": COURSE, "term": TERM, "title": "Introduction to Sociology",
            "primary_code": "69020", "section_source": "registrar_websoc",
            "sections": deepcopy(envelope["sections"]), "pairing": deepcopy(envelope["pairing"]),
            "pairing_source_proof": deepcopy(envelope.get("source_proof")),
        }])
    return sid


def selection(payload):
    return payload["schedule_validation"]["pairing"][0]["selection_verification"]


def evidence(sid):
    return sessions.get_session_state("demo_001", sid)["schedule_pairing_evidence"][TERM][COURSE]


def fail_lookup(*_args, **_kwargs):
    raise AssertionError("Unexpected source request")


def live_result(rows):
    return {"found": True, "source": "live_anteater_websoc", "is_live": True, "stale": False,
            "term": TERM, "course_id": COURSE, "retrieved_at": "2026-10-20T15:00:00Z",
            "sections": deepcopy(rows)}


def test_local_lookup_fetches_actual_official_pairing_once_then_restores_it(source_pipeline, app_client, monkeypatch):
    sid = pending_session()
    local = {"found": True, "source": "db", "term": TERM, "sections": _parse()["sections"]}
    monkeypatch.setattr(db, "get_sections", lambda *_: deepcopy(local))
    first = app_client.get("/api/schedule", params={"session_id": sid})
    assert first.status_code == 200
    assert selection(first.json())["status"] == "satisfied"
    saved = evidence(sid)
    assert saved["pairing"]["version"] == 2 and saved["generation"] == 1
    assert saved["pairing_source_proof"]["html_sha256"]
    assert pairing_for_primary(saved["pairing"], "69000")["requirement"] == "required"
    assert pairing_for_primary(saved["pairing"], "69000")["relationship"] == "unknown"
    posts = len([call for call in source_pipeline if call[0] == "POST"])
    assert posts == 1
    monkeypatch.setattr(db, "enrich_sections_pairing", fail_lookup)
    restored = app_client.get("/api/schedule", params={"session_id": sid}).json()
    assert selection(restored)["status"] == "satisfied"
    assert evidence(sid)["generation"] == 1


def test_server_card_full_contract_is_reused_without_another_pairing_lookup(source_pipeline, app_client, monkeypatch):
    envelope = db.get_sections(COURSE, TERM)
    sid = pending_session(envelope)
    monkeypatch.setattr(db, "get_sections", fail_lookup)
    monkeypatch.setattr(db, "enrich_sections_pairing", fail_lookup)
    payload = app_client.get("/api/schedule", params={"session_id": sid}).json()
    assert selection(payload)["status"] == "satisfied"
    pair = payload["schedule_validation"]["pairing"][0]["pairing"]
    assert set(pair["per_primary"]) == {"69000", "69020", "69040"}
    assert len([call for call in source_pipeline if call[0] == "POST"]) == 1


def test_explicit_refresh_updates_real_proof_preserves_live_rows_and_restores_latest(source_pipeline, app_client, monkeypatch):
    envelope = db.get_sections(COURSE, TERM)
    sid = pending_session(envelope)
    original_hash = envelope["source_proof"]["html_sha256"]
    original_session = websoc_workflow.requests.Session

    class RevisedSource(original_session):
        def post(self, *args, **kwargs):
            result = super().post(*args, **kwargs)
            assert b">69021</td>" in result.content
            result.content = result.content.replace(b">69021</td>", b">69031</td>")
            result.text = result.content.decode("ISO-8859-1")
            return result

    monkeypatch.setattr(websoc_workflow.requests, "Session", RevisedSource)
    rows = deepcopy(envelope["sections"])
    for row in rows:
        if row["section_code"] == "69020":
            row.update(start_time="15:00", end_time="16:20")
            # A source refresh updates the complete meeting snapshot too;
            # legacy flat fields cannot override a newer complete payload.
            row["meetings"][0].update(start_time="15:00", end_time="16:20")
        if row["section_code"] == "69021":
            row["section_code"] = "69031"
    live_calls = []

    def live(cid, term, **kwargs):
        live_calls.append((cid, term, kwargs))
        return live_result(rows)

    monkeypatch.setattr(db, "get_live_sections", live)
    response = app_client.post("/api/schedule/refresh", json={"session_id": sid})
    assert response.status_code == 200
    payload = response.json()
    assert payload["timed_out"] is False and selection(payload)["status"] == "satisfied"
    assert live_calls == [(COURSE, TERM, {"force_refresh": True, "request_timeout_s": 2.5})]
    saved = evidence(sid)
    assert saved["pairing_source_proof"]["html_sha256"] != original_hash
    b = pairing_for_primary(saved["pairing"], "69020")
    assert ["69031"] in [bundle["secondary_codes"] for bundle in b["allowed_bundles"]]
    assert ["69021"] not in [bundle["secondary_codes"] for bundle in b["allowed_bundles"]]
    assert selection(payload)["selected_secondary_codes"] == ["69031"]
    assert {event["start"] for event in payload["events"] if event["section_code"] == "69020"} == {"15:00"}
    monkeypatch.setattr(db, "get_sections", fail_lookup)
    monkeypatch.setattr(db, "enrich_sections_pairing", fail_lookup)
    restored = app_client.get("/api/schedule", params={"session_id": sid}).json()
    assert selection(restored) == selection(payload)
    assert restored["events"] == payload["events"]
    assert restored["schedule_validation"]["pairing"][0]["pairing"] == payload["schedule_validation"]["pairing"][0]["pairing"]


@pytest.mark.parametrize("live_available", [False, True])
def test_failed_pairing_refresh_supersedes_old_card_even_when_live_rows_are_missing(source_pipeline, app_client, monkeypatch, live_available):
    envelope = db.get_sections(COURSE, TERM)
    sid = pending_session(envelope)
    assert selection(app_client.get("/api/schedule", params={"session_id": sid}).json())["status"] == "satisfied"
    monkeypatch.setattr(db, "get_live_sections", lambda *_args, **_kwargs: live_result(envelope["sections"]) if live_available else None)

    def failed_pair(cid, term, response, **kwargs):
        assert kwargs == {"force_refresh": True, "request_timeout_s": 2.5}
        raise RuntimeError("source unavailable")

    monkeypatch.setattr(db, "enrich_sections_pairing", failed_pair)
    response = app_client.post("/api/schedule/refresh", json={"session_id": sid})
    assert response.status_code == 200
    payload = response.json()
    assert len(payload["pending_schedule"]) == 2
    assert selection(payload)["status"] == "unknown"
    assert payload["schedule_validation"]["valid"] is False
    saved = evidence(sid)
    assert saved["pairing"]["version"] == 2 and saved["pairing"]["per_primary"] == {}
    assert saved["pairing_lookup_status"] == "unavailable"
    assert "pairing_refresh_unavailable" in saved["pairing"]["evidence"]["reason_codes"]
    monkeypatch.setattr(db, "enrich_sections_pairing", fail_lookup)
    restored = app_client.get("/api/schedule", params={"session_id": sid}).json()
    assert selection(restored)["status"] == "unknown"
    # Extra client fields are not accepted as source evidence by mutations.
    mutated = app_client.post("/api/schedule/add", json={"session_id": sid, "course_id": COURSE,
        "term": TERM, "section": "B", "pairing": envelope["pairing"],
        "schedule_pairing_evidence": {TERM: {COURSE: {"pairing": envelope["pairing"]}}}}).json()
    assert selection(mutated)["status"] == "unknown"
    assert evidence(sid) == saved


def test_pairing_timeout_persists_unknown_with_shared_three_second_budget(source_pipeline, app_client, monkeypatch):
    envelope = db.get_sections(COURSE, TERM)
    sid = pending_session(envelope)
    release = Event()
    started = Event()

    def slow_pair(*_args, **kwargs):
        assert kwargs["request_timeout_s"] == 2.5
        started.set()
        release.wait(timeout=1)
        return {"pairing": envelope["pairing"]}

    real_wait = asyncio.wait

    async def shortened_wait(tasks, *, timeout):
        assert timeout == 3.0
        return await real_wait(tasks, timeout=0.03)

    monkeypatch.setattr(db, "enrich_sections_pairing", slow_pair)
    monkeypatch.setattr(db, "get_live_sections", lambda *_args, **_kwargs: live_result(envelope["sections"]))
    monkeypatch.setattr(chat.asyncio, "wait", shortened_wait)
    try:
        response = app_client.post("/api/schedule/refresh", json={"session_id": sid})
    finally:
        release.set()
    assert started.is_set() and response.status_code == 200
    assert response.json()["timed_out"] is True
    assert selection(response.json())["status"] == "unknown"
    saved = evidence(sid)
    assert saved["pairing_lookup_status"] == "timeout"
    assert "pairing_refresh_timeout" in saved["pairing"]["evidence"]["reason_codes"]
    monkeypatch.setattr(db, "enrich_sections_pairing", fail_lookup)
    assert selection(app_client.get("/api/schedule", params={"session_id": sid}).json())["status"] == "unknown"


def test_a_later_explicit_retry_can_replace_failed_state_with_new_official_evidence(source_pipeline, app_client, monkeypatch):
    envelope = db.get_sections(COURSE, TERM)
    sid = pending_session(envelope)
    real_enrich = db.enrich_sections_pairing
    monkeypatch.setattr(db, "get_live_sections", lambda *_args, **_kwargs: live_result(envelope["sections"]))
    monkeypatch.setattr(db, "enrich_sections_pairing", lambda cid, term, response, **_kwargs: {
        **response, "pairing_lookup_status": "unavailable"})
    failed = app_client.post("/api/schedule/refresh", json={"session_id": sid}).json()
    assert selection(failed)["status"] == "unknown" and evidence(sid)["generation"] == 1
    monkeypatch.setattr(db, "enrich_sections_pairing", real_enrich)
    retried = app_client.post("/api/schedule/refresh", json={"session_id": sid}).json()
    assert selection(retried)["status"] == "satisfied"
    assert evidence(sid)["generation"] == 2
    assert evidence(sid)["pairing_lookup_status"] == "available"


def test_refresh_deadline_does_not_trigger_a_new_lookup_when_no_card_or_live_rows(source_pipeline, app_client, monkeypatch):
    envelope = db.get_sections(COURSE, TERM)
    sid = pending_session()
    state = sessions.get_session_state("demo_001", sid)
    by_number = {row["section_num"]: row for row in envelope["sections"]}
    for entry in state["pending_schedule"]:
        entry["materialized_section"] = deepcopy(by_number[entry["section"]])
    sessions.update_session_state("demo_001", sid, {"pending_schedule": state["pending_schedule"]})
    monkeypatch.setattr(db, "get_sections", fail_lookup)
    monkeypatch.setattr(db, "get_course_info", fail_lookup)
    monkeypatch.setattr(db, "get_live_sections", lambda *_args, **_kwargs: None)

    def unavailable(*_args, **_kwargs):
        raise RuntimeError("pair source unavailable")

    monkeypatch.setattr(db, "enrich_sections_pairing", unavailable)
    response = app_client.post("/api/schedule/refresh", json={"session_id": sid})
    assert response.status_code == 200
    assert selection(response.json())["status"] == "unknown"
    assert response.json()["events"]
    assert len(response.json()["pending_schedule"]) == 2
    assert evidence(sid)["pairing_lookup_status"] == "unavailable"


def test_flags_off_preserve_legacy_refresh_without_pairing_state(source_pipeline, app_client, monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    sid = pending_session()
    rows = _parse()["sections"]
    monkeypatch.setattr(db, "get_sections", lambda *_: {"found": True, "sections": rows})
    monkeypatch.setattr(db, "get_live_sections", lambda *_args, **_kwargs: live_result(rows))
    monkeypatch.setattr(db, "enrich_sections_pairing", fail_lookup)
    refreshed = app_client.post("/api/schedule/refresh", json={"session_id": sid})
    assert refreshed.status_code == 200 and refreshed.json()["timed_out"] is False
    assert "pairing" not in refreshed.json()["schedule_validation"]
    assert "schedule_pairing_evidence" not in sessions.get_session_state("demo_001", sid)
