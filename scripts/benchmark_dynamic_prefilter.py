"""Offline fixture benchmark; does not measure production/real-LLM quality."""
from __future__ import annotations

import json
from pathlib import Path
import statistics
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.recommendations.filter_plan import empty_filter_plan
from app.recommendations.prefilter import apply_dynamic_prefilter


def candidate(number, start="11:00", *, labels=None, **evidence):
    return {"course_id": f"COMPSCI {number}", "term": "2026 Fall",
            "ge_categories": ["III"] if labels is None else labels, "ge_labels_verified": True,
            "prerequisite_status": "met", "requires_secondary": False,
            "sections": [{"section_code": str(10000 + number), "section_num": "A", "section_type": "Lec",
                          "days": "MW", "start_time": start, "end_time": "12:00",
                          "instructors": ["SYNTHETIC, A."]}], **evidence}


def run():
    normal = empty_filter_plan()
    normal["goal"] = "gpa_optimization"
    hard = empty_filter_plan()
    hard["hard_filters"].update(earliest_start="10:00", earliest_start_is_hard=True)
    strict_ge = empty_filter_plan()
    strict_ge["hard_filters"]["only_unmet_ge"] = True
    audit = {"ge_audit": {"verified": True, "authorized": True, "source": "registrar_degree_audit",
                          "unmet_categories": ["II"], "projected_categories": []}}
    cases = [
        ([candidate(1)], normal, audit, {"COMPSCI 1"}),
        ([candidate(2, "08:00")], normal, {}, {"COMPSCI 2"}),
        ([candidate(3, "08:00"), candidate(4)], hard, {}, {"COMPSCI 4"}),
        ([candidate(5, labels=["III", "II"]), candidate(6, labels=["III"])], strict_ge, audit, {"COMPSCI 5"}),
        ([candidate(7)], strict_ge, {}, {"COMPSCI 7"}),
        ([candidate(8, prerequisite_status="unknown")], normal, {}, {"COMPSCI 8"}),
        ([candidate(9, offering_status="not_offered", authoritative=True)], normal, {}, set()),
        ([candidate(10)], normal, {"completed_courses": ["CS10"]}, {"COMPSCI 10"}),
    ]
    false_exclusions = unexpected_keeps = expected_count = actual_count = 0
    for rows, plan, profile, expected in cases:
        result = apply_dynamic_prefilter(rows, plan, profile)
        actual = {row["course_id"] for row in result["kept"]}
        false_exclusions += len(expected - actual)
        unexpected_keeps += len(actual - expected)
        expected_count += len(expected)
        actual_count += len(actual)

    # All 35 IDs remain eligible for retrieval; the last one alone satisfies
    # the supplied hard time rule. No first-30 cutoff is applied by the engine.
    rows = [candidate(n, "08:00" if n < 35 else "11:00") for n in range(1, 36)]
    timings = []
    for _ in range(100):
        started = time.perf_counter()
        result = apply_dynamic_prefilter(rows, hard, {})
        timings.append((time.perf_counter() - started) * 1000)

    # Actual synthetic function calls, rather than guessed external API counts.
    def load_details(candidates):
        calls = []
        def grade_lookup(course_id):
            calls.append(("grades", course_id))
        def rating_lookup(course_id):
            calls.append(("rating", course_id))
        for row in candidates:
            grade_lookup(row["course_id"])
            rating_lookup(row["course_id"])
        return calls

    old_calls, new_calls = load_details(rows), load_details(result["kept"])
    output = {
        "scope": "synthetic pure-rule fixtures; not production, not a real LLM evaluation",
        "labelled_cases": len(cases), "expected_kept": expected_count, "actual_kept": actual_count,
        "false_exclusions": false_exclusions, "unexpected_keeps": unexpected_keeps,
        "candidate_recall": (expected_count - false_exclusions) / expected_count,
        "hard_time_case": {"before": len(rows), "after": len(result["kept"]),
                           "kept_ids": [row["course_id"] for row in result["kept"]],
                           "unfiltered_synthetic_detail_calls": len(old_calls),
                           "prefiltered_synthetic_detail_calls": len(new_calls)},
        "filter_ms_35_candidates_100_runs": {"median": round(statistics.median(timings), 3),
                                             "p95": round(sorted(timings)[94], 3)},
        "network_calls": 0, "llm_calls": 0,
    }
    print(json.dumps(output, ensure_ascii=False, indent=2))
    if false_exclusions or unexpected_keeps or len(result["kept"]) != 1:
        raise SystemExit("Synthetic expectation mismatch")


if __name__ == "__main__":
    run()
