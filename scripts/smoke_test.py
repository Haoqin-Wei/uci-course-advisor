"""
Smoke test — runs the full Phase 1 validation pipeline on a synthetic
LLM answer to confirm everything wires up.

Run AFTER scripts/import_term_data.py has populated data/uci/*.csv:

    python scripts/import_term_data.py path/to/spring_sections_relational.xlsx
    python scripts/smoke_test.py
"""

try:
    from scripts._bootstrap import ensure_repo_root_on_path
except ModuleNotFoundError:  # direct execution: python scripts/smoke_test.py
    from _bootstrap import ensure_repo_root_on_path

ensure_repo_root_on_path()

from app.catalog.term import Term
from app.catalog.cache import get_catalog
from app.catalog.normalization import parse_course_mention
from app.validation.types import ValidationContext
from app.validation.orchestrator import validate
from app.validation.policies import decide_action
from app.validation.apply import apply_report


def run():
    term = Term(year=2025, quarter="Spring")
    catalog = get_catalog(term)
    if catalog is None:
        print("❌ No catalog data for", term.term_id)
        print("   Did you run: python scripts/import_term_data.py <xlsx>?")
        return

    print(f"✅ Loaded catalog for {term.display()}")
    print(f"   {len(catalog.all_course_refs())} unique courses")

    # ── Sanity check: parse + lookup ──
    for s in ["COMPSCI 122B", "compsci 122b", "ICS33", "cs 999", "FAKEDEPT 100"]:
        ref = parse_course_mention(s)
        exists = catalog.course_exists(ref) if ref else None
        print(f"   parse({s!r:24}) → {str(ref):28} exists={exists}")

    # ── Synthetic answer with a mix of good + bad mentions ──
    answer = (
        "Based on your profile I'd recommend **COMPSCI 122B**, taught by "
        "Professor Herold — it's a solid databases course. You might also "
        "consider **COMPSCI 999** if you've completed all prereqs, and "
        "**MATH 2D** is offered as well. Avoid **FAKEDEPT 100** this term.\n\n"
        "Some students also like Professor Nonexistent's section, but I'd "
        "skip that one."
    )
    retrieved = {
        "primary": [{"course": {"course_id": "COMPSCI 122B"}}],
        "flagged": [],
        "total_found": 1,
    }

    ctx = ValidationContext(
        llm_answer=answer,
        retrieved=retrieved,
        catalog=catalog,
        session_state={"major": "Computer Science"},
        user_message="recommend a databases course",
    )

    report = validate(ctx)
    action = decide_action(report)
    final_answer, _, changed = apply_report(answer, [], report, action)

    print(f"\nValidation overall: {report.overall}")
    print(f"  errors:   {len(report.errors)}")
    print(f"  warnings: {len(report.warnings)}")
    print(f"  infos:    {len(report.infos)}")
    for i in report.issues:
        print(f"  - [{i.severity.value:5s}] {i.code}: {i.message}")
    print(f"\nAction applied: {action.value} (answer_modified={changed})")
    print(f"\n--- Final answer ---\n{final_answer}")


if __name__ == "__main__":
    run()
