"""Source identity is independent of rule verification and freshness."""
import json

from tests.test_zotistics_cards import render


def badges(card):
    return render(f"_renderCardSourceBadges({json.dumps(card)})")


def test_public_c1_internal_metadata_and_official_alias_render_one_official_source():
    html = badges({"section_source": "prefilter_metadata", "sections": [
        {"source": "registrar_websoc", "is_live": False, "retrieved_at": "2026-10-20T20:44:33+00:00"},
        {"source": "uci_websoc", "is_live": False},
    ]})
    assert html.count('cc-source-badge ') == 1
    assert "UCI WebSoc" in html
    assert "2026-10-20T20:44:33+00:00" in html
    assert "Unverified" not in html and "Live WebSoc" not in html


def test_unknown_source_is_neutral_and_escaped():
    html = badges({"section_source": "<unknown-source>", "source_badges": ["other_unknown"]})
    assert html.count('cc-source-badge ') == 1
    assert 'cc-source-unknown' in html and "Source unknown" in html
    assert "cc-source-unverified" not in html and "<unknown-source>" not in html


def test_explicit_unverified_evidence_still_has_its_own_warning_badge():
    assert "cc-source-unverified" in badges({"source_badges": ["unverified"]})


def test_warning_deduplication_keeps_distinct_primary_scopes():
    payload = {"course_id": "SOCIOL1", "term": "2026 Fall", "prefilter": {
        "enabled": True, "status": "needs_verification", "warnings": [
            {"code": "time_unknown", "primary_code": "69000", "message": "A unknown"},
            {"code": "time_unknown", "primary_code": "69000", "message": "A duplicate"},
            {"code": "time_unknown", "primary_code": "69020", "message": "B unknown"},
        ]}}
    html = render(f"_renderDynamicPrefilterMetadata({json.dumps(payload)})")
    assert "A unknown" in html and "B unknown" in html and "A duplicate" not in html


def test_confirmed_ge_shows_annual_official_source_separately():
    html = badges({"ge_evidence": {"status": "verified", "source": {
        "source": "uci_catalogue", "catalogue_year": "2026-27",
        "source_url": "https://catalogue.uci.edu/informationforadmittedstudents/requirementsforabachelorsdegree/"}}})
    assert "UCI GE 2026-27" in html and "https://catalogue.uci.edu/" in html
    assert "cc-source-unverified" not in html


def test_explicit_refresh_is_not_reused_from_pre_refresh_tool_cache(monkeypatch):
    from app.agent.loop import _idempotent_tool_key
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "true")
    assert _idempotent_tool_key("get_live_sections", {"course_id": "SOCIOL1", "force_refresh": True}) is None
    assert _idempotent_tool_key("get_live_sections", {"course_id": "SOCIOL1"}) is not None


def test_visible_plan_summary_separates_required_rules_from_actual_saved_selection():
    result = render("""(() => {
      globalThis.currentResponseLanguage='zh';
      const proof={requirement:'required',required_types:['Dis'],required_components:[{types:['Dis'],min:1,max:1}],evidence:{status:'verified'}};
      const card={course_id:'SOCIOL1',term:'Fall 2026',primary_code:'69040',
        pairing:{version:2,per_primary:{'69040':proof}},selection_verification:{status:'unsatisfied',primary_code:'69040'}};
      const initial=_renderCoursePairingSummary(card);
      addedCourses.add(scheduleCourseKey(card.term,card.course_id));
      globalThis.scheduleValidation={pairing:[{course_id:card.course_id,term:card.term,pairing:card.pairing,
        selection_verification:{status:'satisfied',primary_code:'69040'}}]};
      const complete=_renderCoursePairingSummary(card);
      scheduleValidation.pairing[0].pairing={version:2,per_primary:{}};
      scheduleValidation.pairing[0].selection_verification={status:'unknown',primary_code:'69040'};
      return {initial,complete,refreshed:_renderCoursePairingSummary(card)};
    })()""")
    assert "尚未选择完整配套班次" in result["initial"]
    assert "当前选择已满足配对要求" in result["complete"]
    assert "配对要求待核实" in result["refreshed"]


def test_or_and_and_components_have_different_ui_labels():
    result = render("""({or:_pairingComponentLabel({required_components:[{types:['Dis','Lab'],min:1,max:1}]},false),
      and:_pairingComponentLabel({required_components:[{types:['Dis'],min:1,max:1},{types:['Lab'],min:1,max:1}]},false)})""")
    assert result == {"or": "Dis or Lab", "and": "Dis + Lab"}
