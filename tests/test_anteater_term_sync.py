import json
from pathlib import Path

import pytest
import requests

from app.data import anteater
from app.terms import TermKey


FIXTURES = Path(__file__).parent / "fixtures" / "terms"


class FakeResponse:
    def __init__(self, body=None, *, status_code=200, json_error=None):
        self._body = body
        self.status_code = status_code
        self._json_error = json_error
        self.content = json.dumps(body).encode() if body is not None else b"not-json"

    def json(self):
        if self._json_error:
            raise self._json_error
        return self._body


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def test_calendar_all_client_uses_documented_endpoint(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        anteater.requests,
        "get",
        lambda url, **kwargs: calls.append((url, kwargs))
        or FakeResponse(fixture("calendar_all.json")),
    )
    result = anteater.fetch_calendar_all()
    assert result.ok
    assert result.data[0]["instructionStart"] == "2026-09-24"
    assert calls[0][0].endswith("/v2/rest/calendar/all")


def test_websoc_terms_requires_short_names(monkeypatch) -> None:
    monkeypatch.setattr(
        anteater.requests,
        "get",
        lambda *args, **kwargs: FakeResponse(fixture("websoc_terms.json")),
    )
    result = anteater.fetch_websoc_terms()
    assert result.ok
    assert [record["shortName"] for record in result.data] == [
        "2026 Fall",
        "2027 Winter",
    ]


def test_full_websoc_requires_course_and_section_for_availability(monkeypatch) -> None:
    calls = []
    monkeypatch.setattr(
        anteater.requests,
        "get",
        lambda url, **kwargs: calls.append((url, kwargs))
        or FakeResponse(fixture("websoc_full_available.json")),
    )
    result = anteater.check_term_data_availability(TermKey(2027, "Winter"))
    assert result.available is True
    assert (result.course_count, result.section_count) == (1, 1)
    assert calls[0][1]["params"] == {"year": "2027", "quarter": "Winter"}


@pytest.mark.parametrize(
    "data",
    [
        {"schools": []},
        {"schools": [{"departments": [{"courses": []}]}]},
        {"schools": [{"departments": [{"courses": [{"sections": []}]}]}]},
    ],
)
def test_empty_websoc_shells_are_unavailable(monkeypatch, data) -> None:
    monkeypatch.setattr(
        anteater.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"ok": True, "data": data}),
    )
    result = anteater.check_term_data_availability(TermKey(2027, "Winter"))
    assert result.available is False


@pytest.mark.parametrize(
    ("response", "expected_status"),
    [
        (FakeResponse({"ok": False, "message": "bad"}), "api_error"),
        (FakeResponse({"ok": True}), "schema_error"),
        (FakeResponse({"ok": True, "data": {}}, status_code=503), "non_200"),
        (FakeResponse(json_error=ValueError("bad json")), "invalid_json"),
    ],
)
def test_transport_and_envelope_failures_are_structured(
    monkeypatch, response, expected_status
) -> None:
    monkeypatch.setattr(anteater.requests, "get", lambda *args, **kwargs: response)
    result = anteater.fetch_websoc_terms()
    assert result.status == expected_status
    assert result.ok is False


def test_timeout_is_distinct_from_network_error(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise requests.Timeout("slow")

    monkeypatch.setattr(anteater.requests, "get", timeout)
    assert anteater.fetch_calendar_all().status == "timeout"

    def network_error(*args, **kwargs):
        raise requests.ConnectionError("offline")

    monkeypatch.setattr(anteater.requests, "get", network_error)
    assert anteater.fetch_calendar_all().status == "network_error"


def test_schema_errors_do_not_accept_wrong_endpoint_shapes(monkeypatch) -> None:
    monkeypatch.setattr(
        anteater.requests,
        "get",
        lambda *args, **kwargs: FakeResponse({"ok": True, "data": {"items": []}}),
    )
    assert anteater.fetch_calendar_all().status == "schema_error"
    assert anteater.fetch_full_websoc(TermKey(2027, "Winter")).status == "schema_error"
