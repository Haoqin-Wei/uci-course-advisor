"""Course offering comparisons with separately labelled historical evidence."""
from __future__ import annotations

import asyncio

from app.data import db
from app.terms.models import TermKey
from app.terms.parser import parse_term_key
from app.terms.service import get_term_resolution_service


def summarize_offering(raw: dict, term: str, *, future: bool = False) -> dict:
    sections = raw.get("sections") or []
    evidence = raw if raw.get("source") in {"db", "api", "registrar_websoc"} else (raw.get("registrar_websoc") or raw)
    if sections:
        status = "offered"
    elif future and evidence.get("error_code") == "websoc_term_unavailable":
        status = "unpublished"
    elif raw.get("authoritative") and raw.get("offering_status") == "not_offered":
        status = "not_offered"
    else:
        status = "unavailable"
    # Lecture/seminar instructors are the professors; discussion instructors
    # must not silently become the course's professor when a lecture is TBA.
    primaries = [s for s in sections if str(s.get("section_type") or "").lower().startswith(("lec", "sem"))]
    instructors = []
    pending = False
    for section in primaries or sections:
        names = section.get("instructors") or []
        if isinstance(names, str):
            names = [names]
        if not names:
            pending = True
        for name in names:
            if not isinstance(name, str):
                continue
            name = name.strip()
            if name.upper() in {"", "STAFF", "TBA", "TBD", "UNKNOWN"}:
                pending = True
            elif name not in instructors:
                instructors.append(name)
    return {
        "term": term, "offering_status": status, "instructors": instructors,
        "instructors_pending": pending if sections else False,
        "source": raw.get("source"), "source_url": evidence.get("source_url"),
        "retrieved_at": evidence.get("retrieved_at"),
        "checked_at": raw.get("checked_at"),
        "authoritative": raw.get("authoritative") is True,
        "error_code": raw.get("error_code") or evidence.get("error_code"),
        "lookup_attempts": raw.get("lookup_attempts") or [],
        "coverage_status": raw.get("coverage_status"),
        "reason": raw.get("reason") or evidence.get("message"),
    }


async def get_course_offerings(course_id: str, terms: list[str], *, service=None) -> dict:
    service = service or get_term_resolution_service()
    limit = asyncio.Semaphore(3)
    cache: dict[str, asyncio.Task] = {}

    async def fetch(term: str) -> dict:
        async with limit:
            try:
                raw = await asyncio.to_thread(db.get_sections, course_id, term)
            except Exception as exc:
                raw = {"source": "none", "reason": type(exc).__name__,
                       "error_code": type(exc).__name__, "sections": []}
        key = parse_term_key(term).terms[0]
        return summarize_offering(raw, term, future=service.is_future(key))

    async def cached(term: str) -> dict:
        if term not in cache:
            cache[term] = asyncio.create_task(fetch(term))
        return dict(await cache[term])

    rows = await asyncio.gather(*(cached(term) for term in terms))
    for row in rows:
        if row["offering_status"] != "unpublished":
            continue
        target = parse_term_key(row["term"]).terms[0]
        references = [TermKey(target.year - offset, target.quarter).canonical_name for offset in (2, 1)]
        history = await asyncio.gather(*(cached(term) for term in references))
        observed = sum(item["offering_status"] == "offered" for item in history)
        row["historical_reference"] = history
        row["prediction"] = {
            "status": "possibly_offered" if observed else "insufficient_evidence",
            "offered_in_years": observed,
            "years_checked": 2,
            "professor_prediction": None,
            "basis": "historical_same_quarter_only",
        }
    return {"course_id": course_id, "query_terms": terms, "offerings": rows,
            "comparison_fields": ["offering_status", "instructors"],
            "historical_reference_does_not_change_target": True}
