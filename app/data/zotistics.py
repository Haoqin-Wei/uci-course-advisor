"""Exact historical grade searches on the original Zotistics website.

The original site returns a server-rendered search result, not the beta
site's API. Only bounded JSON chart data and offering metadata are read;
page scripts are never executed. This module does no course/professor
fallback: callers must retain the requested evidence scope.
"""
from __future__ import annotations

import ast
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import logging
import math
from pathlib import Path
import re
import tempfile
import time
from typing import Optional

import requests

from app import observability
from app.catalog.departments import colloquial_course_id, resolve_department
from app.catalog.normalization import iter_course_mentions, parse_course_mention

logger = logging.getLogger(__name__)
SOURCE_URL = "https://zotistics.com/"
CACHE_DIR = Path(__file__).resolve().parents[2] / "data/runtime/zotistics"
REQUEST_TIMEOUT_S = 10
MAX_RESPONSE_BYTES = 2_000_000
CACHE_TTL_S = 7 * 24 * 60 * 60
NO_DATA_TTL_S = 60 * 60
_CACHE_VERSION = 1
_LETTERS = ("A", "B", "C", "D", "F", "P", "NP")
_QUARTERS = {"Winter": 0, "Spring": 1, "Summer": 2, "Fall": 3}
_LIMITATIONS = [
    "Historical records only; not a forecast of future grades or instructors.",
    "No +/- grades; the count-based GPA estimate excludes P/NP.",
    "Small classes and incomplete records may be omitted; missing data is not poor grading.",
    "Instructor attribution may differ from WebSoc and omit co-instructors.",
    "Grade samples are course grade records, not distinct students across classes.",
    "Different courses and student cohorts are not directly comparable.",
    "Do not combine these counts with Anteater counts; records may overlap.",
]


class _Page(HTMLParser):
    """Read source data literals only, with no eval or script execution."""

    def __init__(self, html: str):
        super().__init__(convert_charrefs=True)
        self.csrf_token = None
        self.names: list[str] = []
        self.departments: list[str] = []
        self.counts = None
        self.percentages = None
        self.rows = None
        self.last_grade_update = None
        self.source_reported_gpa = None
        self._in_script = False
        self._in_departments = False
        self._scripts: list[str] = []
        self._text: list[str] = []
        self.feed(html)
        self.close()
        for script in self._scripts:
            match = re.search(r"autoSearch\((\[.*?\]),\s*['\"]nameField['\"]", script, re.S)
            if match:
                directory = ast.literal_eval(match[1])
                if (not isinstance(directory, list) or len(directory) > 20000
                        or any(not isinstance(row, dict) or not isinstance(row.get("name"), str)
                               or not row["name"].strip() or len(row["name"]) > 150 for row in directory)):
                    raise ValueError("invalid instructor directory")
                self.names = sorted({row["name"].strip() for row in directory})
                break
        text = " ".join(self._text)
        update = re.search(r"Last Grade Update:\s*(Winter|Spring|Summer|Fall)\s+(\d{4})", text)
        if update:
            self.last_grade_update = f"{update[2]} {update[1]}"
        reported = re.search(r"Total GPA:\s*([^\s|<]+)", text)
        if reported:
            try:
                number = float(reported[1])
            except ValueError:
                number = None
            if _finite_number(number, 0, 4):
                self.source_reported_gpa = number

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "script":
            self._in_script = True
        if tag == "select" and attrs.get("id") == "dep":
            self._in_departments = True
        if tag == "option" and self._in_departments:
            value = attrs.get("value")
            if value and value != "ALL":
                self.departments.append(value)
        if tag == "input" and attrs.get("name") == "csrf_token":
            self.csrf_token = attrs.get("value")
        if attrs.get("id") == "customSwitch1":
            if self.counts is not None:
                raise ValueError("multiple grade distributions")
            match = re.search(r"changeGraph\(\s*(\[[^\]]*\])\s*,\s*(\[[^\]]*\])", attrs.get("onclick", ""))
            if match:
                # The source uses JavaScript nan in an empty result's graph.
                percentages = re.sub(r"\bnan\b", "null", match[1], flags=re.I)
                self.percentages = json.loads(percentages, parse_constant=_reject_constant)
                self.counts = json.loads(match[2], parse_constant=_reject_constant)
        onclick = attrs.get("onclick", "")
        if "modalInfo(" in onclick:
            if self.rows is not None:
                raise ValueError("multiple offering lists")
            self.rows, _ = json.JSONDecoder(parse_constant=_reject_constant).raw_decode(
                onclick.split("modalInfo(", 1)[1].lstrip()
            )

    def handle_endtag(self, tag):
        if tag == "script":
            self._in_script = False
        if tag == "select":
            self._in_departments = False

    def handle_data(self, data):
        (self._scripts if self._in_script else self._text).append(data)


def _reject_constant(value):
    raise ValueError(f"non-finite JSON value: {value}")


def _finite_number(value, minimum=0, maximum=None) -> bool:
    try:
        return (type(value) in (int, float) and math.isfinite(value)
                and value >= minimum and (maximum is None or value <= maximum))
    except OverflowError:
        return False


def _parse_page(html: str) -> _Page:
    if not isinstance(html, str) or len(html.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise ValueError("invalid or oversized source page")
    return _Page(html)


def _name_key(name: str) -> str:
    return re.sub(r"[.\s]+", "", name.upper())


def _normalize_name(name: str) -> str:
    name = " ".join(name.upper().split())
    if "," in name:
        last, first = name.split(",", 1)
        tokens = re.findall(r"[A-Z]+", first)
        if tokens and all(len(token) <= 2 for token in tokens):
            return f"{last.strip()}, {''.join(tokens)}."
    return name


def _resolve_instructor(query: str, names: list[str], department=None) -> tuple[Optional[str], list[str]]:
    """Require a source name or a verified full-name-to-initial match.

    The local RMP lookup may pick another person sharing an initial. Its
    result is accepted only when the complete requested given/surname
    agrees; a conflicting or unverified full name requires clarification.
    """
    query = " ".join(query.upper().split())
    exact = [name for name in names if _name_key(name) == _name_key(query)]
    if len(exact) == 1:
        return exact[0], exact
    full_name = False
    if "," in query:
        last, first = (value.strip() for value in query.split(",", 1))
        tokens = re.findall(r"[A-Z]+", first)
        full_name = any(len(token) > 2 for token in tokens)
        initials = "".join(token[0] if len(token) > 2 else token for token in tokens)
        expected_full_name = f"{first} {last}"
    else:
        # A unique whole surname (including compound surnames) is safe.
        surname_hits = [name for name in names if name.upper().partition(",")[0].strip().rstrip(".") == query.rstrip(".")]
        if surname_hits:
            return (surname_hits[0] if len(surname_hits) == 1 else None), surname_hits
        tokens = query.split()
        last, initials = tokens[-1], "".join(token[0] for token in tokens[:-1])
        full_name = len(tokens) > 1
        expected_full_name = query
    verified_full_name = False
    if full_name:
        from app.data.professors import lookup_professor

        local = lookup_professor(query, department=department)
        local_full_name = " ".join(f"{(local or {}).get('firstName', '')} {(local or {}).get('lastName', '')}".upper().split())
        if local and _name_key(local_full_name) == _name_key(expected_full_name):
            last = str(local["lastName"]).upper()
            initials = "".join(token[0] for token in re.findall(r"[A-Z]+", str(local["firstName"]).upper()))
            verified_full_name = True
    candidates = []
    for name in names:
        source_last, _, source_first = name.upper().partition(",")
        if source_last.strip().rstrip(".") != last.strip().rstrip("."):
            continue
        source_initials = re.sub(r"[^A-Z]", "", source_first)
        if not initials or source_initials == initials:
            candidates.append(name)
    return (candidates[0] if len(candidates) == 1 and (not full_name or verified_full_name) else None), candidates


def _prepare_query(course_id=None, instructor_name=None, department=None) -> dict:
    values = (course_id, instructor_name, department)
    if any(value is not None and not isinstance(value, str) for value in values):
        raise ValueError("query fields must be strings")
    course_id, instructor_name, department = (value.strip() or None if value else None for value in values)
    if not course_id and not instructor_name:
        raise ValueError("provide a course or instructor; unfiltered searches are disabled")
    ref = parse_course_mention(course_id) if course_id else None
    # Reject prose and multiple course IDs rather than silently picking one.
    if course_id:
        mentions = iter_course_mentions(course_id)
        if (not ref or len(mentions) != 1 or mentions[0][1] != 0 or mentions[0][2] != len(course_id)
                or not course_id.upper().endswith(ref.course_number)
                or resolve_department(course_id[:-len(ref.course_number)].strip()) != ref.department):
            raise ValueError("invalid course query")
    dept = resolve_department(department) if department else None
    if department and not dept or ref and dept and ref.department != dept:
        raise ValueError("invalid or conflicting department")
    if instructor_name and (len(instructor_name) > 100 or not re.search(r"[A-Za-z]", instructor_name)
                            or re.search(r"\d|[\x00-\x1f]", instructor_name)
                            or "," in instructor_name and not re.search(r"[A-Za-z]", instructor_name.split(",", 1)[1])
                            or instructor_name.upper() in {"STAFF", "TBA", "UNKNOWN", "UNKNOWN, INSTRUCTOR", "[REDACTED]"}):
        raise ValueError("invalid instructor query")
    return {"course_id": colloquial_course_id(ref.department, ref.course_number) if ref else None,
            "instructor_name": _normalize_name(instructor_name) if instructor_name else None,
            "department": ref.department if ref else dept}


def _scope(query: dict) -> str:
    return "course_instructor" if query.get("course_id") and query.get("instructor_name") else "course" if query.get("course_id") else "instructor"


def _envelope(status: str, query=None, *, coverage=None, reason=None) -> dict:
    query = query or {"course_id": None, "instructor_name": None, "department": None}
    return {"found": status == "ok", "status": status, "source": "zotistics", "source_url": SOURCE_URL,
            "query": deepcopy(query), **query, "resolved_instructor_name": None, "scope": _scope(query),
            "historical_only": True, "coverage": deepcopy(coverage or {}), "limitations": list(_LIMITATIONS),
            "grades": None, "by_instructor": [], "by_course": [], "cached": False, "retrieved_at": None,
            **({"reason": reason} if reason else {})}


def _normalize_evidence(counts, rows, percentages, source_gpa, query: dict, instructor_name=None, coverage=None) -> dict:
    result = _envelope("unavailable", query, coverage=coverage)
    if (not isinstance(counts, list) or len(counts) != 7
            or any(type(count) is not int or count < 0 or count > 100_000_000 for count in counts)
            or not isinstance(rows, list) or len(rows) > 10000
            or not isinstance(percentages, list) or len(percentages) != 7):
        raise ValueError("grade counts or offering metadata missing/invalid")
    result["evidence"] = {"counts": counts, "offerings": rows, "percentages": percentages, "source_reported_gpa": source_gpa}
    result["resolved_instructor_name"] = instructor_name
    if instructor_name:
        result["instructor_name"] = instructor_name
    if not rows and sum(counts) == 0:
        if any(value is not None and not _finite_number(value, 0, 100) for value in percentages):
            raise ValueError("invalid empty-result percentages")
        result.update(status="no_data", reason="No matching historical grade records; small/incomplete classes may be omitted.")
        return result
    if not rows or sum(counts) == 0:
        raise ValueError("inconsistent grade counts and offering metadata")
    if any(not _finite_number(value, 0, 100) for value in percentages):
        raise ValueError("invalid source percentages")
    samples = sum(counts)
    if any(abs(value - round(count / samples * 100, 1)) > 0.051 for value, count in zip(percentages, counts)):
        raise ValueError("source percentages do not match grade counts")
    ref = parse_course_mention(query["course_id"]) if query["course_id"] else None
    terms, instructors, courses, identities = set(), set(), set(), set()
    for row in rows:
        if (not isinstance(row, list) or len(row) != 9
                or type(row[0]) is not int or row[0] < 0
                or row[1] not in _QUARTERS or type(row[2]) is not int or not 1900 <= row[2] <= 2200
                or any(not isinstance(row[index], str) or not row[index].strip() for index in (3, 4, 5, 6, 8))
                or type(row[7]) is not int or row[7] < 0):
            raise ValueError("invalid offering row")
        row_dept = resolve_department(row[3])
        row_course = _prepare_query(course_id=f"{row[3]} {row[4]}")
        if (not row_dept or ref and (row_dept != ref.department or row[4].upper() != ref.course_number)
                or query["department"] and row_dept != query["department"]
                or instructor_name and _name_key(row[6]) != _name_key(instructor_name)):
            raise ValueError("returned grades do not match requested course/instructor/department")
        identity = (row[1], row[2], row_dept, row[4], row[7])
        if identity in identities:
            raise ValueError("duplicate offering row")
        identities.add(identity)
        terms.add((row[2], _QUARTERS[row[1]], row[1]))
        instructors.add(row[6])
        courses.add(row_course["course_id"])
    if source_gpa is not None and not _finite_number(source_gpa, 0, 4):
        raise ValueError("invalid reported GPA")
    ordered = sorted(terms)
    term_range = {"first": f"{ordered[0][0]} {ordered[0][2]}", "last": f"{ordered[-1][0]} {ordered[-1][2]}"}
    term_labels = [f"{year} {quarter}" for year, _, quarter in ordered]
    result["coverage"].update(term_range=term_range, terms=term_labels, class_count=len(rows))
    counts_by_letter = dict(zip(_LETTERS, counts))
    graded = sum(counts[:5])
    pct = {letter: round(counts_by_letter[letter] / graded * 100, 1) if graded else None for letter in _LETTERS[:5]}
    grades = {"course_id": query["course_id"], "instructor_name": instructor_name, "scope": _scope(query),
              "avg_gpa": round(sum(count * point for count, point in zip(counts[:5], (4, 3, 2, 1, 0))) / graded, 3) if graded else None,
              "gpa_is_approximate": True, "gpa_method": "A=4,B=3,C=2,D=1,F=0; no +/-; excludes P/NP",
              "source_reported_gpa": source_gpa, "samples": samples, "graded": graded, "class_count": len(rows),
              "letter_counts": counts_by_letter, "letter_pct": pct,
              "percentage_denominator": "A–F grade records only", "site_percentages": dict(zip(_LETTERS, percentages)),
              "site_percentage_denominator": "A–F plus P/NP grade records", "a_rate": counts[0] / graded if graded else None,
              **{f"pct_{letter}": pct[letter] for letter in _LETTERS[:5]},
              "term_range": term_range, "terms": term_labels, "instructors": sorted(instructors), "courses": sorted(courses),
              "source": "Zotistics", "source_url": SOURCE_URL, "historical_only": True, "coverage": deepcopy(result["coverage"])}
    result.update(status="ok", found=True, grades=grades)
    if instructor_name:
        result["by_instructor"] = [{"instructor": instructor_name, "grades": grades}]
    return result


def _parse_result(html: str, course_id=None, instructor_name=None, department=None, coverage=None) -> dict:
    query = {"course_id": course_id, "instructor_name": instructor_name, "department": department}
    try:
        query = _prepare_query(course_id, instructor_name, department)
        page = _parse_page(html)
        return _normalize_evidence(page.counts, page.rows, page.percentages, page.source_reported_gpa,
                                   query, instructor_name, coverage)
    except (ValueError, TypeError, KeyError, SyntaxError, RecursionError) as error:
        return _envelope("unavailable", query, coverage=coverage, reason=f"Zotistics response could not be verified: {error}")


def _cache_key(query: dict) -> str:
    return hashlib.sha256(json.dumps(query, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()


def _read_cache(key: str, query: Optional[dict] = None) -> Optional[dict]:
    try:
        path = CACHE_DIR / f"{key}.json"
        if path.stat().st_size > MAX_RESPONSE_BYTES:
            return None
        entry = json.loads(path.read_text(encoding="utf-8"), parse_constant=_reject_constant)
        if not isinstance(entry, dict) or entry.get("version") != _CACHE_VERSION:
            return None
        result, expiry = entry.get("result"), entry.get("expires_at")
        if not isinstance(result, dict) or not _finite_number(expiry) or time.time() >= expiry:
            return None
        stored_query = result.get("query")
        if (not isinstance(stored_query, dict) or set(stored_query) != {"course_id", "instructor_name", "department"}
                or _prepare_query(**stored_query) != stored_query or _cache_key(stored_query) != key
                or query is not None and stored_query != query
                or result.get("source") != "zotistics" or result.get("source_url") != SOURCE_URL
                or result.get("status") not in {"ok", "no_data"}
                or type(result.get("found")) is not bool or result["found"] != (result["status"] == "ok")
                or result.get("historical_only") is not True or result.get("scope") != _scope(stored_query)
                or not isinstance(result.get("coverage"), dict) or not isinstance(result.get("evidence"), dict)):
            return None
        matched = result.get("resolved_instructor_name")
        if stored_query["instructor_name"] and (not isinstance(matched, str) or not matched
                                                or result.get("instructor_name") != matched
                                                or _resolve_instructor(stored_query["instructor_name"], [matched], stored_query["department"])[0] != matched):
            return None
        if not stored_query["instructor_name"] and matched is not None:
            return None
        evidence = result["evidence"]
        expected = _normalize_evidence(evidence.get("counts"), evidence.get("offerings"), evidence.get("percentages"),
                                       evidence.get("source_reported_gpa"), stored_query, matched, result["coverage"])
        if (expected["status"] != result["status"] or expected["grades"] != result.get("grades")
                or expected["coverage"] != result["coverage"]
                or expected["by_instructor"] != result.get("by_instructor") or result.get("by_course") != []
                or result.get("course_id") != stored_query["course_id"] or result.get("department") != stored_query["department"]):
            return None
        returned = deepcopy(result)
        returned["cached"] = True
        return returned
    except (OSError, ValueError, TypeError, KeyError, SyntaxError, RecursionError):
        return None


def _write_cache(key: str, result: dict) -> None:
    if result.get("status") not in {"ok", "no_data"}:
        return
    temp = None
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        entry = {"version": _CACHE_VERSION, "expires_at": time.time() + (CACHE_TTL_S if result["found"] else NO_DATA_TTL_S),
                 "result": result}
        with tempfile.NamedTemporaryFile(mode="w", dir=CACHE_DIR, suffix=".tmp", delete=False, encoding="utf-8") as stream:
            temp = Path(stream.name)
            json.dump(entry, stream, ensure_ascii=False, allow_nan=False)
        temp.replace(CACHE_DIR / f"{key}.json")
    except (OSError, ValueError, TypeError) as error:
        logger.warning("Zotistics cache write failed: %s", error)
    finally:
        if temp is not None:
            try:
                temp.unlink(missing_ok=True)
            except OSError:
                pass


def get_cached_query_result(course_id=None, instructor_name=None, department=None) -> dict:
    """Read a verified successful or empty-query result, without I/O writes.

    A fresh no_data result retains the exact pair and source-name evidence
    needed by a caller deciding whether an instructor-wide fallback is valid.
    A cache miss never establishes that the source has no matching grades.
    """
    try:
        query = _prepare_query(course_id, instructor_name, department)
    except ValueError as error:
        return _envelope("invalid_query", reason=str(error))
    cached = _read_cache(_cache_key(query), query)
    if cached is not None:
        return cached
    return _envelope("cache_miss", query, reason="No fresh verified historical grade snapshot for this exact query.")


def get_cached_grade_distribution(course_id=None, instructor_name=None, department=None) -> dict:
    """Read a verified positive snapshot only; never request or write data."""
    result = get_cached_query_result(course_id, instructor_name, department)
    if result["status"] != "no_data":
        return result
    return _envelope("cache_miss", result["query"], reason="No fresh verified positive historical grade snapshot for this exact query.")


def _request(session, method: str, query: dict, data=None) -> str:
    started = observability.now()
    observability.log_agent_web_fetch_started(logger, url=SOURCE_URL, method=method,
        tool="get_zotistics_grades", workflow_id="zotistics", request_params=query)
    response = None
    try:
        response = getattr(session, method.lower())(SOURCE_URL, timeout=REQUEST_TIMEOUT_S, stream=True, allow_redirects=False,
                    **({"data": data} if data is not None else {}))
        response.raise_for_status()
        if response.status_code != 200 or response.url.rstrip("/") != SOURCE_URL.rstrip("/"):
            raise ValueError("unexpected source HTTP status or redirect")
        content_type = response.headers.get("content-type", "")
        if content_type and "text/html" not in content_type.lower():
            raise ValueError("source did not return HTML")
        pieces, total = [], 0
        chunks = response.iter_content(chunk_size=65536) if hasattr(response, "iter_content") else [response.content]
        for chunk in chunks:
            if not chunk:
                continue
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                raise ValueError("source response exceeds size limit")
            pieces.append(chunk)
        html = b"".join(pieces).decode(getattr(response, "encoding", None) or "utf-8")
        observability.log_agent_web_fetch_completed(logger, url=SOURCE_URL, final_url=response.url,
            method=method, tool="get_zotistics_grades", workflow_id="zotistics", status_code=response.status_code,
            content_length=total, content_type=content_type, duration_ms=observability.elapsed_ms(started), request_params=query)
        return html
    except (requests.RequestException, ValueError, UnicodeError, LookupError) as error:
        observability.log_agent_web_fetch_failed(logger, url=SOURCE_URL, method=method,
            tool="get_zotistics_grades", workflow_id="zotistics", duration_ms=observability.elapsed_ms(started),
            error=type(error).__name__, request_params=query)
        raise
    finally:
        if response is not None and hasattr(response, "close"):
            response.close()


def get_grade_distribution(course_id=None, instructor_name=None, department=None) -> dict:
    """Query one course, one professor, or their exact pair across history."""
    try:
        query = _prepare_query(course_id, instructor_name, department)
    except ValueError as error:
        return _envelope("invalid_query", reason=str(error))
    key = _cache_key(query)
    cached = _read_cache(key, query)
    if cached is not None:
        return cached
    result = _envelope("unavailable", query)
    try:
        with requests.Session() as session:
            session.headers.update({"User-Agent": "UCI-Course-Advisor/1.0 (+historical-grade-lookup)"})
            home = _parse_page(_request(session, "GET", query))
            coverage = {"last_grade_update": home.last_grade_update, "coverage_start": "2014 Summer",
                        "excludes_small_or_incomplete_classes": True, "plus_minus_available": False}
            result["coverage"] = coverage
            result["retrieved_at"] = datetime.now(timezone.utc).isoformat()
            if not home.csrf_token:
                raise ValueError("source search form missing CSRF token")
            if not home.departments:
                raise ValueError("source department directory unavailable")
            if query["department"] and query["department"] not in home.departments:
                result.update(status="invalid_query", reason="Requested department is not in the source search form.")
                return result
            matched = None
            if query["instructor_name"]:
                if not home.names:
                    raise ValueError("source instructor directory unavailable")
                matched, candidates = _resolve_instructor(query["instructor_name"], home.names, query["department"])
                if matched is None:
                    result.update(status="ambiguous_instructor" if candidates else "not_found", candidates=candidates[:20],
                        reason="Instructor identity needs clarification; choose an exact source name." if candidates else "Instructor not found in the source directory.")
                    return result
            ref = parse_course_mention(query["course_id"]) if query["course_id"] else None
            form = {"csrf_token": home.csrf_token, "teacher": matched or "", "selectDep": query["department"] or "ALL",
                    "classNum": ref.course_number if ref else "", "code": "", "submit": "Submit"}
            page = _parse_page(_request(session, "POST", query, form))
            result = _normalize_evidence(page.counts, page.rows, page.percentages, page.source_reported_gpa, query, matched, coverage)
    except (requests.RequestException, ValueError, TypeError, KeyError, SyntaxError, RecursionError, UnicodeError, LookupError) as error:
        result.update(status="unavailable", found=False, grades=None,
                      reason=f"Zotistics is temporarily unavailable ({type(error).__name__}); no grading conclusion can be drawn.")
    result.update(retrieved_at=datetime.now(timezone.utc).isoformat(), cached=False)
    _write_cache(key, result)
    return result
