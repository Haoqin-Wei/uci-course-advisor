"""Production fetch→db→prefilter→card→schedule using captured public responses.

Only transport and unrelated grade/RMP/profile services are replaced. Neither
GE verification nor pairing dictionaries are manually injected.
"""
from copy import deepcopy
from datetime import datetime
import json
from pathlib import Path

import pytest

from app.agent import prefilter, tools
from app.catalog.loaders.uci_relational import UCIRelationalLoader
from app.catalog.term import Term
from app.data import db, ge_evidence, policies, websoc_workflow
from app.recommendations.filter_plan import empty_filter_plan
from app.recommendations.pairing import pairing_for_primary, verify_selection
from app.recommendations.presentation import normalize_recommendation_card, render_recommendation_answer
from app.scheduling.service import build_pending_schedule_bundle_items, validate_schedule_bundle
from tests.test_zotistics_cards import render

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def source_pipeline(monkeypatch):
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    calls = []
    capture_clock = [None]
    def raw(stem):
        folder = FIXTURES / "websoc/pairing"
        meta = json.loads((folder / f"{stem}.json").read_text())
        content = (folder / f"{stem}.html").read_bytes()
        return content, meta
    class Response:
        status_code = 200
        url = websoc_workflow.WEBSOC_URL
        headers = {"content-type": "text/html"}
        def __init__(self, stem):
            self.content, meta = raw(stem)
            self.text = self.content.decode(meta["response"]["encoding"])
        def raise_for_status(self):
            pass
    class Session:
        def get(self, url, **kwargs):
            calls.append(("GET", url))
            return Response("search_form_2026_10_20")
        def post(self, url, *, data, **kwargs):
            calls.append(("POST", deepcopy(data)))
            stem = {("SOCIOL", "1"): "sociol_1_2026_fall", ("PHILOS", "3"): "philos_3_2026_fall",
                    ("DRAMA", "10"): "drama_10_2026_fall", ("ECON", "13"): "econ_13_2026_fall"}[(data["Dept"], data["CourseNum"])]
            return Response(stem)
    monkeypatch.setattr(websoc_workflow.requests, "Session", Session)
    # Transport replay must show the original capture time, not pretend the
    # recorded HTML was fetched live at the time this test is executed.
    monkeypatch.setattr(websoc_workflow, "_utc_now", lambda: capture_clock[0] or raw("sociol_1_2026_fall")[1]["captured_at"])
    original_fetch = websoc_workflow.fetch_websoc_course_offering
    def replay_fetch(**kwargs):
        stem = {("SOCIOL", "1"): "sociol_1_2026_fall", ("PHILOS", "3"): "philos_3_2026_fall",
                ("DRAMA", "10"): "drama_10_2026_fall", ("ECON", "13"): "econ_13_2026_fall"}[
                    (kwargs["department"], kwargs["course_number"])]
        # Production records retrieval time before its POST. Set the replay
        # clock before entering it, rather than borrowing the last response.
        capture_clock[0] = raw(stem)[1]["captured_at"]
        return original_fetch(**kwargs)
    monkeypatch.setattr(websoc_workflow, "fetch_websoc_course_offering", replay_fetch)
    ge_meta = json.loads((FIXTURES / "ge_evidence/uci_ge_2026-27.metadata.json").read_text())
    class SnapshotClock(datetime):
        @classmethod
        def now(cls, tz=None):
            captured = datetime.fromisoformat(ge_meta["retrieved_at"])
            return captured.astimezone(tz) if tz else captured.replace(tzinfo=None)
    monkeypatch.setattr(ge_evidence, "datetime", SnapshotClock)
    def official_ge(url):
        calls.append(("GE", url))
        return (FIXTURES / "ge_evidence/uci_ge_2026-27.html").read_text()
    monkeypatch.setattr(ge_evidence, "_get_html", official_ge)
    local, _ = UCIRelationalLoader(FIXTURES / "ge_evidence/local_sociol1").load(Term(2026, "Spring"))
    course = db._course_record_to_dict(next(row for row in local if row.ref.display() == "SOCIOL 1"))
    monkeypatch.setattr(db, "get_catalog", lambda *_: None)
    monkeypatch.setattr(db, "get_term_coverage", lambda *_: {"coverage_status": "unavailable"})
    def info(cid):
        if cid == "SOCIOL 1":
            return {"found": True, "source": "db", "course": deepcopy(course)}
        # Replay actual basic catalog fields from the captured official table;
        # no GE or pairing verification is supplied by this replacement.
        stem = {"DRAMA 10": "drama_10_2026_fall", "ECON 13": "econ_13_2026_fall"}[cid]
        body, meta = raw(stem)
        parsed = websoc_workflow.parse_websoc_course_html(
            body.decode(meta["response"]["encoding"]), term=meta["scope"]["term"],
            department=meta["request"]["form"]["Dept"], course_number=meta["request"]["form"]["CourseNum"],
            source_url=websoc_workflow.WEBSOC_URL)
        return {"found": True, "source": "captured_official_metadata", "course": {
            "course_id": cid, "title": parsed["course_title"], "units": parsed["sections"][0]["units"],
            "ge_categories": []}}
    monkeypatch.setattr(db, "get_course_info", info)
    monkeypatch.setattr(db, "get_student_profile", lambda *_: {"found": True, "profile": {}})
    monkeypatch.setattr(db, "get_grade_distribution", lambda *_: {"found": False})
    monkeypatch.setattr(db, "get_professor_rating", lambda *a, **k: {"found": False})
    monkeypatch.setattr(policies, "is_term_past_deadline", lambda *a, **k: False)
    monkeypatch.setattr(policies, "get_term_calendar", lambda *_: {})
    return calls


def context(hard_day=None):
    plan = empty_filter_plan()
    plan["course_scope"] = "ge"
    if hard_day:
        plan["hard_filters"]["excluded_weekdays"] = [hard_day]
    return {"user_id": "demo_001", "term": "2026 Fall", "default_term": "2027 Winter",
            "allowed_query_terms": ["2026 Fall"], "user_query": "推荐简单GE课程", "response_language": "zh",
            "pending_schedule": [], "_prefilter_state": {
                "enabled": True, "plan": plan, "plan_status": "validated", "issues": [], "parse_metrics": {},
                "profile": {}, "pools": {}, "candidates": {}, "excluded": {}, "section_overrides": {},
                "detail_query_attempts": 0, "ge_scope_requested": True}}


def stage(ctx, course_id="SOCIOL 1"):
    ack = tools.dispatch("propose_recommendation", {"term": "2026 Fall", "items": [
        {"course_id": course_id, "category": "elective", "reason": "用户需求"}]}, context=ctx)
    assert ack["ok"] is True, ack
    return ctx["_proposed_cards"][0]


def test_actual_http_content_reaches_card_and_saved_selection_without_injected_verified(source_pipeline, app_client):
    ctx = context()
    card = stage(ctx)
    assert card["ge_evidence"]["status"] == "verified"
    assert card["ge_categories"] == ["GE-III", "GE-VII"]
    assert card["ge_evidence"]["source"]["catalogue_year"] == "2026-27"
    assert card["pairing"]["version"] == 2
    assert card["pairing_source_proof"]["request_form"]["ClassType"] == "ALL"
    assert card["pairing_source_proof"]["response_document_complete"] is True
    assert card["section_source"] == "registrar_websoc"
    assert pairing_for_primary(card["pairing"], "69000")["requirement"] == "required"
    assert pairing_for_primary(card["pairing"], "69000")["relationship"] == "unknown"
    for code in ("69020", "69040"):
        rule = pairing_for_primary(card["pairing"], code)
        assert rule["requirement"] == "required" and len(rule["allowed_bundles"]) == 9
    assert card["primary_code"] in {"69020", "69040"}  # A is FULL in the actual page.
    assert card["selection_verification"]["status"] == "unsatisfied"
    assert card["selection_verification"]["selected_secondary_codes"] == []
    assert not any(w["code"] in {"ge_labels_unknown", "secondary_pairing_unknown"} for w in card["prefilter"]["warnings"])
    html = render(f"renderCard({json.dumps(card)})")
    assert "cc-source-unverified" not in html and "UCI WebSoc" in html and "UCI GE 2026-27" in html
    assert "+ Dis required" in html and "Complete secondary selection needed" in html
    assert html.count('cc-source-uci_websoc') == 1
    assert len([call for call in source_pipeline if call[0] == "POST"]) == 1
    assert len([call for call in source_pipeline if call[0] == "GE"]) == 1
    # A complete rule is not a selection. Both incomplete and incompatible
    # saved drafts must be distinguished from a proven full combination.
    primary = card["primary_code"]
    rule = pairing_for_primary(card["pairing"], primary)
    secondary = rule["allowed_bundles"][0]["secondary_codes"][0]
    entries = [{"course_id": "SOCIOL 1", "term": "2026 Fall", "section": primary}]
    lookup = lambda *_: {"found": True, "sections": card["sections"], "pairing": card["pairing"]}
    items = build_pending_schedule_bundle_items(entries, term=None, section_lookup=lookup)
    assert validate_schedule_bundle(items)["pairing"][0]["selection_verification"]["status"] == "unsatisfied"
    entries.append({"course_id": "SOCIOL 1", "term": "2026 Fall", "section": secondary})
    items = build_pending_schedule_bundle_items(entries, term=None, section_lookup=lookup)
    assert validate_schedule_bundle(items)["pairing"][0]["selection_verification"]["status"] == "satisfied"
    by_code = {section["section_code"]: section for section in card["sections"]}
    wrong = "69041" if primary == "69020" else "69021"
    assert verify_selection(card["pairing"], [by_code[primary], by_code[wrong]])["status"] == "unsatisfied"
    # Public server-owned cards survive persistence and are used by actual add
    # routes without another source lookup or any student record injection.
    from app.data import sessions
    sid = sessions.create_session("demo_001", title="Public source acceptance")
    sessions.append_turn("demo_001", sid, "assistant", render_recommendation_answer([card], "zh"), cards=[card])
    added = app_client.post("/api/schedule/add", json={"session_id": sid, "course_id": "SOCIOL 1", "term": "2026 Fall", "section": primary})
    assert added.status_code == 200
    assert added.json()["schedule_validation"]["pairing"][0]["selection_verification"]["status"] == "unsatisfied"
    added = app_client.post("/api/schedule/add", json={"session_id": sid, "course_id": "SOCIOL 1", "term": "2026 Fall", "section": secondary})
    assert added.status_code == 200
    assert added.json()["schedule_validation"]["pairing"][0]["selection_verification"]["status"] == "satisfied"
    restored = app_client.get(f"/api/sessions/demo_001/{sid}").json()["turns"][-1]["cards"][0]
    assert restored["pairing"] == card["pairing"] and restored["ge_evidence"] == card["ge_evidence"]
    assert ctx["default_term"] == "2027 Winter" and ctx["term"] == "2026 Fall"


def test_real_friday_filter_keeps_b_complete_bundles_and_excludes_c_before_grades(source_pipeline, monkeypatch):
    ctx = context("Fri")
    ctx["user_query"] = "推荐GE，绝对不能周五上课"
    queried = []
    from app.agent import grade_evidence
    monkeypatch.setattr(grade_evidence, "get_history", lambda _ctx, cid, name, dept: queried.append(name) or {"found": False})
    tools.prepare_recommendation_grades([{"course_id": "SOCIOL 1"}], "2026 Fall", ctx)
    card = stage(ctx)
    assert card["primary_code"] == "69020"
    assert "CARIAN, E." not in queried
    assert not any("F" in (section.get("days") or "") for section in card["sections"])
    assert any(section["section_type"] == "Dis" for section in card["sections"])
    assert pairing_for_primary(card["pairing"], "69020")["requirement"] == "required"
    assert "69040" not in [section["section_code"] for section in card["sections"]]
    exclusions = ctx["_prefilter_state"]["candidates"][("2026 Fall", "SOCIOL 1")]["prefilter"]["excluded_sections"]
    assert any(row["section_code"] == "69040" and "excluded_weekday" in row["reasons"] for row in exclusions)


def test_switching_primary_recomputes_projected_proof_and_ge_is_not_personal_credit(source_pipeline):
    card = stage(context())
    # The actual A row is FULL and absent from enrollable section groups, but
    # remains in the raw snapshot. Restore uses its actual code and raw facts.
    card["primary_code"] = "69000"
    revised = normalize_recommendation_card(card, "zh")
    assert revised["pairing_selected"]["requirement"] == "required"
    assert revised["pairing_selected"]["relationship"] == "unknown"
    assert revised["selection_verification"]["status"] == "unsatisfied"
    assert revised["ge_evidence"]["categories"] == ["III", "VII"]
    assert revised["ge_evidence"]["personal_contribution"]["status"] == "unknown"


@pytest.mark.parametrize("cid,primary,requirement,secondary_count", [
    ("DRAMA 10", "03005", "not_required", 0),
    ("ECON 13", "62040", "required", 7),
])
def test_screenshot_courses_full_source_to_card_and_persisted_schedule(
        source_pipeline, app_client, cid, primary, requirement, secondary_count):
    """No hand-written verified contracts: replay the exact public WebSoc body."""
    from app.data import sessions
    from app.recommendations.pairing import discussion_policy_current
    card = stage(context(), cid)
    assert card["primary_code"] == primary
    rule = pairing_for_primary(card["pairing"], primary)
    assert discussion_policy_current(rule)
    assert rule["requirement"] == requirement
    if requirement == "required":
        assert rule["relationship_complete"] is True
    assert len(rule["allowed_bundles"]) == secondary_count
    assert card["pairing_source_proof"]["response_document_complete"] is True
    stem = "drama_10_2026_fall" if cid == "DRAMA 10" else "econ_13_2026_fall"
    meta = json.loads((FIXTURES / "websoc/pairing" / f"{stem}.json").read_text())
    assert card["pairing_source_proof"]["retrieved_at"] == meta["captured_at"]
    secondary = [s for s in card["sections"] if s["section_type"] == "Dis"]
    assert len(secondary) == secondary_count
    html = render(f"renderCard({json.dumps(card)})")
    assert "Pairing condition unverified" not in html
    assert "No discussion required" in html if requirement == "not_required" else "+ Dis required" in html
    answer = render_recommendation_answer([card], "zh")
    assert "配对要求待核实" not in answer
    assert "无需选 Dis" in answer if requirement == "not_required" else "需选 Dis" in answer
    sid = sessions.create_session("demo_001", title=f"Discussion rule {cid}")
    sessions.append_turn("demo_001", sid, "assistant", answer, cards=[card])
    response = app_client.post("/api/schedule/add", json={"session_id": sid, "course_id": cid,
                                "term": "2026 Fall", "section": primary})
    assert response.status_code == 200, response.text
    expected = "satisfied" if requirement == "not_required" else "unsatisfied"
    actual = response.json()["schedule_validation"]["pairing"][0]["selection_verification"]
    assert actual["status"] == expected
    if secondary:
        # The screenshot's A7 is a real complete choice under a single primary.
        response = app_client.post("/api/schedule/add", json={"session_id": sid, "course_id": cid,
                                    "term": "2026 Fall", "section": "62047"})
        assert response.json()["schedule_validation"]["pairing"][0]["selection_verification"]["status"] == "satisfied"
    restored = app_client.get("/api/schedule", params={"session_id": sid}).json()
    assert restored["schedule_validation"]["pairing"][0]["selection_verification"]["status"] == "satisfied"
    assert len([call for call in source_pipeline if call[0] == "POST"]) == 1


def test_econ_friday_only_open_discussion_prevents_formal_card_before_grading(source_pipeline, monkeypatch):
    from app.agent import grade_evidence
    ctx = context("Fri")
    ctx["user_query"] = "推荐 GE，绝对不能周五上课"
    queried = []
    monkeypatch.setattr(grade_evidence, "get_history", lambda _ctx, cid, name, dept: queried.append(name) or {"found": False})
    tools.prepare_recommendation_grades([{"course_id": "ECON 13"}], "2026 Fall", ctx)
    candidate = ctx["_prefilter_state"]["candidates"][("2026 Fall", "ECON 13")]
    assert "62047" not in [s["section_code"] for s in candidate["sections"]]
    # The complete raw listing has seven discussions, but only Friday A7 is
    # open at capture time. Remaining Monday/Thursday rows are all FULL.
    ack = tools.dispatch("propose_recommendation", {"term": "2026 Fall", "items": [
        {"course_id": "ECON 13", "category": "elective", "reason": "用户需求"}]}, context=ctx)
    assert ack["ok"] and ack["staged_count"] == 0
    assert not queried


def test_drama_no_discussion_does_not_prove_tba_asynchronous_or_time_safe(source_pipeline):
    ctx = context("Fri")
    candidate, excluded = prefilter.ensure_candidate(ctx, "DRAMA 10", "2026 Fall")
    assert excluded is None
    assert pairing_for_primary(candidate["pairing"], "03005")["requirement"] == "not_required", candidate["pairing"]
    assert candidate["prefilter"]["status"] != "eligible"
    assert "time_unknown" in candidate["prefilter"]["reasons"]
    assert prefilter._verified_time_sections(candidate, ctx["_prefilter_state"]["plan"]["hard_filters"]) == []


def test_all_proposals_missing_catalog_return_ack_without_unbound_local(source_pipeline, monkeypatch):
    monkeypatch.setattr(db, "get_course_info", lambda *_: {"found": False})
    result = tools._tool_propose_recommendation([{"course_id": "DRAMA 10"}], context=context(), term="2026 Fall")
    assert result["ok"] is True and result["staged_count"] == 0
