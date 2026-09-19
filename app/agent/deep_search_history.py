"""Agent-facing history hints and trace lifecycle helpers."""

from __future__ import annotations

import logging
import re
from typing import Any, Optional

from app import observability
from app.agent.deep_search_state import DeepSearchRunState
from app.data.deep_search import normalize_url
from app.data.deep_search_history import DeepSearchHistoryStore


logger = logging.getLogger(__name__)
_URL_PATTERN = re.compile(r"https?://[^\s)\]>]+", re.IGNORECASE)


def load_history_hint(
    query: str,
    *,
    store: Optional[DeepSearchHistoryStore] = None,
) -> tuple[list[dict[str, Any]], Optional[dict[str, str]]]:
    history = store or DeepSearchHistoryStore()
    try:
        matches = history.find_similar(query, limit=2)
    except Exception as exc:
        logger.warning("deep-search history lookup failed: %s: %s", type(exc).__name__, exc)
        observability.increment("deep_search.history_lookup_failures")
        return [], None
    return matches, build_history_hint_message(matches)


def build_history_hint_message(matches: list[dict[str, Any]]) -> Optional[dict[str, str]]:
    if not matches:
        return None

    lines = [
        "Deep-search history hint (strong recommendation, never cached proof):",
        "Start from these previously useful public paths when relevant, but you may visit additional pages.",
        "You MUST successfully call fetch_page for at least one source in this run before answering.",
        "Do not present the historical answer summary as current fact and do not say history proves anything.",
    ]
    for index, match in enumerate(matches, start=1):
        path = [
            f"depth {item.get('depth')}: {item.get('url')}"
            for item in match.get("url_path", [])
            if item.get("url")
        ]
        explanation = match.get("similarity") or {}
        lines.extend(
            [
                f"Match {index}: cluster={match.get('cluster_id')} score={explanation.get('score')} hits={match.get('hit_count')}",
                f"Prior public query: {match.get('normalized_query')}",
                f"Recommended max depth: {match.get('max_depth')}",
                f"Recommended path: {path}",
                f"Prior answer summary (reference only): {match.get('final_answer_summary')}",
                f"Prior sources: {match.get('source_urls') or []}",
                f"Last seen: {match.get('last_seen_at')}",
                (
                    "Similarity explanation: matched tokens="
                    f"{explanation.get('matched_tokens') or []}, intents="
                    f"{explanation.get('matched_intents') or []}, entities="
                    f"{explanation.get('matched_entities') or []}"
                ),
            ]
        )
    return {"role": "system", "content": "\n".join(lines)}


def record_run_trace(
    state: DeepSearchRunState,
    final_answer: str,
    *,
    store: Optional[DeepSearchHistoryStore] = None,
) -> dict[str, Any]:
    if state.trace_recorded:
        return {"stored": False, "reason": "already_recorded"}
    history = store or DeepSearchHistoryStore()
    sources = state.successful_source_urls()
    cited_urls = _extract_urls(final_answer)
    cited_normalized = {normalize_url(url) for url in cited_urls}
    sources = [
        url for url in sources
        if normalize_url(url) in cited_normalized or not cited_normalized
    ]
    try:
        result = history.record_trace(
            query=state.query,
            final_answer=final_answer,
            url_path=state.visited_path(),
            source_urls=sources,
            fallback_search_used=state.fallback_search_used,
        )
    except Exception as exc:
        logger.warning("deep-search trace persistence failed: %s: %s", type(exc).__name__, exc)
        observability.increment("deep_search.history_write_failures")
        return {"stored": False, "reason": "history_write_failed"}

    if result.get("stored"):
        state.trace_recorded = True
    observability.increment(
        "deep_search.history_writes",
        stored=str(bool(result.get("stored"))).lower(),
        reason=str(result.get("reason") or "stored"),
    )
    return result


def history_refresh_missing(state: DeepSearchRunState) -> bool:
    return bool(state.history_matches) and not state.has_successful_fetch


def verification_required_text(query: str) -> str:
    if re.search(r"[\u3400-\u9fff]", query or ""):
        return "找到了相似的历史搜索路径，但本次尚未重新成功读取任何来源页面，因此我现在无法验证并复用旧答案。"
    return (
        "A similar historical search path was found, but no source page was "
        "successfully rechecked in this run, so I cannot verify or reuse the old answer."
    )


def _extract_urls(text: str) -> set[str]:
    return {match.rstrip(".,;:") for match in _URL_PATTERN.findall(text or "")}
