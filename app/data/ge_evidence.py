"""Annual official GE evidence, shared by every course in the same catalogue.

Only a course explicitly listed in a successfully parsed matching edition is
verified. A local CSV label, a missing course, or a failed GET stays unknown.
"""
from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
from html.parser import HTMLParser
import json
import math
from pathlib import Path
import re
import tempfile
import threading
import time
from urllib.parse import parse_qs, urlsplit

import requests

from app.catalog.ge import normalize_ge_category, parse_ge_categories
from app.catalog.normalization import parse_course_mention
from app.terms import parse_term_key

SOURCE = "uci_catalogue"
PARSER_VERSION = 1
GE_PATH = "informationforadmittedstudents/requirementsforabachelorsdegree/"
CURRENT_URL = "https://catalogue.uci.edu/" + GE_PATH
CACHE_DIR = Path(__file__).resolve().parents[2] / "data/runtime/ge_evidence"
CACHE_TTL_S = 24 * 60 * 60
FAILURE_TTL_S = 5 * 60
MAX_HTML_BYTES = 3 * 1024 * 1024
_cache = {}
_lock = threading.Lock()


def academic_year_for_term(term):
    parsed = parse_term_key(term) if isinstance(term, str) else None
    if not parsed or parsed.kind != "single":
        return None
    target = parsed.terms[0]
    start = target.year if target.quarter == "Fall" else target.year - 1
    return f"{start}-{str(start + 1)[-2:]}"


def _course(value):
    ref = parse_course_mention(value.replace("_", " ")) if isinstance(value, str) else None
    return ref.display() if ref else None


def _official_url(value):
    try:
        parsed = urlsplit(value)
    except (TypeError, ValueError):
        return False
    return (parsed.scheme == "https" and parsed.netloc == "catalogue.uci.edu"
            and bool(re.fullmatch(r"/(?:previouseditions/20\d{2}-\d{2}/)?" + re.escape(GE_PATH), parsed.path)))


def verified_ge_provenance(provenance, term):
    """A server field named verified alone does not authenticate old CSV tags."""
    source = provenance if isinstance(provenance, Mapping) else {}
    year = academic_year_for_term(term)
    archive = re.search(r"/previouseditions/(20\d{2}-\d{2})/", str(source.get("source_url") or ""))
    return bool(year and source.get("source") == SOURCE and source.get("status") == "verified"
                and source.get("catalogue_year") == year and _official_url(source.get("source_url"))
                and (not archive or archive[1] == year)
                and source.get("parser_version") == PARSER_VERSION
                and isinstance(source.get("content_sha256"), str)
                and re.fullmatch(r"[0-9a-f]{64}", source["content_sha256"])
                and isinstance(source.get("retrieved_at"), str) and source["retrieved_at"])


class _GEListParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.heading = None
        self.heading_text = []
        self.category = None
        self.table_depth = 0
        self.categories = set()
        self.courses = defaultdict(set)

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag in {"h4", "h5"}:
            self.heading, self.heading_text = tag, []
        if tag == "table":
            self.table_depth += 1
        if tag == "a" and self.table_depth and self.category and "code" in attrs.get("class", "").split():
            query = parse_qs(urlsplit(attrs.get("href", "")).query)
            course = _course((query.get("P") or [""])[0])
            if course:
                self.courses[course].add(self.category)

    def handle_data(self, data):
        if self.heading:
            self.heading_text.append(data)

    def handle_endtag(self, tag):
        if tag == self.heading:
            text = "".join(self.heading_text).strip()
            match = re.match(r"^([IV]+[ab]?)\.\s", text, re.I)
            self.category = normalize_ge_category(match[1]) if match else None
            if self.category:
                self.categories.add(self.category)
            self.heading = None
        if tag == "table":
            self.table_depth = max(0, self.table_depth - 1)


def parse_official_ge_html(html, *, source_url, retrieved_at=None):
    """Parse the full unmodified official page; malformed pages fail closed."""
    if not _official_url(source_url) or not isinstance(html, str):
        return {"status": "unknown", "reason": "official_source_unverified"}
    edition = re.search(r"\b(20\d{2}-\d{2})\s+Edition\b", html)
    parser = _GEListParser()
    parser.feed(html)
    needed = {"II", "III", "IV", "VA", "VB", "VI", "VII", "VIII"}
    observed = {category for values in parser.courses.values() for category in values}
    if not edition or not needed.issubset(parser.categories & observed) or len(parser.courses) < 100:
        return {"status": "unknown", "reason": "official_ge_page_incomplete"}
    return {"status": "verified", "catalogue_year": edition[1],
            "courses": {course: sorted(labels) for course, labels in parser.courses.items()},
            "source": {"source": SOURCE, "status": "verified", "source_url": source_url,
                       "catalogue_year": edition[1], "retrieved_at": retrieved_at or datetime.now(timezone.utc).isoformat(),
                       "parser_version": PARSER_VERSION,
                       "content_sha256": hashlib.sha256(html.encode("utf-8")).hexdigest()}}


def _get_html(url):
    # Redirects are not followed: source approval applies only to this host.
    with requests.get(url, timeout=(4, 12), allow_redirects=False, stream=True,
                      headers={"User-Agent": "Solon-GE-Evidence/1.0"}) as response:
        response.raise_for_status()
        if response.status_code != 200:
            raise ValueError("unexpected official response")
        parts, length = [], 0
        for part in response.iter_content(65536):
            length += len(part)
            if length > MAX_HTML_BYTES:
                raise ValueError("official page exceeds limit")
            parts.append(part)
    return b"".join(parts).decode("utf-8")


def _save(year, entry):
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(mode="w", dir=CACHE_DIR, suffix=".tmp", delete=False, encoding="utf-8") as stream:
            json.dump(entry, stream, ensure_ascii=False)
            temp = Path(stream.name)
        temp.replace(CACHE_DIR / f"{year}.json")
    except OSError:
        pass  # The in-memory evidence remains useful on read-only deployments.


def _annual_evidence(year, allow_network):
    # One full GE list serves all departments/courses; lock prevents fan-out.
    with _lock:
        entry = _cache.get(year)
        in_memory = entry is not None
        if entry is None:
            try:
                entry = json.loads((CACHE_DIR / f"{year}.json").read_text())
            except (OSError, ValueError, TypeError):
                entry = None
        expiry = entry.get("expires_at") if isinstance(entry, dict) else None
        fresh = isinstance(expiry, (float, int)) and not isinstance(expiry, bool) and math.isfinite(expiry) and expiry > time.time()
        if isinstance(entry, dict) and entry.get("parser_version") == PARSER_VERSION and fresh:
            result = entry.get("result")
            if isinstance(result, dict) and result.get("status") == "verified":
                if in_memory and result.get("catalogue_year") == year:
                    return deepcopy(result)  # Already parsed and checked in this process.
                source = result.get("source") or {}
                snapshot = entry.get("source_html")
                if isinstance(snapshot, str):
                    parsed = parse_official_ge_html(snapshot, source_url=source.get("source_url"),
                                                    retrieved_at=source.get("retrieved_at"))
                    if parsed.get("catalogue_year") == year and parsed == result:
                        _cache[year] = entry
                        return deepcopy(result)
            elif isinstance(result, dict) and result.get("status") == "unknown":
                _cache[year] = entry
                return deepcopy(result)
        if not allow_network:
            return {"status": "unknown", "reason": "official_ge_evidence_unavailable"}
        result, html = {"status": "unknown", "reason": "official_ge_evidence_unavailable"}, None
        for url in (CURRENT_URL, f"https://catalogue.uci.edu/previouseditions/{year}/{GE_PATH}"):
            try:
                fetched = _get_html(url)
                parsed = parse_official_ge_html(fetched, source_url=url)
                if parsed.get("catalogue_year") == year and parsed.get("status") == "verified":
                    result, html = parsed, fetched
                    break
                result = {"status": "unknown", "reason": "catalogue_year_unverified"}
                if parsed.get("catalogue_year", "") < year and parsed.get("status") == "verified":
                    break  # A future edition cannot exist in the archive yet.
            except (requests.RequestException, ValueError, UnicodeError):
                continue
        entry = {"parser_version": PARSER_VERSION, "expires_at": time.time() + (
            CACHE_TTL_S if result["status"] == "verified" else FAILURE_TTL_S), "result": result}
        # Preserve exact public text and metadata, never cookies or headers.
        if html is not None:
            entry["source_html"] = html
        _cache[year] = entry
        _save(year, entry)
        return deepcopy(result)


def get_course_ge_evidence(course_id, term, *, allow_network=True):
    course, year = _course(course_id), academic_year_for_term(term)
    base = {"status": "unknown", "categories": [], "course_id": course,
            "term": term, "catalogue_year": year, "source": None}
    if not course or not year:
        return {**base, "reason": "course_or_term_unverified"}
    annual = _annual_evidence(year, allow_network)
    if annual.get("status") != "verified":
        return {**base, "reason": annual.get("reason", "official_ge_evidence_unavailable")}
    labels = annual.get("courses", {}).get(course)
    if not labels:
        return {**base, "reason": "course_not_listed_in_official_ge_evidence"}
    return {**base, "status": "verified", "categories": labels,
            "source": annual["source"], "reason": None}


def enrich_course_ge_evidence(course, term, *, allow_network=True):
    """Return target-term fields for adapters, without mutating local metadata."""
    course = course if isinstance(course, Mapping) else {}
    cid = _course(course.get("course_id"))
    local = parse_ge_categories(course.get("ge_categories", course.get("ge_list")))
    provenance = course.get("ge_provenance") or {}
    if (course.get("ge_labels_verified") is True and verified_ge_provenance(provenance, term)
            and cid is not None and _course(course.get("ge_evidence_course_id")) == cid
            and academic_year_for_term(course.get("ge_evidence_term")) == academic_year_for_term(term)
            and not local["unrecognized"]):
        official = {"status": "verified", "categories": local["categories"], "source": provenance}
    else:
        official = get_course_ge_evidence(cid, term, allow_network=allow_network)
    verified = official["status"] == "verified"
    source = official.get("source") if verified else {
        "source": "local_catalogue", "status": "unknown", "catalogue_year": None,
        "reason": official.get("reason"), "raw_labels": deepcopy(course.get("ge_categories", course.get("ge_list"))),
        "unrecognized_labels": local["unrecognized"]}
    return {"ge_categories": official["categories"] if verified else local["categories"],
            "ge_labels_verified": verified, "ge_provenance": deepcopy(source),
            "ge_evidence_course_id": cid, "ge_evidence_term": term}
