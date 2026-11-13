"""Reverse discovery of course codes mentioned in target-term course notices.

The cache stores source comments, not inferred equivalence or enrollment rules.
"""
from collections import OrderedDict
from copy import deepcopy
import threading
import time

from app.catalog.normalization import iter_course_mentions, parse_course_mention
from app.data import anteater
from app.data.websoc_payload import course_comment_context, compact_comment_contexts
from app.terms import parse_term_key

_cache = OrderedDict()
_lock = threading.Lock()


def find_related_notices(course_id, term):
    ref = parse_course_mention(course_id)
    parsed = parse_term_key(term)
    if not ref or parsed.kind != "single" or not parsed.terms[0].is_regular:
        return {"status": "unavailable", "reason": "invalid course or term"}
    target = parsed.terms[0]
    key = target.canonical_name
    with _lock:
        cached = _cache.get(key)
        if cached and time.monotonic() - cached[0] < 300:
            notices = cached[1]
        else:
            # No department/number filter: renumbering may change both.
            try:
                body = anteater._get_json(anteater.WEBSOC_URL, params={
                    "year": str(target.year), "quarter": target.quarter,
                }, audit_tool="course_notice_discovery")
            except Exception:
                body = None
            data = (body or {}).get("data")
            if not body or body.get("ok") is not True or not isinstance(data, dict) or not isinstance(data.get("schools"), list):
                return {"status": "unavailable", "term": key,
                        "reason": "target-term comment discovery unavailable; no identity conclusion"}
            notices = []
            for school in data["schools"]:
                for department in school.get("departments") or []:
                    for course in department.get("courses") or []:
                        text = course.get("courseComment")
                        if isinstance(text, str) and text.strip():
                            mentions = {r.display() for r, _, _ in iter_course_mentions(text)}
                            notices.append((mentions, {
                                "course_id": course.get("courseId"),
                                "comments": course_comment_context(school, department, course),
                            }))
            _cache[key] = (time.monotonic(), notices)
            _cache.move_to_end(key)
            while len(_cache) > 2:
                _cache.popitem(last=False)
    matches = [deepcopy(item) for mentions, item in notices
               if ref.display() in mentions and parse_course_mention(item["course_id"] or "") != ref]
    return compact_comment_contexts({
        "status": "candidate_notices" if matches else "no_matching_notice",
        "source": "live_anteater_websoc", "term": key, "requested_course_id": ref.display(),
        "scope": "target_term_course_comments_all_departments",
        "interpretation": "References are candidates, not automatic equivalence. Read the explicit notice; verify any new code in the SAME term. No match does not establish that no rename exists.",
        "candidates": matches,
    })


def attach_related_notices(result, course_id, term):
    # A course-level notice may survive even with zero sections. Read that
    # existing evidence before doing another network lookup.
    if any((context.get("course") or {}).get("comment")
           for context in (result.get("comment_contexts") or {}).values()):
        return result
    if (result.get("offering_status") == "not_offered" or
            (result.get("source") == "live_anteater_websoc" and result.get("is_live") is True
             and result.get("found") is False and not result.get("sections"))):
        result = deepcopy(result)
        result["course_code_scope"] = "No offering under this code; does not rule out a renamed course."
        result["related_course_notices"] = find_related_notices(course_id, term)
    return result
