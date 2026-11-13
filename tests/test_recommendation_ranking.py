from __future__ import annotations

from copy import deepcopy

import pytest

from app.recommendations import build_candidate_features, rank_candidates


def candidate(course_id, a=None, rating=None, *, graded=100, ratings=10, **payload):
    features = {}
    if a is not None:
        features["grade_a_rate"] = {"value": a, "sample_count": graded}
    if rating is not None:
        features["professor_rating"] = {"value": rating, "sample_count": ratings}
    return {"course_id": course_id, "features": features, **payload}


def test_default_score_explains_raw_normalized_adjusted_and_contributions():
    ranking = rank_candidates([candidate("ICS33", .8, 4)])[0]["ranking"]
    a = ranking["features"]["grade_a_rate"]
    r = ranking["features"]["professor_rating"]
    assert a["raw"] == .8
    assert a["normalized"] == 80
    assert a["adjusted"] == 65
    assert a["sample_count"] == 100
    assert a["reliability_factor"] == .5
    assert r["normalized"] == 75
    assert r["adjusted"] == 62.5
    assert a["contribution"] == pytest.approx(39)
    assert r["contribution"] == pytest.approx(25)
    assert ranking["final_score"] == pytest.approx(64)
    assert ranking["configured_weights"] == pytest.approx({"grade_a_rate": .6, "professor_rating": .4})
    assert ranking["missing_features"] == []


def test_multiple_weight_configs_switch_order_and_normalize_totals():
    rows = [candidate("HIGH_A", 1, 1), candidate("HIGH_RATING", 0, 5)]
    assert [r["course_id"] for r in rank_candidates(rows)] == ["HIGH_A", "HIGH_RATING"]
    assert [r["course_id"] for r in rank_candidates(rows, {"grade_a_rate": 1, "professor_rating": 9})] == ["HIGH_RATING", "HIGH_A"]
    assert rank_candidates(rows, {"professor_rating": 4})[0]["ranking"]["effective_weights"] == {
        "grade_a_rate": 0, "professor_rating": 1,
    }
    assert [r["ranking"]["final_score"] for r in rank_candidates(rows, {"grade_a_rate": 6, "professor_rating": 4})] == pytest.approx(
        [r["ranking"]["final_score"] for r in rank_candidates(rows)]
    )


@pytest.mark.parametrize("weights", [
    {}, {"grade_a_rate": 0}, {"grade_a_rate": -1}, {"typo": 1},
    {"grade_a_rate": True}, {"grade_a_rate": "1"},
    {"grade_a_rate": float("nan")}, {"grade_a_rate": float("inf")}, [],
])
def test_invalid_weight_configs_fail_explicitly(weights):
    with pytest.raises(ValueError):
        rank_candidates([], weights=weights)


@pytest.mark.parametrize("reliability", [
    {"grade_a_rate": 0}, {"professor_rating": -1}, {"grade_a_rate": True},
    {"professor_rating": float("nan")}, {"professor_rating": float("inf")}, {"typo": 1}, [],
])
def test_invalid_reliability_configs_fail_explicitly(reliability):
    with pytest.raises(ValueError):
        rank_candidates([], reliability=reliability)


def test_weight_normalization_and_shrinkage_handle_large_finite_numbers():
    ranking = rank_candidates(
        [candidate("LARGE", 1, 5, graded=1e308, ratings=1e308)],
        weights={"grade_a_rate": 1e308, "professor_rating": 1e308},
        reliability={"grade_a_rate": 1e308, "professor_rating": 1e308},
    )[0]["ranking"]
    assert ranking["final_score"] == 75
    assert all(feature["reliability_factor"] == .5 for feature in ranking["features"].values())


def test_missing_feature_redistributes_weight_without_scoring_it_as_zero():
    rows = rank_candidates([candidate("ONLY_A", 1), candidate("ONLY_RATING", rating=5)])
    for row in rows:
        ranking = row["ranking"]
        assert ranking["final_score"] == 75
        assert len(ranking["missing_features"]) == 1
        missing = ranking["features"][ranking["missing_features"][0]]
        assert missing["contribution"] is None
        assert missing["normalized"] is None
        assert missing["effective_weight"] == 0
        assert missing["uncertain"] is True
    assert rows[0]["ranking"]["effective_weights"] == {"grade_a_rate": 1, "professor_rating": 0}


def test_all_missing_scores_are_none_last_and_stable():
    rows = [candidate("MISSING_FIRST"), candidate("ZERO_A", 0), candidate("MISSING_SECOND")]
    ranked = rank_candidates(rows)
    assert [r["course_id"] for r in ranked] == ["ZERO_A", "MISSING_FIRST", "MISSING_SECOND"]
    assert ranked[0]["ranking"]["features"]["grade_a_rate"]["normalized"] == 0
    assert ranked[0]["ranking"]["final_score"] == 25
    assert all(row["ranking"]["final_score"] is None for row in ranked[1:])
    assert all(row["ranking"]["uncertain_features"] == ["grade_a_rate", "professor_rating"] for row in ranked[1:])


@pytest.mark.parametrize("feature,value", [
    ("grade_a_rate", True), ("grade_a_rate", float("nan")), ("grade_a_rate", float("inf")),
    ("grade_a_rate", -.01), ("grade_a_rate", 1.01), ("grade_a_rate", "0.8"),
    ("professor_rating", 0), ("professor_rating", 5.01), ("professor_rating", False),
])
def test_invalid_values_are_missing_uncertain_not_clamped_or_zero(feature, value):
    result = rank_candidates([{"features": {feature: {"value": value, "sample_count": 100}}}])[0]["ranking"]
    assert result["final_score"] is None
    assert result["features"][feature]["missing_reason"] in {"invalid_value", "value_out_of_range"}
    assert result["features"][feature]["adjusted"] is None
    assert result["features"][feature]["uncertain"] is True


@pytest.mark.parametrize("sample,reason", [
    (None, "missing_sample_count"), (0, "nonpositive_sample_count"),
    (-1, "nonpositive_sample_count"), (False, "invalid_sample_count"),
    (float("nan"), "invalid_sample_count"), (float("inf"), "invalid_sample_count"),
])
def test_missing_or_invalid_samples_do_not_invent_a_neutral_50(sample, reason):
    ranking = rank_candidates([candidate("ICS33", .8, graded=sample)])[0]["ranking"]
    feature = ranking["features"]["grade_a_rate"]
    assert feature["missing_reason"] == reason
    assert feature["normalized"] == 80
    assert feature["adjusted"] is None
    assert ranking["final_score"] is None


def test_sample_shrinkage_is_symmetric_and_can_be_configured():
    low, high = rank_candidates([candidate("HIGH", 1, graded=1), candidate("LOW", 0, graded=1)])
    assert low["ranking"]["final_score"] == pytest.approx(50 + 50 / 101)
    assert high["ranking"]["final_score"] == pytest.approx(50 - 50 / 101)
    many = rank_candidates([candidate("MANY", 1, graded=1000)])[0]["ranking"]["final_score"]
    assert many > low["ranking"]["final_score"]
    assert rank_candidates([candidate("ICS33", 1, graded=100)], reliability={"grade_a_rate": 300})[0]["ranking"]["final_score"] == 62.5


def test_input_is_unchanged_payload_scope_is_preserved_and_ties_keep_input_order():
    rows = [candidate("Z_LAST_ALPHABETICALLY", .8, 4, term="2026 Fall", section={"code": "10001"}, instructor="LECTURER, A."),
            candidate("A_FIRST_ALPHABETICALLY", .8, 4, term="2026 Winter", custom={"nested": [1, 2]})]
    before = deepcopy(rows)
    ranked = rank_candidates(rows)
    assert rows == before
    assert [r["course_id"] for r in ranked] == [r["course_id"] for r in rows]
    for original, result in zip(rows, ranked):
        assert {key: result[key] for key in original} == original
    ranked[0]["section"]["code"] = "CHANGED"
    assert rows == before


def test_invalid_nested_feature_values_do_not_share_mutable_references_with_output():
    rows = [{"features": {"grade_a_rate": {"value": [0, 1], "sample_count": [100]}}}]
    before = deepcopy(rows)
    ranking = rank_candidates(rows)[0]["ranking"]
    assert ranking["final_score"] is None
    ranking["features"]["grade_a_rate"]["raw"].append(2)
    ranking["features"]["grade_a_rate"]["sample_count"].append(200)
    assert rows == before


def test_no_batch_minmax_and_topk_is_applied_after_scoring_all_candidates():
    row = candidate("TARGET", .72, 4.2)
    alone = rank_candidates([row])[0]["ranking"]
    together = rank_candidates([candidate("WORSE", 0, 1), row, candidate("BETTER", 1, 5)])
    assert next(r["ranking"] for r in together if r["course_id"] == "TARGET") == alone
    assert rank_candidates([candidate("FIRST", 0), candidate("SECOND", 1)], top_k=1)[0]["course_id"] == "SECOND"
    assert rank_candidates([row], top_k=0) == []


@pytest.mark.parametrize("top_k", [-1, True, 1.5, "2"])
def test_invalid_topk_is_rejected(top_k):
    with pytest.raises(ValueError):
        rank_candidates([], top_k=top_k)


def test_disabled_known_feature_does_not_receive_weight_when_requested_feature_missing():
    ranking = rank_candidates([candidate("ONLY_A", 1)], weights={"professor_rating": 1})[0]["ranking"]
    assert ranking["effective_weights"] == {"grade_a_rate": 0, "professor_rating": 0}
    assert ranking["final_score"] is None


def test_adapter_prefers_a_count_over_percent_excludes_non_letter_samples_and_ignores_gpa():
    data = {"found": True, "source": "zotistics", "scope": "course", "grades": {
        "course_id": "ICS33", "graded": 100, "samples": 200, "pct_A": 95,
        "letter_counts": {"A": 0, "B": 50, "C": 50, "D": 0, "F": 0, "P": 100},
        "source_reported_gpa": 4, "avg_gpa": 4,
    }}
    before = deepcopy(data)
    features = build_candidate_features(data, course_id="ICS33", instructor_name="LECTURER, A.")
    a = features["grade_a_rate"]
    assert a["value"] == 0
    assert a["sample_count"] == 100
    assert a["scope"] == "course"
    assert a["instructor_name"] is None
    assert data == before
    assert rank_candidates([{"features": features}])[0]["ranking"]["final_score"] == 25


@pytest.mark.parametrize("source,expected", [
    ({"course_id": "ICS33", "graded": 80, "pct_A": 62.5}, .625),
    ({"department": "I&C SCI", "courseNumber": "33", "gradeACount": 2, "gradeBCount": 1, "gradeCCount": 1, "gradeDCount": 0, "gradeFCount": 0}, .5),
    ({"course_id": "ICS33", "graded": 100, "letter_pct": {"A": 0}}, 0),
])
def test_adapter_converts_percent_scale_and_raw_anteater_counts(source, expected):
    feature = build_candidate_features(source)["grade_a_rate"]
    assert feature["value"] == expected
    assert feature["sample_count"] in {4, 80, 100}


@pytest.mark.parametrize("percent", [True, float("nan"), float("inf"), -1, 101])
def test_adapter_invalid_percent_is_not_a_valid_a_rate(percent):
    feature = build_candidate_features({"pct_A": percent, "graded": 100})["grade_a_rate"]
    assert feature["missing_reason"] == "invalid_a_percentage"
    assert feature["value"] is None


@pytest.mark.parametrize("source,reason", [
    ({"letter_counts": {"A": 10}, "graded": None}, "missing_sample_count"),
    ({"letter_counts": {"A": 0}, "graded": 0}, "nonpositive_sample_count"),
    ({"letter_counts": {"A": 0}, "graded": False}, "invalid_sample_count"),
    ({"letter_counts": {"A": True}, "graded": 100}, "invalid_a_count"),
    ({"letter_counts": {"A": 110}, "graded": 100, "pct_A": 60}, "inconsistent_grade_counts"),
])
def test_adapter_count_errors_do_not_fall_back_to_a_convenient_percentage(source, reason):
    feature = build_candidate_features(source)["grade_a_rate"]
    assert feature["missing_reason"] == reason
    assert rank_candidates([{"features": {"grade_a_rate": feature}}])[0]["ranking"]["final_score"] is None


@pytest.mark.parametrize("source,expected_reason", [
    ({"scope": "instructor", "grades": {"instructor_name": "LECTURER, A.", "pct_A": 80, "graded": 100}}, "instructor_history_not_course_scoped"),
    ({"scope": "course", "grades": {"course_id": "MATH2A", "pct_A": 80, "graded": 100}}, "grade_course_mismatch"),
    ({"scope": "course_instructor", "grades": {"course_id": "ICS33", "instructor_name": "OTHER, B.", "pct_A": 80, "graded": 100}}, "grade_instructor_mismatch"),
])
def test_adapter_cannot_use_career_or_other_course_professor_grades_as_the_pair(source, expected_reason):
    feature = build_candidate_features(source, course_id="ICS33", instructor_name="LECTURER, A.")["grade_a_rate"]
    assert feature["missing_reason"] == expected_reason
    assert rank_candidates([{"features": {"grade_a_rate": feature}}])[0]["ranking"]["final_score"] is None


def test_adapter_uses_selected_primary_rmp_and_preserves_exact_pair_scope():
    grades = {"scope": "course_instructor", "source": "zotistics", "grades": {
        "course_id": "ICS33", "instructor_name": "LECTURER, A.", "graded": 100, "pct_A": 75,
    }}
    rmp = {"found": True, "source": "db", "instructor": {"avg_rating": 5, "num_ratings": 40}}
    primary = {"section_type": "Lec", "instructors": ["LECTURER, A."]}
    features = build_candidate_features(grades, rmp, course_id="ICS33", instructor_name="LECTURER, A.", primary_section=primary)
    assert features["grade_a_rate"]["scope"] == "course_instructor"
    assert features["professor_rating"]["sample_count"] == 40
    assert features["professor_rating"]["instructor_name"] == "LECTURER, A."
    explanation = rank_candidates([{"features": features}])[0]["ranking"]["features"]["professor_rating"]
    assert explanation["normalized"] == 100
    assert explanation["adjusted"] == 90


@pytest.mark.parametrize("primary,instructor_name,rmp,reason", [
    ({"section_type": "Dis", "instructors": ["ASSISTANT, T."]}, "ASSISTANT, T.", {"avg_rating": 5, "num_ratings": 100}, "not_a_primary_instructor"),
    ({"section_type": "Lec", "instructors": ["LECTURER, A."]}, "ASSISTANT, T.", {"avg_rating": 5, "num_ratings": 100}, "instructor_not_in_primary_section"),
    ({"section_type": "Lec", "instructors": ["LECTURER, A."]}, "LECTURER, A.", {"instructor_name": "ASSISTANT, T.", "avg_rating": 5, "num_ratings": 100}, "professor_instructor_mismatch"),
])
def test_ta_or_wrong_instructor_rating_cannot_replace_primary_rating(primary, instructor_name, rmp, reason):
    feature = build_candidate_features(professor_data=rmp, instructor_name=instructor_name, primary_section=primary)["professor_rating"]
    assert feature["missing_reason"] == reason


def test_missing_source_status_survives_adaptation_instead_of_inventing_features():
    features = build_candidate_features({"found": False, "status": "unavailable"}, {"found": False})
    ranking = rank_candidates([{"features": features}])[0]["ranking"]
    assert ranking["features"]["grade_a_rate"]["missing_reason"] == "unavailable"
    assert ranking["final_score"] is None


def _instructor_fallback():
    return {
        "found": True, "status": "ok", "source": "zotistics", "scope": "instructor",
        "fallback_from_scope": "course_instructor", "requested_course_id": "ICS33",
        "grades": {"course_id": None, "instructor_name": "LECTURER, A.",
                   "graded": 100, "pct_A": 80, "scope": "instructor"},
    }


def test_explicit_matching_instructor_fallback_scores_but_remains_career_scoped():
    source = _instructor_fallback()
    before = deepcopy(source)
    features = build_candidate_features(source, course_id="ICS33", instructor_name="LECTURER, A.")
    ranking = rank_candidates([{"course_id": "ICS33", "features": features}])[0]["ranking"]
    grade = ranking["features"]["grade_a_rate"]
    assert grade["valid"] is True
    assert grade["scope"] == "instructor"
    assert grade["course_id"] is None
    assert grade["fallback"] is True
    assert grade["requested_course_id"] == "ICS33"
    assert grade["uncertain_reason"] == "instructor_wide_fallback"
    assert grade["missing_reason"] is None
    assert ranking["final_score"] == 65
    assert "grade_a_rate" not in ranking["missing_features"]
    assert "grade_a_rate" in ranking["uncertain_features"]
    assert source == before


@pytest.mark.parametrize("field,value", [
    ("fallback_from_scope", None), ("fallback_from_scope", "course"),
    ("requested_course_id", None), ("requested_course_id", "MATH2A"),
    ("source_instructor", None), ("source_instructor", "OTHER, B."),
])
def test_instructor_fallback_needs_all_explicit_scope_and_identity_matches(field, value):
    source = _instructor_fallback()
    if field == "source_instructor":
        source["grades"]["instructor_name"] = value
    else:
        source[field] = value
    feature = build_candidate_features(source, course_id="ICS33", instructor_name="LECTURER, A.")["grade_a_rate"]
    assert feature["missing_reason"] == "instructor_history_not_course_scoped"
    assert rank_candidates([{"features": {"grade_a_rate": feature}}])[0]["ranking"]["final_score"] is None


def test_unavailable_envelope_cannot_score_even_if_found_flag_and_values_are_present():
    data = {"found": True, "status": "unavailable", "grades": {"graded": 100, "pct_A": 80}}
    feature = build_candidate_features(data)["grade_a_rate"]
    assert feature["missing_reason"] == "unavailable"
    assert feature["value"] is None


def _verified_fullname_history():
    return {
        "found": True, "status": "ok", "source": "zotistics", "scope": "course_instructor",
        "query": {"course_id": "ICS33", "instructor_name": "RAINER KLEFSTAD"},
        "resolved_instructor_name": "KLEFSTAD, R.",
        "grades": {"course_id": "ICS33", "instructor_name": "KLEFSTAD, R.",
                   "graded": 100, "pct_A": 80},
    }


@pytest.mark.parametrize("candidate_name", ["Rainer Klefstad", "KLEFSTAD, R"])
def test_verified_source_fullname_to_resolved_alias_is_accepted_without_name_guessing(candidate_name):
    features = build_candidate_features(_verified_fullname_history(), course_id="ICS33", instructor_name=candidate_name)
    ranking = rank_candidates([{"features": features}])[0]["ranking"]
    assert ranking["final_score"] == 65
    assert ranking["features"]["grade_a_rate"]["instructor_name"] == "KLEFSTAD, R."


@pytest.mark.parametrize("change", ["different_fullname", "missing_resolved", "resolved_mismatch", "query_mismatch"])
def test_only_verified_exact_source_alias_can_bridge_fullname_and_initials(change):
    source = _verified_fullname_history()
    candidate_name = "Rainer Klefstad"
    if change == "different_fullname":
        candidate_name = "Roger Klefstad"
    elif change == "missing_resolved":
        source.pop("resolved_instructor_name")
    elif change == "resolved_mismatch":
        source["resolved_instructor_name"] = "KLEFSTAD, S."
    else:
        source["query"]["instructor_name"] = "ROGER KLEFSTAD"
    feature = build_candidate_features(source, course_id="ICS33", instructor_name=candidate_name)["grade_a_rate"]
    assert feature["missing_reason"] == "grade_instructor_mismatch"


def test_verified_fullname_alias_also_applies_to_explicit_person_fallback():
    source = _verified_fullname_history()
    source.update({"scope": "instructor", "fallback_from_scope": "course_instructor", "requested_course_id": "ICS33"})
    source["grades"]["course_id"] = None
    feature = build_candidate_features(source, course_id="ICS33", instructor_name="Rainer Klefstad")["grade_a_rate"]
    assert feature["scope"] == "instructor"
    assert feature["fallback"] is True
    assert rank_candidates([{"features": {"grade_a_rate": feature}}])[0]["ranking"]["final_score"] == 65


@pytest.mark.parametrize("scope", ["course_instructor", "instructor"])
@pytest.mark.parametrize("primary,instructor_name,reason", [
    ({"section_type": "Dis", "instructors": ["LECTURER, A."]}, "LECTURER, A.", "not_a_primary_instructor"),
    ({"section_type": "Lec", "instructors": ["OTHER, B."]}, "LECTURER, A.", "instructor_not_in_primary_section"),
])
def test_pair_and_person_fallback_grades_require_actual_primary_role_and_membership(scope, primary, instructor_name, reason):
    source = _instructor_fallback()
    if scope == "course_instructor":
        source["scope"] = scope
        source["grades"]["course_id"] = "ICS33"
        source["grades"]["scope"] = scope
    features = build_candidate_features(source, {"avg_rating": 5, "num_ratings": 100},
        course_id="ICS33", instructor_name=instructor_name, primary_section=primary)
    assert features["grade_a_rate"]["missing_reason"] == reason
    assert features["professor_rating"]["missing_reason"] == reason
    assert rank_candidates([{"features": features}])[0]["ranking"]["final_score"] is None


def test_course_aggregate_stays_course_scoped_when_primary_role_cannot_be_verified():
    features = build_candidate_features({"scope": "course", "grades": {
        "course_id": "ICS33", "graded": 100, "pct_A": 80,
    }}, {"avg_rating": 5, "num_ratings": 100}, course_id="ICS33", instructor_name="ASSISTANT, T.",
       primary_section={"section_type": "Dis", "instructors": ["ASSISTANT, T."]})
    ranking = rank_candidates([{"features": features}])[0]["ranking"]
    assert ranking["features"]["grade_a_rate"]["scope"] == "course"
    assert ranking["features"]["grade_a_rate"]["instructor_name"] is None
    assert ranking["features"]["professor_rating"]["missing_reason"] == "not_a_primary_instructor"
    assert ranking["final_score"] == 65
