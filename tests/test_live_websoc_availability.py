from __future__ import annotations

import pytest
import requests

from app import observability
from app.catalog.types import CourseRef, SectionRecord
from app.data import anteater, db


def _raw_section(**overrides):
    section = {
        "sectionCode": "34070",
        "sectionNum": "A",
        "sectionType": "Lec",
        "meetings": [
            {
                "days": "TuTh",
                "startTime": {"hour": 9, "minute": 30},
                "endTime": {"hour": 10, "minute": 50},
                "bldg": ["DBH 1100"],
            }
        ],
        "instructors": ["THORNTON, A."],
        "maxCapacity": "100",
        "numCurrentlyEnrolled": {"totalEnrolled": "86"},
        "numOnWaitlist": "5",
        "numWaitlistCap": "20",
        "numNewOnlyReserved": "10",
        "status": "OPEN",
        "restrictions": "A",
        "finalExam": {"examStatus": "NO_FINAL"},
        "updatedAt": "2026-07-18T10:00:00Z",
    }
    section.update(overrides)
    return section


def test_fetch_live_sections_queries_section_codes_and_caches(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict] = []

    class FakeResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return {
                "ok": True,
                "data": {
                    "schools": [
                        {
                            "departments": [
                                {
                                    "courses": [
                                        {"sections": [_raw_section(sectionCode="34070")]}
                                    ]
                                }
                            ]
                        }
                    ]
                },
            }

    def fake_request(self, method, url, **kwargs):
        calls.append({"method": method, "url": url, "params": kwargs["params"]})
        return FakeResponse()

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)

    first = anteater.fetch_live_sections(
        year="2026",
        quarter="Fall",
        section_codes=["34070"],
    )
    second = anteater.fetch_live_sections(
        year="2026",
        quarter="Fall",
        section_codes=["34070"],
    )

    assert calls == [
        {
            "method": "get",
            "url": f"{anteater.ANTEATER_BASE_URL}/websoc",
            "params": {"year": "2026", "quarter": "Fall", "sectionCodes": "34070"},
        }
    ]
    assert first is not None
    assert first["cache_hit"] is False
    assert first["source"] == "live_anteater_websoc"
    assert first["sections"][0]["sectionCode"] == "34070"
    assert second is not None
    assert second["cache_hit"] is True
    metrics = observability.snapshot_metrics()
    assert metrics["counters"]["live_websoc.cache{result=miss}"] == 1
    assert metrics["counters"]["live_websoc.cache{result=hit}"] == 1
    assert metrics["counters"]["live_websoc.api_result{result=ok}"] == 1
    assert metrics["timings"]["live_websoc.api_latency{endpoint=/websoc,service=anteater}"]["count"] == 1


def test_fetch_live_sections_force_refresh_bypasses_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    class FakeResponse:
        status_code = 200
        text = "ok"

        def json(self):
            return {"ok": True, "data": {"schools": []}}

    def fake_request(self, method, url, **kwargs):
        nonlocal calls
        calls += 1
        return FakeResponse()

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)

    anteater.fetch_live_sections(
        year="2026",
        quarter="Fall",
        department="COMPSCI",
        course_number="161",
    )
    anteater.fetch_live_sections(
        year="2026",
        quarter="Fall",
        department="COMPSCI",
        course_number="161",
        force_refresh=True,
    )

    assert calls == 2


def test_fetch_live_sections_rate_limit_returns_unavailable_metric(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status_code = 429
        text = "rate limited"

    def fake_request(self, method, url, **kwargs):
        return FakeResponse()

    monkeypatch.setattr(requests.sessions.Session, "request", fake_request)

    result = anteater.fetch_live_sections(
        year="2026",
        quarter="Fall",
        department="COMPSCI",
        course_number="161",
    )

    assert result is None
    metrics = observability.snapshot_metrics()
    assert metrics["counters"]["external_api.rate_limited{service=anteater}"] == 1
    assert metrics["counters"]["live_websoc.api_result{result=unavailable}"] == 1


def test_get_live_sections_maps_anteater_availability_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fake_live_sections(**kwargs):
        assert kwargs["department"] == "COMPSCI"
        assert kwargs["course_number"] == "161"
        assert kwargs["year"] == "2026"
        assert kwargs["quarter"] == "Fall"
        return {
            "source": "live_anteater_websoc",
            "retrieved_at": "2026-07-18T11:00:00Z",
            "cache_hit": False,
            "sections": [_raw_section()],
        }

    monkeypatch.setattr(db.anteater, "fetch_live_sections", fake_live_sections)

    result = db.get_live_sections("COMPSCI 161", "Fall 2026")

    assert result["found"] is True
    assert result["source"] == "live_anteater_websoc"
    assert result["is_live"] is True
    assert result["retrieved_at"] == "2026-07-18T11:00:00Z"
    section = result["sections"][0]
    assert section["status"] == "OPEN"
    assert section["max_capacity"] == 100
    assert section["enrolled"] == 86
    assert section["seats_open"] == 14
    assert section["waitlisted"] == 5
    assert section["waitlist_capacity"] == 20
    assert section["new_only_reserved"] == 10
    assert section["restrictions"] == "A"
    assert section["updated_at"] == "2026-07-18T10:00:00Z"
    assert section["retrieved_at"] == "2026-07-18T11:00:00Z"
    assert section["source"] == "live_anteater_websoc"


def test_get_live_sections_does_not_guess_seats_when_capacity_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        db.anteater,
        "fetch_live_sections",
        lambda **_kwargs: {
            "source": "live_anteater_websoc",
            "retrieved_at": "2026-07-18T11:00:00Z",
            "cache_hit": False,
            "sections": [_raw_section(maxCapacity="", numCurrentlyEnrolled={})],
        },
    )

    result = db.get_live_sections("COMPSCI 161", "Fall 2026")

    section = result["sections"][0]
    assert section["max_capacity"] is None
    assert section["enrolled"] is None
    assert section["seats_open"] is None


def test_get_live_sections_local_fallback_is_marked_not_live(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ref = CourseRef("COMPSCI", "161")
    local_section = SectionRecord(
        section_id="2026_Fall_34070",
        course=ref,
        section_code="34070",
        term_id="2026_Fall",
        section_type="Lec",
        section_num="A",
        max_capacity=100,
        section_enrolled=90,
        num_on_waitlist=4,
        status="OPEN",
    )

    class FakeCatalog:
        def get_sections(self, requested_ref):
            assert requested_ref == ref
            return [local_section]

    monkeypatch.setattr(db.anteater, "fetch_live_sections", lambda **_kwargs: None)
    monkeypatch.setattr(db, "get_catalog", lambda _term: FakeCatalog())

    result = db.get_live_sections("COMPSCI 161", "Fall 2026")

    assert result["found"] is True
    assert result["source"] == "local_not_live"
    assert result["is_live"] is False
    assert "not current availability" in result["reason"]
    section = result["sections"][0]
    assert section["source"] == "local_not_live"
    assert section["is_live"] is False
    assert section["seats_open"] == 10
    assert section["retrieved_at"] is None
    metrics = observability.snapshot_metrics()
    assert metrics["counters"][
        "live_websoc.fallback{reason=anteater_unavailable,result=attempt_local}"
    ] == 1
    assert metrics["counters"][
        "live_websoc.fallback{reason=anteater_unavailable,result=local_not_live}"
    ] == 1
