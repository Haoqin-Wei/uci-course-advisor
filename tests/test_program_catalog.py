from __future__ import annotations

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.catalog import program_store, store
from app.catalog.program_sync import ProgramSyncResult, sync_programs
from app.catalog.scheduler import is_course_sync_due, run_due
from app.catalog.sync import SyncResult
from app.data.uci_general import anteater_programs
from app.terms.models import TermKey

# Shapes copied from real Anteater /programs responses (catalogue year 2026–27).
MAJORS = [
    {"id": "BA-780", "name": "Major in Psychology (B.A.)", "type": "B.A.", "division": "Undergraduate",
     "specializationRequired": False, "catalogYear": "20262027", "specializations": []},
    {"id": "BA-174", "name": "Major in Classics", "type": "B.A.", "division": "Undergraduate",
     "specializationRequired": True, "catalogYear": "20262027", "specializations": ["BA-174F"]},
]
MINORS = [{"id": "22A", "name": "Minor in Digital Filmmaking"}]
SPECS = [{"id": "BA-174F", "majorId": "BA-174", "name": "Greek and Latin"}]
PSYCH = {
    "id": "BA-780", "name": "Major in Psychology (B.A.)", "catalogYear": "20262027",
    "requirements": [
        {"label": "PSYCH 9A-9B-9C", "requirementId": "a1", "requirementType": "Course", "courseCount": 3,
         "courses": ["PSYCH9A", "PSYCH9B", "PSYCH9C"]},
        {"label": "1 Course and 1 Lab", "requirementId": "g1", "requirementType": "Group", "requirementCount": 1,
         "requirements": [
             {"label": "PSYCH 112A-112LA", "requirementId": "c1", "requirementType": "Course",
              "courseCount": 2, "courses": ["PSYCH112A", "PSYCH112LA"]},
             {"label": "Approved Electives", "requirementId": "u1", "requirementType": "Unit",
              "unitCount": 12, "courses": [], "qualifiers": ["Upper-division only"]}]},
        {"label": "See advisor", "requirementId": "m1", "requirementType": "Marker"},
    ],
    "schoolRequirements": {"name": "School of Social Sciences", "requirements": [
        {"label": "Math", "requirementId": "s1", "requirementType": "Course", "courseCount": 1,
         "courses": ["MATH2A"], "courseConstraints": {"minGrade": "C"}}]},
}


def detail(program_id, name="Program"):
    return {"id": program_id, "name": name, "catalogYear": "20262027", "requirements": [
        {"label": "Core", "requirementId": "r", "requirementType": "Course", "courseCount": 1, "courses": ["MATH2A"]}]}


def ok(data):
    return SimpleNamespace(ok=True, status="ok", error=None, data=data)


def api(calls=None, *, majors=MAJORS, minors=MINORS, specs=SPECS, overrides=None):
    """Fake Anteater: endpoint + params → response."""
    def fetch(endpoint, params=None):
        if calls is not None:
            calls.append((endpoint, (params or {}).get("programId") or (params or {}).get("id")))
        if overrides and (endpoint, str(params)) in overrides:
            return overrides[(endpoint, str(params))]
        if endpoint == "/programs/majors":
            return ok(majors)
        if endpoint == "/programs/minors":
            return ok(minors)
        if endpoint == "/programs/specializations":
            return ok(specs)
        if endpoint == "/programs/major":
            return ok(PSYCH if params["programId"] == "BA-780" else detail(params["programId"], "Major in Classics"))
        if endpoint in {"/programs/minor", "/programs/specialization"}:
            return ok(detail(params["programId"]))
        if endpoint == "/programs/ugradRequirements":
            return ok({"id": params["id"], "catalogYear": "20262027", "requirements": [
                {"label": "II. Science and Technology", "requirementId": "ge2", "requirementType": "Course",
                 "courseCount": 3, "courses": ["I&CSCI31"]},
                {"label": "I. Lower-Division Writing", "requirementId": "ge1", "requirementType": "Course",
                 "courseCount": 2, "courses": ["WRITING39B"]}]})
        raise AssertionError(endpoint)
    return fetch


@pytest.fixture
def db(tmp_path):
    return tmp_path / "catalog.db"


def test_sync_stores_every_kind_losslessly(db):
    calls = []
    result = sync_programs(fetch=api(calls), path=db)

    assert (result.outcome, result.program_count, result.previous_program_count) == ("synced", 8, 0)
    assert result.requests == len(calls) == 3 + 4 + 4            # lists + program details + campus sets
    assert calls[:3] == [("/programs/majors", None), ("/programs/major", "BA-780"), ("/programs/major", "BA-174")]
    assert program_store.counts_by_kind(path=db) == {
        "major": 2, "minor": 1, "specialization": 1, "ugrad_requirement": 4}
    assert program_store.get_program("major", "BA-780", path=db) == PSYCH       # nested tree, optional keys intact
    assert program_store.list_programs("major", path=db) == [MAJORS[1], MAJORS[0]]   # by name
    assert program_store.list_programs("specialization", path=db) == SPECS
    assert program_store.get_program("ugrad_requirement", "GE", path=db)["catalogYear"] == "20262027"
    assert program_store.get_program("major", "NOPE", path=db) is None
    snapshot = program_store.get_program_snapshot(path=db)
    assert snapshot["source"] == "anteater_programs" and snapshot["last_error"] is None

    again = sync_programs(fetch=api(), path=db)
    assert (again.outcome, again.program_count, again.previous_program_count) == ("synced", 8, 8)
    store.replace_term_sections("2027_Winter", [
        {"section_code": "1", "department": "MATH", "course_number": "2A"}], source="t", path=db)
    assert len(store.get_sections("2027_Winter", path=db)) == 1               # same file, separate tables


def test_read_only_probes_before_any_sync(db):
    assert program_store.has_programs(path=db) is False
    assert program_store.get_program_snapshot(path=db) is None
    assert program_store.list_programs("major", path=db) == []
    assert program_store.get_program("major", "BA-780", path=db) is None
    assert not db.exists()


def test_one_failing_program_aborts_the_whole_snapshot(db):
    sync_programs(fetch=api(), path=db)
    attempts = []
    timeout = SimpleNamespace(ok=False, status="timeout", error="slow", data=None)

    def flaky(endpoint, params=None):
        if endpoint == "/programs/minor":
            attempts.append(1)
            if len(attempts) < 3:
                return timeout
        return api()(endpoint, params)

    assert sync_programs(fetch=flaky, retry_wait_s=0, path=db).ok and len(attempts) == 3   # retried, then fine

    def dead(endpoint, params=None):
        if endpoint == "/programs/specialization":
            raise RuntimeError("socket closed")
        return api(majors=MAJORS[:1])(endpoint, params)

    failed = sync_programs(fetch=dead, retry_wait_s=0, path=db)
    assert failed.outcome == "failed" and "/programs/specialization" in failed.error
    assert "socket closed" in failed.error and (failed.program_count, failed.previous_program_count) == (8, 8)
    assert len(program_store.list_programs("major", path=db)) == 2          # the partial crawl never landed
    assert program_store.get_program_snapshot(path=db)["last_error"].startswith("/programs/specialization")


@pytest.mark.parametrize("bad_endpoint, bad, expected", [
    ("/programs/majors", ok({"not": "a list"}), "/programs/majors: schema_error"),
    ("/programs/major", ok(["not", "an object"]), "/programs/major {'programId': 'BA-780'}: schema_error"),
    ("/programs/ugradRequirements", SimpleNamespace(ok=False, status="non_200", error="HTTP 503", data=None),
     "/programs/ugradRequirements {'id': 'GE'}: non_200"),
])
def test_malformed_responses_fail_cleanly(db, bad_endpoint, bad, expected):
    def fetch(endpoint, params=None):
        return bad if endpoint == bad_endpoint else api()(endpoint, params)
    result = sync_programs(fetch=fetch, retry_wait_s=0, path=db)
    assert result.outcome == "failed" and result.error.startswith(expected)
    assert program_store.has_programs(path=db) is False


def test_shrink_guard_and_invalid_rows(db):
    many = [{"id": f"M{i}", "name": f"Major {i}"} for i in range(20)]
    sync_programs(fetch=api(majors=many), path=db)
    rejected = sync_programs(fetch=api(majors=many[:5]), path=db)
    assert rejected.outcome == "rejected_shrink" and rejected.error.startswith("rejected_shrink")
    assert len(program_store.list_programs("major", path=db)) == 20
    assert sync_programs(fetch=api(majors=many[:5]), force=True, path=db).program_count == 11

    for rows in ([], [{"kind": "club", "program_id": "x", "summary": {}, "detail": {}}],
                 [{"kind": "major", "program_id": "", "summary": {}, "detail": {}}],
                 [{"kind": "major", "program_id": "x", "summary": {}, "detail": ["list"]}]):
        with pytest.raises(ValueError):
            program_store.replace_programs(rows, source="t", force=True, path=db)
    assert len(program_store.list_programs("major", path=db)) == 5


def test_profile_api_readers_use_the_snapshot_and_fall_back_to_live_requests(monkeypatch):
    live = []

    def fake_get(path, params=None, **_):
        live.append(path)
        if path == "/programs/majors":
            return [{"id": "LIVE-1", "name": "Live Major", "type": "B.S.", "division": "Undergraduate"}]
        if path == "/programs/major":
            return {"id": params["programId"], "name": "Live detail", "requirements": []}
        if path == "/programs/ugradRequirements":
            return {"id": params["id"], "requirements": []}
        return None

    monkeypatch.setattr(anteater_programs, "_get", fake_get)
    assert [m["id"] for m in anteater_programs.majors()] == ["LIVE-1"]          # nothing synced: live
    assert anteater_programs.programs_source() == "anteater_api"
    assert anteater_programs.minors() == [] and anteater_programs.specializations() == []

    sync_programs(fetch=api())
    live.clear()

    assert anteater_programs.programs_source() == "catalog_store"
    assert [m["id"] for m in anteater_programs.majors()] == ["BA-174", "BA-780"]
    assert [m["id"] for m in anteater_programs.majors(type_filter="B.A.", division="Undergraduate")] == ["BA-174", "BA-780"]
    assert anteater_programs.majors(type_filter="B.S.") == []
    assert anteater_programs.major("BA-780") == PSYCH
    assert [r["label"][:2] for r in anteater_programs.ge_requirements()["requirements"]] == ["I.", "II"]   # still sorted
    assert anteater_programs.extra_requirements("UC")["id"] == "UC"
    assert anteater_programs.minors() == MINORS and anteater_programs.minor("22A")["id"] == "22A"
    assert anteater_programs.specializations("BA-174") == SPECS and anteater_programs.specializations("BA-780") == []
    assert anteater_programs.specialization("BA-174F")["id"] == "BA-174F"
    assert live == []                                                            # no request was needed

    assert anteater_programs.major("NOT-STORED")["name"] == "Live detail"        # unknown id: live fallback
    assert live == ["/programs/major"]


def service():
    def resolved(key):
        return SimpleNamespace(year=key.year, quarter=key.quarter, key=key,
                               week2_friday_cutoff=None, instruction_start=None)
    return SimpleNamespace(current_term=lambda: resolved(TermKey(2026, "Fall")),
                           automatic_term=lambda: resolved(TermKey(2027, "Winter")),
                           calendar_dates=lambda key: {})


def test_requirements_follow_the_once_per_term_rule(db):
    now = datetime.now(timezone.utc)
    snapshot = {"program_count": 304, "last_synced_at": (now - timedelta(days=60)).isoformat(),
                "last_attempt_at": (now - timedelta(days=60)).isoformat(), "last_error": None}
    assert is_course_sync_due(snapshot, now, count_key="program_count") is False
    assert is_course_sync_due(snapshot, now, latest_publication=now - timedelta(days=5),
                              count_key="program_count") is True
    assert is_course_sync_due({**snapshot, "program_count": 0}, now, count_key="program_count") is True

    calls = []

    def program_syncer(*, path):
        calls.append(path)
        program_store.replace_programs(
            [{"kind": "major", "program_id": "X", "summary": {"id": "X", "name": "X"}, "detail": {"id": "X"}}],
            source="t", force=True, path=path)
        return ProgramSyncResult("synced", 1, 0, 1)

    idle = lambda term_id, *, path: SyncResult(term_id, "failed", None, 0, 0, "offline")
    run_due(now=now + timedelta(days=1), service=service(), syncer=idle, course_syncer=None,
            program_syncer=program_syncer, path=db)
    assert calls == [db]                                          # nothing stored: fetch once
    run_due(now=now + timedelta(days=30), service=service(), syncer=idle, course_syncer=None,
            program_syncer=program_syncer, path=db)
    assert len(calls) == 1                                        # no new timetable since
