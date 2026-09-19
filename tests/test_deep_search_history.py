from __future__ import annotations

import json
import sqlite3

from app.data.deep_search_history import (
    DeepSearchHistoryStore,
    build_local_features,
    personal_context_reasons,
    similarity,
)


def _path(*urls: str) -> list[dict]:
    return [
        {
            "url": url,
            "normalized_url": url,
            "depth": index,
            "parent_url": urls[index - 2] if index > 1 else None,
            "ok": True,
            "source_class": "official_uci",
            "retrieved_at": "2026-07-21T00:00:00+00:00",
            "summary": "must never be stored",
            "key_passages": ["must never be stored"],
            "links": ["must never be stored"],
        }
        for index, url in enumerate(urls, start=1)
    ]


def test_public_trace_writes_only_whitelisted_history_fields(tmp_path) -> None:
    db_path = tmp_path / "history.db"
    store = DeepSearchHistoryStore(db_path)
    result = store.record_trace(
        query="When do ICS major restrictions lift?",
        final_answer="The official ICS page says restrictions change in late August.",
        url_path=_path(
            "https://reg.uci.edu/perl/WebSoc",
            "https://ics.uci.edu/course-enrollment-restrictions/",
        ),
        source_urls=["https://ics.uci.edu/course-enrollment-restrictions/"],
        fallback_search_used=False,
    )

    assert result["stored"] is True
    assert result["max_depth"] == 2
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM deep_search_traces").fetchone()
        columns = {item[1] for item in conn.execute("PRAGMA table_info(deep_search_traces)")}

    stored_path = json.loads(row["url_path_json"])
    assert set(stored_path[0]) <= {
        "url",
        "normalized_url",
        "depth",
        "parent_url",
        "ok",
        "source_class",
        "retrieved_at",
    }
    serialized = json.dumps(dict(row), ensure_ascii=False)
    assert "must never be stored" not in serialized
    assert {"workflow_candidate", "candidate_reason", "review_status"} <= columns
    assert row["review_status"] == "unreviewed"


def test_personal_query_or_answer_is_not_written(tmp_path) -> None:
    store = DeepSearchHistoryStore(tmp_path / "history.db")
    cases = [
        ("I have completed ICS 32 and my GPA is 3.7. What should I take?", "Try ICS 33."),
        ("What courses are open?", "Given your CS major and junior class level, take CS 161."),
        ("帮我安排课程", "根据我的课表调整个人计划。"),
    ]
    for query, answer in cases:
        result = store.record_trace(
            query=query,
            final_answer=answer,
            url_path=_path("https://reg.uci.edu/"),
            source_urls=["https://reg.uci.edu/"],
            fallback_search_used=False,
        )
        assert result["stored"] is False
        assert result["reason"] == "personal_context"

    assert not store.db_path.exists()
    assert personal_context_reasons("When do major restrictions lift?", "Public policy answer") == []


def test_local_similarity_is_explainable_for_policy_paraphrases() -> None:
    left = build_local_features("When do ICS major restrictions lift?")
    right = build_local_features("ICS restriction removal date")
    unrelated = build_local_features("How is Professor Smith rated?")

    related_match = similarity(left, right)
    unrelated_match = similarity(left, unrelated)
    assert related_match["score"] > unrelated_match["score"]
    assert "restriction" in related_match["matched_intents"]
    assert "I&C SCI" in related_match["matched_entities"]
    assert related_match["components"]["local_vector_cosine"] > 0


def test_similar_traces_share_cluster_and_increment_hit_count(tmp_path) -> None:
    store = DeepSearchHistoryStore(tmp_path / "history.db")
    first = store.record_trace(
        query="When do ICS major restrictions lift?",
        final_answer="Check the current ICS restrictions page.",
        url_path=_path("https://ics.uci.edu/course-enrollment-restrictions/"),
        source_urls=["https://ics.uci.edu/course-enrollment-restrictions/"],
        fallback_search_used=False,
    )
    matches = store.find_similar("ICS restriction removal date", threshold=0.30)
    assert matches
    assert matches[0]["similarity"]["matched_intents"] == ["deadline", "restriction"]
    assert matches[0]["url_path"][0]["depth"] == 1

    second = store.record_trace(
        query="ICS restriction removal date",
        final_answer="The official page should be checked again.",
        url_path=_path("https://ics.uci.edu/course-enrollment-restrictions/"),
        source_urls=["https://ics.uci.edu/course-enrollment-restrictions/"],
        fallback_search_used=True,
    )
    assert second["cluster_id"] == first["cluster_id"]
    assert second["occurrence_count"] == 2

    latest = store.find_similar("What date are ICS restrictions removed?", threshold=0.30)
    assert latest[0]["hit_count"] == 2
    assert latest[0]["cluster_id"] == first["cluster_id"]


def test_failed_trace_is_stored_for_review_but_not_recommended(tmp_path) -> None:
    store = DeepSearchHistoryStore(tmp_path / "history.db")
    result = store.record_trace(
        query="Where is the UCI policy update?",
        final_answer="I could not verify the page.",
        url_path=[
            {
                "url": "https://reg.uci.edu/unavailable",
                "depth": 1,
                "ok": False,
                "source_class": None,
                "retrieved_at": "2026-07-21T00:00:00+00:00",
            }
        ],
        source_urls=[],
        fallback_search_used=False,
    )
    assert result["stored"] is True
    assert store.find_similar("Where is the UCI policy update?", threshold=0.0) == []
