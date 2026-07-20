"""SQLite history and explainable local similarity for public deep searches."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any, Iterable, Optional
from urllib.parse import urlsplit

from app.catalog.departments import DEPARTMENT_ALIASES
from app.catalog.normalization import iter_course_mentions
from app.data.deep_search import normalize_url


DB_PATH = Path("data/deep_search_history.db")
FEATURE_DIMENSIONS = 256
DEFAULT_SIMILARITY_THRESHOLD = 0.42
MAX_HISTORY_SCAN = 500
MAX_ANSWER_SUMMARY_CHARS = 1_200

_PATH_FIELDS = {
    "url",
    "normalized_url",
    "depth",
    "parent_url",
    "ok",
    "source_class",
    "retrieved_at",
}
_INTENT_PATTERNS = {
    "availability": (r"\bopen\b", r"\bseat", r"\bfull\b", r"waitlist", r"空位", r"位置", r"满"),
    "restriction": (r"restriction", r"\bNORS?\b", r"专业限制", r"限制.*解除", r"non[- ]?major"),
    "deadline": (r"deadline", r"when", r"date", r"截止", r"什么时候", r"日期"),
    "professor": (r"professor", r"instructor", r"教授", r"老师"),
    "review": (r"review", r"rating", r"评价", r"口碑", r"评分"),
    "news": (r"news", r"announcement", r"update", r"新闻", r"公告", r"更新"),
}
_PERSONAL_PATTERNS = (
    ("gpa", r"\bGPA\b|grade point average|绩点"),
    ("completed_courses", r"completed courses?|courses? (?:I|I've) (?:taken|completed)|已修课程|我修过|我已经修|成绩单|transcript"),
    ("student_profile", r"student profile|学生画像|个人档案|student id|学号|ucinetid"),
    ("personal_major", r"\b(?:my|your) (?:[A-Za-z&]+ )?major\b|\bI am (?:an? )?[A-Za-z& ]+ major\b|我的专业|你的专业|我是[^，。,.]{0,20}专业"),
    ("class_level", r"\bI am (?:a |an )?(?:freshman|sophomore|junior|senior)\b|\b(?:my|your) class level\b|\byour (?:freshman|sophomore|junior|senior) (?:status|year|class level)\b|我是大[一二三四]|我的年级|你的年级"),
    ("personal_plan", r"\bmy (?:academic |course )?plan\b|\bI plan to take\b|个人计划|我的计划|我计划|我打算"),
    ("personal_schedule", r"\bmy schedule\b|\bmy timetable\b|我的课表|我的选课|我的时间表"),
    ("personal_preference", r"\bmy preferences?\b|\bI prefer\b|我的偏好|我偏好|我更喜欢"),
)
_TOKEN_STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "at",
    "for",
    "in",
    "is",
    "of",
    "on",
    "the",
    "to",
    "uci",
    "what",
}


def normalize_query(query: str) -> str:
    value = (query or "").strip().lower()
    value = re.sub(r"[^\w\u3400-\u9fff&]+", " ", value, flags=re.UNICODE)
    return re.sub(r"\s+", " ", value).strip()


def personal_context_reasons(query: str, final_answer: str = "") -> list[str]:
    """Return reasons that make a trace ineligible for global history."""

    text = f"{query or ''}\n{final_answer or ''}"
    return [name for name, pattern in _PERSONAL_PATTERNS if re.search(pattern, text, re.IGNORECASE)]


def build_local_features(query: str, *, urls: Optional[Iterable[str]] = None) -> dict[str, Any]:
    normalized = normalize_query(query)
    tokens = sorted(_query_tokens(normalized))
    intents = sorted(
        name
        for name, patterns in _INTENT_PATTERNS.items()
        if any(re.search(pattern, query or "", re.IGNORECASE) for pattern in patterns)
    )
    courses = sorted(
        {ref.display() for ref, _start, _end in iter_course_mentions(query or "")}
    )
    departments = sorted(_extract_departments(query or ""))
    domains, path_tokens = _url_features(urls or [])
    vector_terms = [*tokens, *(f"intent:{item}" for item in intents)]
    vector_terms.extend(f"course:{item}" for item in courses)
    vector_terms.extend(f"department:{item}" for item in departments)
    vector = _hash_vector(vector_terms)
    signature = {
        "tokens": tokens,
        "intents": intents,
        "courses": courses,
        "departments": departments,
        "url_domains": domains,
        "url_path_tokens": path_tokens,
        "vector": vector,
    }
    encoded = json.dumps(signature, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    signature["feature_id"] = hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:24]
    return signature


def similarity(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any]:
    left_tokens = set(left.get("tokens") or [])
    right_tokens = set(right.get("tokens") or [])
    matched_tokens = sorted(left_tokens & right_tokens)
    token_union = left_tokens | right_tokens
    token_jaccard = len(matched_tokens) / len(token_union) if token_union else 0.0

    matched_intents = sorted(set(left.get("intents") or []) & set(right.get("intents") or []))
    intent_union = set(left.get("intents") or []) | set(right.get("intents") or [])
    intent_score = len(matched_intents) / len(intent_union) if intent_union else 0.0

    left_entities = set(left.get("courses") or []) | set(left.get("departments") or [])
    right_entities = set(right.get("courses") or []) | set(right.get("departments") or [])
    matched_entities = sorted(left_entities & right_entities)
    entity_union = left_entities | right_entities
    entity_score = len(matched_entities) / len(entity_union) if entity_union else 0.0

    cosine = _cosine(left.get("vector") or {}, right.get("vector") or {})
    score = 0.55 * cosine + 0.20 * token_jaccard + 0.15 * intent_score + 0.10 * entity_score
    return {
        "score": round(score, 6),
        "components": {
            "local_vector_cosine": round(cosine, 6),
            "token_jaccard": round(token_jaccard, 6),
            "intent_overlap": round(intent_score, 6),
            "entity_overlap": round(entity_score, 6),
        },
        "matched_tokens": matched_tokens[:12],
        "matched_intents": matched_intents,
        "matched_entities": matched_entities,
    }


class DeepSearchHistoryStore:
    def __init__(self, db_path: Optional[str | Path] = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DB_PATH

    def record_trace(
        self,
        *,
        query: str,
        final_answer: str,
        url_path: list[dict[str, Any]],
        source_urls: list[str],
        fallback_search_used: bool,
    ) -> dict[str, Any]:
        privacy_reasons = personal_context_reasons(query, final_answer)
        if privacy_reasons:
            return {
                "stored": False,
                "reason": "personal_context",
                "privacy_reasons": privacy_reasons,
            }
        if not url_path:
            return {"stored": False, "reason": "no_deep_search_path"}

        normalized = normalize_query(query)
        sanitized_path = _sanitize_url_path(url_path)
        sanitized_sources = _sanitize_source_urls(source_urls)
        feature_urls = [item["url"] for item in sanitized_path if item.get("url")]
        features = build_local_features(query, urls=feature_urls)
        similar = self.find_similar(query, limit=1, threshold=DEFAULT_SIMILARITY_THRESHOLD)
        cluster_id = (
            similar[0]["cluster_id"]
            if similar
            else f"cluster_{features['feature_id'][:12]}"
        )
        now = _utc_now()
        max_depth = max((int(item.get("depth") or 0) for item in sanitized_path), default=0)
        answer_summary = _summarize_answer(final_answer)

        with self._connect() as conn:
            cluster = conn.execute(
                "SELECT occurrence_count, first_seen_at FROM deep_search_clusters WHERE cluster_id = ?",
                (cluster_id,),
            ).fetchone()
            occurrence_count = int(cluster["occurrence_count"]) + 1 if cluster else 1
            first_seen_at = str(cluster["first_seen_at"]) if cluster else now
            conn.execute(
                """
                INSERT INTO deep_search_clusters (
                    cluster_id, canonical_query, occurrence_count, first_seen_at, last_seen_at
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cluster_id) DO UPDATE SET
                    occurrence_count = excluded.occurrence_count,
                    last_seen_at = excluded.last_seen_at
                """,
                (cluster_id, normalized, occurrence_count, first_seen_at, now),
            )
            cursor = conn.execute(
                """
                INSERT INTO deep_search_traces (
                    normalized_query, feature_id, feature_json, cluster_id,
                    url_path_json, max_depth, fallback_search_used,
                    final_answer_summary, final_source_urls_json,
                    occurrence_count, first_seen_at, last_seen_at,
                    workflow_candidate, candidate_reason, review_status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, NULL, 'unreviewed')
                """,
                (
                    normalized,
                    features["feature_id"],
                    json.dumps(features, ensure_ascii=False, sort_keys=True),
                    cluster_id,
                    json.dumps(sanitized_path, ensure_ascii=False),
                    max_depth,
                    int(bool(fallback_search_used)),
                    answer_summary,
                    json.dumps(sanitized_sources, ensure_ascii=False),
                    occurrence_count,
                    now,
                    now,
                ),
            )
            trace_id = int(cursor.lastrowid)

        return {
            "stored": True,
            "trace_id": trace_id,
            "cluster_id": cluster_id,
            "feature_id": features["feature_id"],
            "occurrence_count": occurrence_count,
            "max_depth": max_depth,
        }

    def find_similar(
        self,
        query: str,
        *,
        limit: int = 3,
        threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    ) -> list[dict[str, Any]]:
        if not self.db_path.exists():
            return []
        query_features = build_local_features(query)
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*, c.occurrence_count AS cluster_occurrence_count,
                       c.last_seen_at AS cluster_last_seen_at
                FROM deep_search_traces t
                JOIN deep_search_clusters c ON c.cluster_id = t.cluster_id
                ORDER BY t.last_seen_at DESC
                LIMIT ?
                """,
                (MAX_HISTORY_SCAN,),
            ).fetchall()

        best_by_cluster: dict[str, dict[str, Any]] = {}
        for row in rows:
            stored_features = json.loads(row["feature_json"])
            match = similarity(query_features, stored_features)
            if match["score"] < threshold:
                continue
            cluster_id = str(row["cluster_id"])
            candidate = {
                "trace_id": int(row["id"]),
                "cluster_id": cluster_id,
                "normalized_query": str(row["normalized_query"]),
                "feature_id": str(row["feature_id"]),
                "similarity": match,
                "url_path": json.loads(row["url_path_json"]),
                "max_depth": int(row["max_depth"]),
                "fallback_search_used": bool(row["fallback_search_used"]),
                "final_answer_summary": row["final_answer_summary"],
                "source_urls": json.loads(row["final_source_urls_json"]),
                "hit_count": int(row["cluster_occurrence_count"]),
                "last_seen_at": str(row["cluster_last_seen_at"]),
                "workflow_candidate": bool(row["workflow_candidate"]),
                "review_status": str(row["review_status"]),
            }
            existing = best_by_cluster.get(cluster_id)
            if existing is None or candidate["similarity"]["score"] > existing["similarity"]["score"]:
                best_by_cluster[cluster_id] = candidate

        return sorted(
            best_by_cluster.values(),
            key=lambda item: (item["similarity"]["score"], item["hit_count"], item["last_seen_at"]),
            reverse=True,
        )[: max(0, limit)]

    def _connect(self) -> sqlite3.Connection:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS deep_search_clusters (
                cluster_id TEXT PRIMARY KEY,
                canonical_query TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS deep_search_traces (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                normalized_query TEXT NOT NULL,
                feature_id TEXT NOT NULL,
                feature_json TEXT NOT NULL,
                cluster_id TEXT NOT NULL,
                url_path_json TEXT NOT NULL,
                max_depth INTEGER NOT NULL,
                fallback_search_used INTEGER NOT NULL DEFAULT 0,
                final_answer_summary TEXT,
                final_source_urls_json TEXT NOT NULL,
                occurrence_count INTEGER NOT NULL DEFAULT 1,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                workflow_candidate INTEGER NOT NULL DEFAULT 0,
                candidate_reason TEXT,
                review_status TEXT NOT NULL DEFAULT 'unreviewed',
                FOREIGN KEY(cluster_id) REFERENCES deep_search_clusters(cluster_id)
            );

            CREATE INDEX IF NOT EXISTS idx_deep_search_traces_cluster
                ON deep_search_traces(cluster_id);
            CREATE INDEX IF NOT EXISTS idx_deep_search_traces_last_seen
                ON deep_search_traces(last_seen_at DESC);
            """
        )
        return conn


def _query_tokens(normalized_query: str) -> set[str]:
    tokens = {
        token
        for token in re.findall(r"[a-z0-9&]+", normalized_query)
        if len(token) > 1 and token not in _TOKEN_STOPWORDS
    }
    for run in re.findall(r"[\u3400-\u9fff]+", normalized_query):
        if len(run) <= 4:
            tokens.add(run)
        tokens.update(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return tokens


def _extract_departments(text: str) -> set[str]:
    lowered = text.lower()
    found: set[str] = set()
    for canonical, aliases in DEPARTMENT_ALIASES.items():
        for token in (canonical, *aliases):
            escaped = re.escape(token.lower())
            if re.search(rf"(?<![a-z0-9]){escaped}(?![a-z0-9])", lowered):
                found.add(canonical)
                break
    return found


def _url_features(urls: Iterable[str]) -> tuple[list[str], list[str]]:
    domains: set[str] = set()
    path_tokens: set[str] = set()
    for url in urls:
        try:
            parsed = urlsplit(url)
        except ValueError:
            continue
        if parsed.hostname:
            domains.add(parsed.hostname.lower().removeprefix("www."))
        path_tokens.update(
            token.lower()
            for token in re.findall(r"[A-Za-z0-9]+", parsed.path)
            if len(token) > 2
        )
    return sorted(domains), sorted(path_tokens)


def _hash_vector(terms: Iterable[str]) -> dict[str, float]:
    counts: Counter[int] = Counter()
    for term in terms:
        digest = hashlib.blake2b(term.encode("utf-8"), digest_size=8).digest()
        index = int.from_bytes(digest, "big") % FEATURE_DIMENSIONS
        counts[index] += 1
    magnitude = math.sqrt(sum(value * value for value in counts.values()))
    if magnitude == 0:
        return {}
    return {str(index): value / magnitude for index, value in sorted(counts.items())}


def _cosine(left: dict[str, float], right: dict[str, float]) -> float:
    if not left or not right:
        return 0.0
    return sum(float(value) * float(right.get(index, 0.0)) for index, value in left.items())


def _sanitize_url_path(url_path: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sanitized: list[dict[str, Any]] = []
    for item in url_path:
        if not isinstance(item, dict):
            continue
        row = {key: item.get(key) for key in _PATH_FIELDS if key in item}
        if row.get("url"):
            row["url"] = str(row["url"])
            row["normalized_url"] = normalize_url(str(row.get("normalized_url") or row["url"]))
        row["depth"] = int(row.get("depth") or 0)
        sanitized.append(row)
    return sanitized


def _sanitize_source_urls(source_urls: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(str(url).strip() for url in source_urls if str(url).strip()))


def _summarize_answer(answer: str) -> str:
    value = re.sub(r"\s+", " ", answer or "").strip()
    if len(value) <= MAX_ANSWER_SUMMARY_CHARS:
        return value
    return value[: MAX_ANSWER_SUMMARY_CHARS - 3].rstrip() + "..."


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()
