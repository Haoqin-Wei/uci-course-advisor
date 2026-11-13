"""Public recommendation evidence boundaries; no provider or real student data."""
from copy import deepcopy

import pytest

from app.agent import prefilter, tools
from app.data import sessions
from app.recommendations.filter_plan import empty_filter_plan


@pytest.mark.parametrize("labels", [["II"], ["II", "unrecognized"], []])
def test_ge_scope_never_confirms_parseable_but_unverified_labels(labels):
    context = {"_prefilter_state": {
        "plan": empty_filter_plan(), "profile": {}, "ge_scope_requested": True,
    }}
    candidate = {"course_id": "PHILOS 3", "term": "2026 Fall",
                 "ge_categories": labels, "ge_labels_verified": False}
    result = prefilter._apply([candidate], context)
    assert len(result["kept"]) == 1  # Unknown is not a verified non-GE course.
    assert "ge_labels_unknown" in result["kept"][0]["prefilter"]["reasons"]
    assert any("ge_labels_unknown" in item["reasons"] for item in result["needs_verification"])


def test_target_term_ge_fields_use_actual_catalog_record_not_generic_course_info(monkeypatch):
    from app.data import db
    from tests.test_dynamic_prefilter_integration import _install

    _install(monkeypatch)
    fields = tools._recommendation_ge_fields({}, "CS1", "2026 Fall", {
        "ge_categories": ["II"], "ge_labels_verified": True,
    })
    assert fields["ge_categories"] == ["III"]
    # A target-term CSV record is correctly selected, but its legacy boolean
    # does not authenticate GE tags for the target academic year's edition.
    assert fields["ge_labels_verified"] is False
    assert fields["ge_evidence_course_id"] == "COMPSCI 1"
    assert fields["ge_evidence_term"] == "2026 Fall"
    assert fields["ge_provenance"]["source"] == "local_catalogue"

    monkeypatch.setattr(db, "get_catalog", lambda _: None)
    missing = tools._recommendation_ge_fields({}, "CS1", "2027 Winter", {
        "ge_categories": ["II"], "ge_labels_verified": True,
    })
    assert missing["ge_labels_verified"] is False
    assert missing["ge_categories"] == []


@pytest.mark.parametrize("ranking,dynamic", [(True, False), (False, True), (True, True)])
def test_restoring_legacy_card_normalizes_snapshot_without_rewriting_history(app_client, monkeypatch, ranking, dynamic):
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", str(ranking).lower())
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", str(dynamic).lower())
    session_id = sessions.create_session("demo_001", title="Legacy card boundary")
    sessions.append_turn("demo_001", session_id, "user", "推荐简单 GE")
    old_card = {"course_id": "PHILOS 3", "term": "2026 Fall", "primary_code": "10000",
                "reason": "GE II，风险低得多，无需讨论课", "ge_category": "II",
                "requires_secondary": False, "model_reason": "Legacy private diagnostic"}
    sessions.append_turn("demo_001", session_id, "assistant", "Historical answer", cards=[old_card])
    original = deepcopy(sessions.read_turns("demo_001", session_id))
    response = app_client.get(f"/api/sessions/demo_001/{session_id}")
    assert response.status_code == 200
    card = response.json()["turns"][-1]["cards"][0]
    assert card["ge_evidence"]["status"] == "unknown"
    assert card["ge_category"] is None
    assert "model_reason" not in card
    assert "风险低得多" not in card["reason"]
    assert "无需讨论课" not in card["reason"]
    assert "GE II" not in card["reason"]
    assert card["pairing"]["requirement"] == "unknown"
    assert response.json()["turns"][-1]["content"] == "Historical answer"
    assert sessions.read_turns("demo_001", session_id) == original


def test_restoring_legacy_card_with_both_flags_off_preserves_compatibility(app_client, monkeypatch):
    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "false")
    monkeypatch.setenv("DYNAMIC_PREFILTER_ENABLED", "false")
    session_id = sessions.create_session("demo_001", title="Legacy compatibility")
    card = {"course_id": "PHILOS 3", "ge_category": "II", "reason": "Original reason",
            "requires_secondary": False}
    sessions.append_turn("demo_001", session_id, "assistant", "Original answer", cards=[card])
    response = app_client.get(f"/api/sessions/demo_001/{session_id}")
    assert response.json()["turns"][-1]["cards"] == [card]


def test_partial_history_retains_controlled_card_language_without_prior_user_turn(app_client, monkeypatch):
    from app.recommendations.presentation import normalize_recommendation_card

    monkeypatch.setenv("RECOMMENDATION_RANKING_ENABLED", "true")
    session_id = sessions.create_session("demo_001", title="Partial card history")
    sessions.append_turn("demo_001", session_id, "user", "推荐课程")
    card = normalize_recommendation_card({"course_id": "PHILOS 3", "term": "2026 Fall"}, "zh")
    sessions.append_turn("demo_001", session_id, "assistant", "中文回答", cards=[card])
    payload = app_client.get(f"/api/sessions/demo_001/{session_id}?since_turn=1").json()
    assert len(payload["turns"]) == 1
    assert payload["turns"][0]["cards"] == [card]
