from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
import requests

from app.data import zotistics


FIXTURES = Path(__file__).parent / "fixtures" / "zotistics"


def _html(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _homepage() -> str:
    # The checked-in public snapshot omits all session information. Inject a
    # synthetic token so the HTTP test verifies the actual GET/POST handshake.
    return _html("homepage.html").replace(
        '<form id="main-form" method="POST">',
        '<form id="main-form" method="POST">'
        '<input name="csrf_token" value="synthetic-test-token">',
    )


def _with_counts(html: str, counts: list[int]) -> str:
    total = sum(counts)
    percentages = [round(count / total * 100, 1) for count in counts] if total else [None] * 7
    return re.sub(
        r"changeGraph\(\[[^\]]*\],\s*\[[^\]]*\]",
        f"changeGraph({json.dumps(percentages)}, {json.dumps(counts)}",
        html,
    )


class FakeResponse:
    status_code = 200
    url = "https://zotistics.com/"

    def __init__(self, text: str):
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": "text/html; charset=utf-8"}

    def raise_for_status(self) -> None:
        return None


class FakeSession:
    def __init__(self, result: str | Exception | list[str | Exception]):
        self.headers: dict[str, str] = {}
        self.get_calls: list[dict] = []
        self.post_calls: list[dict] = []
        self.results = result if isinstance(result, list) else [result]

    def get(self, url: str, **kwargs):
        assert url == "https://zotistics.com/"
        self.get_calls.append(kwargs)
        return FakeResponse(_homepage())

    def post(self, url: str, **kwargs):
        assert url == "https://zotistics.com/"
        self.post_calls.append(kwargs)
        result = self.results[min(len(self.post_calls) - 1, len(self.results) - 1)]
        if isinstance(result, Exception):
            raise result
        return FakeResponse(result)

    def close(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None


@pytest.fixture(autouse=True)
def isolated_zotistics_cache(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    monkeypatch.setattr(zotistics, "CACHE_DIR", tmp_path / "zotistics")


def _fake_http(monkeypatch: pytest.MonkeyPatch, result=None) -> FakeSession:
    session = FakeSession(result if result is not None else _html("ics33_thornton.html"))
    monkeypatch.setattr(zotistics.requests, "Session", lambda: session)
    return session


def test_parser_reads_sanitized_original_site_schema():
    page = zotistics._parse_page(_html("ics33_thornton.html"))

    assert page.counts == [302, 333, 222, 56, 45, 12, 9]
    assert len(page.rows) == 5
    assert page.rows[0] == [
        52268, "Spring", 2023, "I&C SCI", "33", "INTERMEDIATE PRGRMG",
        "THORNTON, A.", 35745, "2022-23",
    ]
    home = zotistics._parse_page(_homepage())
    assert home.csrf_token == "synthetic-test-token"
    assert "THORNTON, A." in home.names
    assert home.last_grade_update == "2023 Spring"


def test_no_data_nan_graph_is_parsed_without_fabricated_grade_statistics():
    result = zotistics._parse_result(_html("no_data.html"), course_id="ICS33")

    assert result["status"] == "no_data"
    assert "nan" not in json.dumps(result).lower()
    # The real site incorrectly shows median 3.5 for an empty query. It must not
    # escape as a usable statistic in the normalized result.
    assert "3.5" not in json.dumps(result)


@pytest.mark.parametrize("course_id", ["ICS33", "ICS 33", "I&C SCI 33"])
def test_course_aliases_send_registrar_department_and_number(monkeypatch, course_id):
    session = _fake_http(monkeypatch)

    result = zotistics.get_grade_distribution(course_id=course_id, instructor_name="Alex Thornton")

    assert result["status"] == "ok"
    data = session.post_calls[0]["data"]
    assert data["selectDep"] == "I&C SCI"
    assert data["classNum"] == "33"
    assert data["teacher"] == "THORNTON, A."
    assert data["csrf_token"] == "synthetic-test-token"
    assert not data.get("selectYear")
    assert not data.get("selectQuarter")


@pytest.mark.parametrize("course_id", ["COMPSCI161", "COMPSCI 161", "CS 161"])
def test_computer_science_aliases_are_scoped_to_source_department(monkeypatch, course_id):
    session = _fake_http(monkeypatch, _html("no_data.html"))

    result = zotistics.get_grade_distribution(course_id=course_id)

    assert result["status"] == "no_data"
    assert session.post_calls[0]["data"]["selectDep"] == "COMPSCI"
    assert session.post_calls[0]["data"]["classNum"] == "161"


@pytest.mark.parametrize("name", ["THORNTON, A.", "Thornton, A.", "Alex Thornton"])
def test_source_canonical_and_full_names_resolve_to_same_instructor(monkeypatch, name):
    session = _fake_http(monkeypatch)

    result = zotistics.get_grade_distribution(course_id="ICS33", instructor_name=name)

    assert result["status"] == "ok"
    assert session.post_calls[0]["data"]["teacher"] == "THORNTON, A."


@pytest.mark.parametrize("name", ["Alice Thornton", "Andrew Thornton", "THORNTON, Alice"])
def test_conflicting_full_name_cannot_silently_resolve_from_shared_initial(monkeypatch, name):
    from app.data import professors

    monkeypatch.setattr(professors, "lookup_professor", lambda *_args, **_kwargs: {
        "firstName": "Alex", "lastName": "Thornton",
    })
    matched, candidates = zotistics._resolve_instructor(name, ["THORNTON, A."])

    assert matched is None
    assert candidates == ["THORNTON, A."]
    session = _fake_http(monkeypatch)
    result = zotistics.get_grade_distribution(course_id="ICS33", instructor_name=name)
    assert result["status"] == "ambiguous_instructor"
    assert result["candidates"] == ["THORNTON, A."]
    assert not session.post_calls


@pytest.mark.parametrize("name", ["Alex Thornton", "THORNTON, Alex", "THORNTON, A.", "Thornton, A"])
def test_verified_full_name_and_explicit_source_abbreviations_remain_usable(monkeypatch, name):
    from app.data import professors

    monkeypatch.setattr(professors, "lookup_professor", lambda *_args, **_kwargs: {
        "firstName": "Alex", "lastName": "Thornton",
    })

    matched, candidates = zotistics._resolve_instructor(name, ["THORNTON, A.", "THORNTON, K."])

    assert matched == "THORNTON, A."
    assert candidates == ["THORNTON, A."]


def test_unique_surname_resolves_and_does_not_post_unmatched_input(monkeypatch):
    session = _fake_http(monkeypatch, _html("no_data.html"))

    result = zotistics.get_grade_distribution(instructor_name="Klefstad")

    assert result["status"] == "no_data"
    assert session.post_calls[0]["data"]["teacher"] == "KLEFSTAD, R."


def test_ambiguous_surname_requires_disambiguation_before_any_grade_query(monkeypatch):
    session = _fake_http(monkeypatch)

    result = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Thornton")

    assert result["status"] == "ambiguous_instructor"
    assert not session.post_calls
    assert "THORNTON, A." in json.dumps(result)
    assert "THORNTON, K." in json.dumps(result)


def test_missing_instructor_cannot_widen_to_all_professors(monkeypatch):
    session = _fake_http(monkeypatch)

    result = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Not A Real Professor")

    assert result["status"] == "not_found"
    assert not session.post_calls


@pytest.mark.parametrize("kwargs", [
    {}, {"course_id": "33"}, {"department": "ICS"},
    {"course_id": "ICS33", "department": "MATH"},
    {"course_id": "ICS33", "instructor_name": "STAFF"},
    {"instructor_name": "KLEFSTAD, 123"},
    {"instructor_name": "KLEFSTAD,"},
])
def test_queries_without_course_or_instructor_are_rejected_before_network(monkeypatch, kwargs):
    session = _fake_http(monkeypatch)

    result = zotistics.get_grade_distribution(**kwargs)

    assert result["status"] == "invalid_query"
    assert not session.get_calls
    assert not session.post_calls


@pytest.mark.parametrize("counts", [
    "[-1, 333, 222, 56, 45, 12, 9]",
    "[302.5, 333, 222, 56, 45, 12, 9]",
    "[true, 333, 222, 56, 45, 12, 9]",
    "[302, 333, 222, 56, 45, 12]",
])
def test_malformed_grade_counts_are_unavailable_rather_than_zero_or_no_data(counts):
    html = _with_counts(_html("ics33_thornton.html"), json.loads(counts))

    assert zotistics._parse_result(html, course_id="ICS33")["status"] == "unavailable"


@pytest.mark.parametrize("course_id,instructor_name,department", [
    ("ICS32", None, None),
    ("ICS33", "PATTIS, R.", None),
    (None, None, "COMPSCI"),
])
def test_mismatched_source_rows_cannot_be_used_as_requested_evidence(course_id, instructor_name, department):
    result = zotistics._parse_result(
        _html("ics33_thornton.html"), course_id=course_id,
        instructor_name=instructor_name, department=department,
    )

    assert result["status"] == "unavailable"


def test_layout_change_is_not_reported_as_absent_grade_history():
    result = zotistics._parse_result("<html><body>Maintenance</body></html>", course_id="ICS33")

    assert result["status"] == "unavailable"


def test_duplicate_offering_rows_cannot_inflate_grade_evidence():
    html = _html("ics33_thornton.html").replace(
        '[48494, "Fall", 2022, "I\\u0026C SCI", "33", "INTERMEDIATE PRGRMG", "THORNTON, A.", 35680, "2022-23"]',
        '[52268, "Spring", 2023, "I\\u0026C SCI", "33", "INTERMEDIATE PRGRMG", "THORNTON, A.", 35745, "2022-23"]',
    )

    assert zotistics._parse_result(html, course_id="ICS33")["status"] == "unavailable"


def test_source_scripts_are_data_and_cannot_execute_python(tmp_path):
    marker = tmp_path / "source-was-executed"
    script = '<script>autoSearch([__import__("pathlib").Path(' + repr(str(marker)) + ').touch()], "nameField");</script>'

    result = zotistics._parse_result(script, course_id="ICS33")

    assert result["status"] == "unavailable"
    assert not marker.exists()


def test_real_counts_use_letter_grade_denominator_and_preserve_historical_scope():
    result = zotistics._parse_result(
        _html("ics33_thornton.html"), course_id="ICS33", instructor_name="THORNTON, A.",
        coverage={"last_grade_update": "2023 Spring", "plus_minus_available": False},
    )

    assert result["status"] == "ok"
    grades = result["grades"]
    assert grades["samples"] == 979
    assert grades["graded"] == 958
    assert grades["class_count"] == 5
    assert grades["letter_counts"] == {"A": 302, "B": 333, "C": 222, "D": 56, "F": 45, "P": 12, "NP": 9}
    # The site's separately reported 2.852 does not equal its broad-bucket
    # counts. The adapter's stated A=4/B=3/... estimate must follow the counts.
    assert grades["avg_gpa"] == 2.826
    assert grades["source_reported_gpa"] == 2.852
    assert grades["gpa_is_approximate"] is True
    assert grades["pct_A"] == 31.5
    assert grades["site_percentages"]["A"] == 30.8
    assert grades["percentage_denominator"] == "A–F grade records only"
    assert grades["historical_only"] is True
    assert grades["terms"] == ["2022 Fall", "2023 Winter", "2023 Spring"]
    assert grades["term_range"] == {"first": "2022 Fall", "last": "2023 Spring"}
    assert result["coverage"]["plus_minus_available"] is False
    assert all(key not in grades["letter_counts"] for key in ("A+", "A-", "B+"))


@pytest.mark.parametrize("counts,expected_gpa,expected_graded", [
    ("[0, 0, 0, 0, 45, 12, 9]", 0.0, 45),
    ("[0, 0, 0, 0, 0, 12, 9]", None, 0),
])
def test_f_only_and_pass_only_results_do_not_invent_or_drop_zero_gpa(counts, expected_gpa, expected_graded):
    html = _with_counts(_html("ics33_thornton.html"), json.loads(counts))

    result = zotistics._parse_result(html, course_id="ICS33")

    assert result["status"] == "ok"
    assert result["grades"]["graded"] == expected_graded
    assert result["grades"]["avg_gpa"] == expected_gpa
    if expected_graded == 0:
        assert result["grades"]["pct_A"] is None
        assert all(value is None for value in result["grades"]["letter_pct"].values())


@pytest.mark.parametrize("reported", ["nan", "Infinity", "-Infinity", "-0.1", "4.1", "99999999999999999999"])
def test_nonfinite_or_out_of_range_source_gpa_cannot_escape_as_a_statistic(reported):
    html = _html("ics33_thornton.html").replace("Total GPA: 2.852", f"Total GPA: {reported}")

    result = zotistics._parse_result(html, course_id="ICS33")

    assert result["status"] == "ok"
    assert result["grades"]["source_reported_gpa"] is None
    assert result["grades"]["avg_gpa"] == 2.826


def test_pass_only_nan_reported_gpa_is_missing_not_a_numeric_grade():
    html = _with_counts(_html("ics33_thornton.html"), [0, 0, 0, 0, 0, 12, 9]).replace("Total GPA: 2.852", "Total GPA: nan")

    result = zotistics._parse_result(html, course_id="ICS33")

    assert result["status"] == "ok"
    assert result["grades"]["source_reported_gpa"] is None
    assert result["grades"]["avg_gpa"] is None


def test_failed_request_is_not_cached_and_next_call_retries(monkeypatch):
    session = _fake_http(monkeypatch, [requests.Timeout("simulated"), _html("ics33_thornton.html")])

    first = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Alex Thornton")
    assert first["status"] == "unavailable"
    assert not list(zotistics.CACHE_DIR.glob("*.json"))
    second = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Alex Thornton")

    assert second["status"] == "ok"
    assert len(session.post_calls) == 2


@pytest.mark.parametrize("failure", ["missing_token", "missing_directory", "http_error", "unexpected_redirect", "oversized"])
def test_source_form_and_http_failures_never_become_negative_grade_cache(monkeypatch, failure):
    session = _fake_http(monkeypatch)
    response = FakeResponse(_homepage())
    if failure == "missing_token":
        response = FakeResponse(_html("homepage.html"))
    elif failure == "missing_directory":
        response = FakeResponse('<input name="csrf_token" value="synthetic-test-token">')
    elif failure == "http_error":
        def raise_http():
            raise requests.HTTPError("source error")
        response.raise_for_status = raise_http
    elif failure == "unexpected_redirect":
        response.url = "https://other.example/"
    elif failure == "oversized":
        response.content = b"x" * 2_000_001
    monkeypatch.setattr(session, "get", lambda *_args, **_kwargs: response)

    result = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Alex Thornton")

    assert result["status"] == "unavailable"
    assert not session.post_calls
    assert not list(zotistics.CACHE_DIR.glob("*.json"))


def test_successful_query_is_cached_without_repeating_source_requests(monkeypatch):
    session = _fake_http(monkeypatch)

    first = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Alex Thornton")
    second = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Alex Thornton")

    assert first["status"] == second["status"] == "ok"
    assert len(session.get_calls) == 1
    assert len(session.post_calls) == 1


def test_no_data_response_is_cached_but_not_as_permanent_absence(monkeypatch):
    session = _fake_http(monkeypatch, _html("no_data.html"))

    first = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Alex Thornton")
    second = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Alex Thornton")

    assert first["status"] == second["status"] == "no_data"
    assert len(session.post_calls) == 1
    assert list(zotistics.CACHE_DIR.glob("*.json"))


@pytest.mark.parametrize("fixture_name,ttl", [("no_data.html", 3600), ("ics33_thornton.html", 7 * 24 * 3600)])
def test_cache_expiry_requeries_source_and_negative_cache_has_shorter_lifetime(monkeypatch, fixture_name, ttl):
    session = _fake_http(monkeypatch, _html(fixture_name))
    now = [1000.0]
    monkeypatch.setattr(zotistics.time, "time", lambda: now[0])

    first = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Alex Thornton")
    files = list(zotistics.CACHE_DIR.glob("*.json"))
    assert len(files) == 1
    entry = json.loads(files[0].read_text(encoding="utf-8"))
    assert entry["expires_at"] == now[0] + ttl
    now[0] += ttl - 1
    cached = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Alex Thornton")
    assert cached["cached"] is True
    assert len(session.post_calls) == 1
    now[0] += 1
    fresh = zotistics.get_grade_distribution(course_id="ICS33", instructor_name="Alex Thornton")

    assert fresh["status"] == first["status"]
    assert fresh["cached"] is False
    assert len(session.post_calls) == 2


@pytest.mark.parametrize("entry", [
    None, [], "invalid cache root",
    {"expires_at": 9999999999, "result": None},
    {"expires_at": 9999999999, "result": []},
    {"expires_at": 9999999999, "result": "invalid cache result"},
    {"expires_at": 9999999999, "result": {"status": "ok", "found": True, "source": "anteater"}},
    {"expires_at": 9999999999, "result": {"status": "ok", "found": False, "source": "zotistics"}},
    {"expires_at": 9999999999, "result": {"status": "no_data", "found": True, "source": "zotistics"}},
    {"expires_at": 9999999999, "result": {"status": "ok", "source": "zotistics"}},
])
def test_malformed_or_untrusted_cache_entries_are_treated_as_misses(entry):
    zotistics.CACHE_DIR.mkdir(parents=True)
    (zotistics.CACHE_DIR / "invalid.json").write_text(json.dumps(entry), encoding="utf-8")

    assert zotistics._read_cache("invalid") is None


@pytest.mark.parametrize("fixture_name,course_id,expected", [
    ("ics45c_klefstad.html", "ICS45C", {"A": 871, "samples": 2079, "graded": 2066, "classes": 12, "site_A": 41.9, "pct_A": 42.2}),
    ("ics46_klefstad.html", "ICS46", {"A": 232, "samples": 399, "graded": 395, "classes": 2, "site_A": 58.1, "pct_A": 58.7}),
])
def test_verified_klefstad_courses_keep_their_own_distribution(fixture_name, course_id, expected):
    result = zotistics._parse_result(_html(fixture_name), course_id=course_id, instructor_name="KLEFSTAD, R.")

    assert result["status"] == "ok"
    grades = result["grades"]
    assert grades["letter_counts"]["A"] == expected["A"]
    assert grades["samples"] == expected["samples"]
    assert grades["graded"] == expected["graded"]
    assert grades["class_count"] == expected["classes"]
    assert grades["site_percentages"]["A"] == expected["site_A"]
    assert grades["pct_A"] == expected["pct_A"]
    assert grades["a_rate"] == expected["A"] / expected["graded"]
    assert grades["scope"] == result["scope"] == "course_instructor"
    assert grades["courses"] == [course_id]
    assert grades["instructors"] == ["KLEFSTAD, R."]
    assert result["coverage"]["class_count"] == expected["classes"]


def test_professor_overall_distribution_is_preserved_as_multiple_courses(monkeypatch):
    session = _fake_http(monkeypatch, _html("klefstad_overall.html"))

    result = zotistics.get_grade_distribution(instructor_name="KLEFSTAD, R.")

    assert result["status"] == "ok"
    assert result["scope"] == result["grades"]["scope"] == "instructor"
    assert result["query"] == {"course_id": None, "instructor_name": "KLEFSTAD, R.", "department": None}
    assert result["grades"]["letter_counts"]["A"] == 3821
    assert result["grades"]["graded"] == 7879
    assert result["grades"]["samples"] == 7950
    assert result["grades"]["class_count"] == 85
    assert len(result["grades"]["courses"]) == 17
    assert result["coverage"]["last_grade_update"] == "2023 Spring"
    assert result["coverage"]["term_range"] == {"first": "2014 Fall", "last": "2023 Winter"}
    assert session.post_calls[0]["data"]["selectDep"] == "ALL"
    assert session.post_calls[0]["data"]["classNum"] == ""


def test_pair_missing_data_never_triggers_implicit_professor_overall_fallback(monkeypatch):
    session = _fake_http(monkeypatch, _html("no_data.html"))

    result = zotistics.get_grade_distribution(course_id="ICS45J", instructor_name="KLEFSTAD, R.")

    assert result["status"] == "no_data"
    assert result["scope"] == "course_instructor"
    assert result["query"]["course_id"] == "ICS45J"
    assert result["resolved_instructor_name"] == "KLEFSTAD, R."
    assert result["grades"] is None
    assert len(session.post_calls) == 1
    assert session.post_calls[0]["data"]["classNum"] == "45J"


def test_source_department_directory_is_checked_before_post(monkeypatch):
    session = _fake_http(monkeypatch)
    response = FakeResponse(_homepage().replace('<option value="COMPSCI">Computer Science</option>', ""))
    monkeypatch.setattr(session, "get", lambda *_args, **_kwargs: response)

    result = zotistics.get_grade_distribution(course_id="CS161")

    assert result["status"] == "invalid_query"
    assert not session.post_calls


@pytest.mark.parametrize("course_id", ["ICS33 and ICS32", "taking ICS33", "ICS33 extra"])
def test_extra_query_text_cannot_be_silently_reduced_to_one_course(monkeypatch, course_id):
    session = _fake_http(monkeypatch)

    assert zotistics.get_grade_distribution(course_id=course_id)["status"] == "invalid_query"
    assert not session.get_calls


def test_mismatched_percentages_are_unavailable_rather_than_recomputed():
    html = _html("ics33_thornton.html").replace("[30.8, 34.0, 22.7, 5.7, 4.6, 1.2, 0.9]", "[99.9, 0, 0, 0, 0, 0, 0]")

    assert zotistics._parse_result(html, course_id="ICS33")["status"] == "unavailable"


@pytest.mark.parametrize("reported", ["0", "4.0"])
def test_reported_gpa_valid_boundaries_are_preserved(reported):
    html = _html("ics33_thornton.html").replace("Total GPA: 2.852", f"Total GPA: {reported}")

    result = zotistics._parse_result(html, course_id="ICS33")

    assert result["status"] == "ok"
    assert result["grades"]["source_reported_gpa"] == float(reported)
    assert result["grades"]["avg_gpa"] == 2.826


def test_snapshot_cache_miss_performs_no_network_or_filesystem_write(monkeypatch):
    def forbidden():
        raise AssertionError("cache-only lookup must not create HTTP sessions")
    monkeypatch.setattr(zotistics.requests, "Session", forbidden)

    result = zotistics.get_cached_grade_distribution(course_id="ICS45C", instructor_name="KLEFSTAD, R.")

    assert result["status"] == "cache_miss"
    assert result["found"] is False
    assert result["grades"] is None
    assert result["query"] == {"course_id": "ICS45C", "instructor_name": "KLEFSTAD, R.", "department": "I&C SCI"}
    assert not zotistics.CACHE_DIR.exists()


def test_snapshot_lookup_reuses_canonical_query_aliases_without_network_or_writes(monkeypatch):
    session = _fake_http(monkeypatch, _html("ics45c_klefstad.html"))
    result = zotistics.get_grade_distribution(course_id="ICS45C", instructor_name="KLEFSTAD, R.")
    files_before = {path.name: path.read_bytes() for path in zotistics.CACHE_DIR.iterdir()}
    def forbidden():
        raise AssertionError("cache-only lookup must not create HTTP sessions")
    monkeypatch.setattr(zotistics.requests, "Session", forbidden)

    snapshot = zotistics.get_cached_grade_distribution(course_id="I&C SCI 45C", instructor_name="klefstad, r", department="ICS")

    assert snapshot["status"] == "ok"
    assert snapshot["cached"] is True
    assert snapshot["grades"] == result["grades"]
    assert {path.name: path.read_bytes() for path in zotistics.CACHE_DIR.iterdir()} == files_before
    assert len(session.post_calls) == 1


def test_snapshot_cache_does_not_return_negative_or_expired_history(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(zotistics.time, "time", lambda: now[0])
    _fake_http(monkeypatch, _html("no_data.html"))
    zotistics.get_grade_distribution(course_id="ICS45C", instructor_name="KLEFSTAD, R.")
    assert zotistics.get_cached_grade_distribution(course_id="ICS45C", instructor_name="KLEFSTAD, R.")["status"] == "cache_miss"
    _fake_http(monkeypatch, _html("ics45c_klefstad.html"))
    now[0] += 3600
    assert zotistics.get_grade_distribution(course_id="ICS45C", instructor_name="KLEFSTAD, R.")["status"] == "ok"
    now[0] += 7 * 24 * 3600

    assert zotistics.get_cached_grade_distribution(course_id="ICS45C", instructor_name="KLEFSTAD, R.")["status"] == "cache_miss"


@pytest.mark.parametrize("tamper", ["query", "source", "found", "counts", "resolved_instructor", "scope", "percentages"])
def test_positive_cache_requires_consistent_source_query_identity_and_statistics(monkeypatch, tamper):
    _fake_http(monkeypatch, _html("ics45c_klefstad.html"))
    zotistics.get_grade_distribution(course_id="ICS45C", instructor_name="KLEFSTAD, R.")
    path = next(zotistics.CACHE_DIR.glob("*.json"))
    entry = json.loads(path.read_text(encoding="utf-8"))
    result = entry["result"]
    if tamper == "query":
        result["query"]["course_id"] = "ICS46"
    elif tamper == "source":
        result["source"] = "anteater"
    elif tamper == "found":
        result["found"] = False
    elif tamper == "counts":
        result["grades"]["letter_counts"]["A"] += 1
    elif tamper == "resolved_instructor":
        result["resolved_instructor_name"] = result["instructor_name"] = "PATTIS, R."
        for row in result["evidence"]["offerings"]:
            row[6] = "PATTIS, R."
    elif tamper == "scope":
        result["scope"] = "instructor"
    elif tamper == "percentages":
        result["grades"]["site_percentages"]["A"] = 100
    path.write_text(json.dumps(entry), encoding="utf-8")

    assert zotistics.get_cached_grade_distribution(course_id="ICS45C", instructor_name="KLEFSTAD, R.")["status"] == "cache_miss"


def test_streamed_source_read_stops_at_response_limit(monkeypatch):
    session = _fake_http(monkeypatch)
    response = FakeResponse(_homepage())
    delivered = []
    def chunks(**_kwargs):
        for index in range(4):
            delivered.append(index)
            yield b"x" * 1_000_000
    response.iter_content = chunks
    monkeypatch.setattr(session, "get", lambda *_args, **_kwargs: response)

    result = zotistics.get_grade_distribution(course_id="ICS33")

    assert result["status"] == "unavailable"
    assert delivered == [0, 1, 2]
    assert not session.post_calls
    assert not zotistics.CACHE_DIR.exists()


def test_malformed_course_number_in_overall_metadata_is_rejected():
    html = _html("ics33_thornton.html").replace('"33", "INTERMEDIATE PRGRMG"', '"33 extra", "INTERMEDIATE PRGRMG"')

    assert zotistics._parse_result(html, instructor_name="THORNTON, A.")["status"] == "unavailable"


def test_verified_pair_no_data_and_professor_positive_cache_replay_without_network(monkeypatch):
    session = _fake_http(monkeypatch, [_html("no_data.html"), _html("klefstad_overall.html")])
    pair = zotistics.get_grade_distribution(course_id="ICS45J", instructor_name="KLEFSTAD, R.")
    overall = zotistics.get_grade_distribution(instructor_name="KLEFSTAD, R.")
    assert pair["status"] == "no_data"
    assert overall["status"] == "ok"
    before = {path.name: path.read_bytes() for path in zotistics.CACHE_DIR.iterdir()}
    def forbidden():
        raise AssertionError("cache replay must not create HTTP sessions")
    monkeypatch.setattr(zotistics.requests, "Session", forbidden)

    pair_snapshot = zotistics.get_cached_query_result(course_id="I&C SCI 45J", instructor_name="klefstad, r", department="ICS")
    overall_snapshot = zotistics.get_cached_query_result(instructor_name="KLEFSTAD, R.")

    assert pair_snapshot["status"] == "no_data"
    assert pair_snapshot["scope"] == "course_instructor"
    assert pair_snapshot["query"] == {"course_id": "ICS45J", "instructor_name": "KLEFSTAD, R.", "department": "I&C SCI"}
    assert pair_snapshot["resolved_instructor_name"] == "KLEFSTAD, R."
    assert pair_snapshot["found"] is False
    assert pair_snapshot["grades"] is None
    assert overall_snapshot["status"] == "ok"
    assert overall_snapshot["scope"] == "instructor"
    assert overall_snapshot["query"]["department"] is None
    assert overall_snapshot["grades"]["a_rate"] == 3821 / 7879
    assert len(overall_snapshot["grades"]["courses"]) == 17
    assert zotistics.get_cached_grade_distribution(course_id="ICS45J", instructor_name="KLEFSTAD, R.")["status"] == "cache_miss"
    assert zotistics.get_cached_query_result(instructor_name="KLEFSTAD, R.", department="ICS")["status"] == "cache_miss"
    assert len(session.post_calls) == 2
    assert {path.name: path.read_bytes() for path in zotistics.CACHE_DIR.iterdir()} == before


def test_real_negative_cache_replay_produces_explicit_overall_ranking_feature(monkeypatch):
    from app.agent import grade_evidence
    from app.recommendations.features import build_candidate_features

    _fake_http(monkeypatch, [_html("no_data.html"), _html("klefstad_overall.html")])
    zotistics.get_grade_distribution(course_id="ICS45J", instructor_name="KLEFSTAD, R.")
    zotistics.get_grade_distribution(instructor_name="KLEFSTAD, R.")
    monkeypatch.setattr(grade_evidence.db, "get_zotistics_grades", zotistics.get_cached_query_result)
    def forbidden():
        raise AssertionError("replayed evidence must not open an HTTP session")
    monkeypatch.setattr(zotistics.requests, "Session", forbidden)
    context = {}

    history = grade_evidence.get_history(context, "I&C SCI 45J", "KLEFSTAD, R.", "ICS")
    replay = grade_evidence.find_history(context, "ICS45J", "KLEFSTAD, R.", "I&C SCI")
    feature = build_candidate_features(
        grade_evidence.ranking_history(replay), course_id="I&C SCI 45J", instructor_name="KLEFSTAD, R.",
        primary_section={"section_type": "Lec", "instructors": ["KLEFSTAD, R."]},
    )["grade_a_rate"]

    assert history["status"] == "ok"
    assert history["scope"] == "instructor"
    assert history["query"]["department"] is None
    assert history["fallback_from_scope"] == "course_instructor"
    assert feature["value"] == 3821 / 7879
    assert feature["sample_count"] == 7879
    assert feature["scope"] == "instructor"
    assert feature["fallback"] is True
    assert feature["requested_course_id"] == "I&C SCI 45J"
    assert feature["uncertain_reason"] == "instructor_wide_fallback"


def test_negative_query_cache_expires_and_cannot_authorize_later_fallback(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(zotistics.time, "time", lambda: now[0])
    _fake_http(monkeypatch, _html("no_data.html"))
    zotistics.get_grade_distribution(course_id="ICS45J", instructor_name="KLEFSTAD, R.")
    assert zotistics.get_cached_query_result(course_id="ICS45J", instructor_name="KLEFSTAD, R.")["status"] == "no_data"
    now[0] += 3600

    assert zotistics.get_cached_query_result(course_id="ICS45J", instructor_name="KLEFSTAD, R.")["status"] == "cache_miss"
