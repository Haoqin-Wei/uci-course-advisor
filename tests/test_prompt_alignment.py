"""Offline contracts for the approved recommendation prompt alignment."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.agent import loop
from app.llm import adapter
from app.llm.safety import SYSTEM_SAFETY_POLICY
from app.response_language import language_instruction


ROOT = Path(__file__).resolve().parents[1]


def _block(start: str, end: str) -> str:
    text = adapter.AGENT_SYSTEM_PROMPT.split(start, 1)[1].split(end, 1)[0]
    return " ".join(text.split())


@pytest.mark.parametrize("prefilter_enabled", [False, True])
@pytest.mark.parametrize("ranking_enabled", [False, True])
def test_agent_receives_independent_flag_modes_in_system_context(
    monkeypatch, prefilter_enabled, ranking_enabled,
):
    """Both mixed modes reach the real adapter entry without changing term authority."""
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", str(prefilter_enabled).lower())
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", str(ranking_enabled).lower())
    monkeypatch.setattr(adapter, "LLM_ENABLED", True)
    monkeypatch.setattr(adapter, "_get_client", lambda: object())
    captured = {}

    async def capture(messages, **kwargs):
        captured.update(messages=messages, kwargs=kwargs)
        yield {"type": "final", "text": "offline"}

    monkeypatch.setattr(loop, "run_agent", capture)
    state = {
        "current_term": "2026 Fall", "default_term": "2027 Winter",
        "query_terms": ["2026 Fall"], "query_term_source": "explicit",
    }

    async def collect():
        return [event async for event in adapter.stream_agent_response(
            "推荐本学期的简单GE课程", state, user_id="prompt-fixture",
            term="2026 Fall", recent_turns=[],
        )]

    assert asyncio.run(collect()) == [{"type": "final", "text": "offline"}]
    system = [message["content"] for message in captured["messages"] if message["role"] == "system"]
    assert len(system) == 1
    prompt = system[0]
    assert prompt.startswith(SYSTEM_SAFETY_POLICY)
    assert prompt.count(SYSTEM_SAFETY_POLICY) == 1
    assert prompt.endswith(language_instruction("zh"))
    runtime = prompt.split("# Recommendation pipeline configuration (server-owned)", 1)[1]
    assert f"DYNAMIC_PREFILTER_ENABLED={str(prefilter_enabled).lower()}" in runtime
    assert f"RECOMMENDATION_RANKING_ENABLED={str(ranking_enabled).lower()}" in runtime
    if prefilter_enabled:
        assert "retained candidates and retained sections before detailed" in runtime
        assert "does not establish hard-condition compliance" in runtime
        assert "Dynamic prefilter is disabled" not in runtime
    else:
        assert "Dynamic prefilter is disabled" in runtime
        assert "Do not claim a FilterPlan was validated" in runtime
    if ranking_enabled:
        assert "Use returned ranking.final_score and its primary section/instructor" in runtime
        assert "Missing is unknown, not zero" in runtime
        assert "Fixed Python ranking is disabled" not in runtime
    else:
        assert "Fixed Python ranking is disabled" in runtime
        assert "without claiming a Python score" in runtime
    assert captured["kwargs"]["allowed_query_terms"] == ["2026 Fall"]
    assert captured["kwargs"]["default_term"] == "2027 Winter"
    assert state["query_terms"] == ["2026 Fall"]


def test_runtime_flags_default_to_legacy_modes(monkeypatch):
    monkeypatch.delenv("DYNAMIC_PREFILTER_ENABLED", raising=False)
    monkeypatch.delenv("RECOMMENDATION_RANKING_ENABLED", raising=False)
    runtime = adapter._recommendation_runtime_instructions()
    assert "DYNAMIC_PREFILTER_ENABLED=false" in runtime
    assert "RECOMMENDATION_RANKING_ENABLED=false" in runtime
    assert "existing search and evidence workflow" in runtime


def test_ge_example_preserves_term_scope_and_separates_unmet_audit():
    example = _block('## Example flow for "What are some easy GE courses?"', "# Term resolution")
    assert "query_terms" in example
    assert 'ge_category="II"' not in example
    assert "Spring 2026" not in example
    assert "GE-only does not mean only unmet GE" in example
    assert "completed-category mentions alone do not exclude courses" in example
    ordering = _block("## Recommendation ordering", "## What to pass")
    assert "only_unmet_ge`; it defaults to false" in ordering
    assert "only a trusted audit" in ordering
    assert "projected credit separate from completion" in ordering
    assert "Neither flag activates the other" in ordering


def test_expired_term_keeps_query_and_distinguishes_actual_deadlines():
    expired = _block("## Expired-term refusal", "## Lec + Dis/Lab pairing")
    assert "DO NOT keep retrying for the same term" in expired
    assert "Do not automatically retry with another term" in expired
    assert "even if a tool suggests pivoting" in expired
    assert "Fall → Winter → Spring → Fall" in expired
    assert "Summer is unsupported" in expired
    assert "deadline_passed" in expired
    assert "returned actual deadlines" in expired
    assert "Spring → Summer" not in adapter.AGENT_SYSTEM_PROMPT
    assert "After that the schedule is locked" not in adapter.AGENT_SYSTEM_PROMPT


def test_no_sections_is_not_authoritative_no_offering():
    skipped = _block("When you see `skipped_count > 0`:", "## Enrollment restrictions")
    assert "no sections in <term>" in skipped
    assert "this alone does not prove not_offered" in skipped
    assert "Say not offered only with authoritative not_offered evidence" in skipped
    assert "course doesn't run that term" not in skipped


def test_verified_titles_and_grade_outcomes_do_not_require_unrelated_tools():
    prompt = adapter.AGENT_SYSTEM_PROMPT
    assert "including search/prefilter metadata" in prompt
    assert "Call `get_course_info` only for a missing field" in prompt
    assert "Historical grade outcomes do not require an RMP rating" in prompt
    assert 'anything beyond "X teaches this section"' not in prompt
    assert "REQUIRES a prior `summarize_professor_reviews` call" in prompt
    assert "A course-wide average is not an individual professor's average" in prompt


def test_zotistics_numbers_keep_estimate_record_and_term_denominators_separate():
    grades = _block("# Zotistics historical grading evidence", "# Tools")
    assert "avg_gpa` is an estimate" in grades
    assert "without +/-; label it as approximate" in grades
    assert "source_reported_gpa" in grades
    assert "unverified calculation method" in grades
    assert "class_count` counts class records, not distinct terms" in grades
    assert "Count distinct terms only from returned `coverage.terms`" in grades
    assert "all-history aggregates do not establish a particular term's grades" in grades
    assert "Keep `graded`, total `samples` and class counts distinct" in grades


def test_cross_course_fallback_is_not_a_same_course_section_a_rate_gap():
    scopes = _block("## Comparable grade scopes", "# Tools")
    assert "same course and the same percentage denominator" in scopes
    assert "Instructor-wide fallback remains cross-course evidence" in scopes
    assert "even when originally requested for that course" in scopes
    assert "must not subtract them into a same-course section gap" in scopes
    wrong, right = scopes.split("- WRONG:", 1)[1].split("- RIGHT:", 1)
    assert "Olson 59.3% vs Egan 20.3%" in wrong
    assert "差约 40 个百分点" in wrong
    assert "59.3% 是跨课程历史汇总" in right
    assert "20.3% 是本课历史记录" in right
    assert "不能据此计算这门课两个班的 A 率差" in right


def test_actual_staged_primary_cannot_be_replaced_by_a_preferred_alternative_in_prose():
    staged = _block("## Staged primary and alternatives", "**Acceptable order**")
    assert "actual primary_code, section, instructor and meeting time" in staged
    assert "alternative section from search or section_groups is not the staged primary" in staged
    assert "unless an updated card confirms that change" in staged
    assert "a staged card is not proof of enrollment" in staged
    wrong, right = staged.split("- WRONG:", 1)[1].split("- RIGHT:", 1)
    assert "已为你选 B 班 11:00" in wrong
    assert "actual card stages A at 09:30" in wrong
    assert "当前卡片是 A 班 09:30" in right
    assert "B 班 11:00 只是已核实的备选" in right
    assert "尚未替换卡片班次" in right


def test_unknown_secondary_pairing_never_establishes_lecture_only_registration():
    pairing = _block("## Lec + Dis/Lab pairing", "## Prose around the tool call")
    assert "An empty `requires_secondary` list alone does not establish Lec-only" in pairing
    assert "secondary_pairing_unknown" in pairing
    assert 'Do not say "no Dis", "纯 Lec", "无需讨论课", or "只输一个 code"' in pairing
    assert "no remaining pairing uncertainty" in pairing
    wrong, right = pairing.split("- WRONG:", 1)[1].split("- RIGHT:", 1)
    assert "配对信息未知" in wrong and "只输一个 code 就行" in wrong
    assert "Dis/Lab 的配对要求仍待核实" in right
    assert "暂不能确认只输入讲座代码即可注册" in right


def test_current_discussion_policy_uses_complete_official_facts_without_claiming_a_registrar_mandate():
    pairing = _block("## Lec + Dis/Lab pairing", "## Prose around the tool call")
    for text in ("user_confirmed_product_policy", "policy_id=user_confirmed_discussion_presence",
                 "policy_version=1", "user_confirmed=true", "complete, unfiltered official",
                 "If that timetable lists Dis", "if it lists no Dis", "related courses' target-term timetables",
                 "required_course_components", "needs_lookup", "university-wide Registrar mandate",
                 "every required Dis/other component", "TBA is unknown meeting time", "ON LINE",
                 "does not establish asynchronous teaching"):
        assert text in pairing
    assert "Observed Lec/Dis coexistence, section letters, an empty list" not in pairing
    assert "Claim no pairing only from verified self-contained enrollment evidence" not in pairing


def test_single_cta_and_real_recommendation_cards_are_preserved():
    prompt = adapter.AGENT_SYSTEM_PROMPT
    assert "2–3 follow-up questions" not in prompt
    assert "At most one CTA; do not force a follow-up" in prompt
    assert "The tool call is non-negotiable" in prompt
    assert "no post-generation fact rewriter" in prompt
    assert "Do not fetch ratings, grades or meeting times unless the user asks" in prompt


def test_documented_prompt_is_exact_active_safety_wrapped_base():
    assert (ROOT / "docs/current-agent-system-prompt.txt").read_text() == adapter.AGENT_SYSTEM_PROMPT
    assert adapter.get_default_agent_prompt() == adapter.AGENT_SYSTEM_PROMPT
