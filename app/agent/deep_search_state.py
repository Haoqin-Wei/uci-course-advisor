"""Per-run deep-search memory, link-depth accounting, and hard budgets."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from app.data import deep_search


MAX_DEEP_SEARCH_DEPTH = 8
MAX_DEEP_SEARCH_PAGES = 8


@dataclass
class DeepSearchRunState:
    """Mutable state scoped to one agent answer."""

    query: str = ""
    max_depth: int = MAX_DEEP_SEARCH_DEPTH
    max_pages: int = MAX_DEEP_SEARCH_PAGES
    visited: dict[str, dict[str, Any]] = field(default_factory=dict)
    visit_order: list[str] = field(default_factory=list)
    entrypoint_urls: set[str] = field(default_factory=set)
    discovered_depths: dict[str, int] = field(default_factory=dict)
    discovered_parents: dict[str, set[str]] = field(default_factory=dict)
    fetch_attempts: int = 0
    block_reason: Optional[str] = None
    fallback_search_used: bool = False
    search_queries: list[str] = field(default_factory=list)
    history_matches: list[dict[str, Any]] = field(default_factory=list)
    trace_recorded: bool = False

    @property
    def fetch_blocked(self) -> bool:
        return self.block_reason is not None

    @property
    def has_successful_fetch(self) -> bool:
        return any(record.get("ok") for record in self.visited.values())

    def register_search_result(self, result: dict[str, Any]) -> dict[str, Any]:
        """Register depth-0 entrypoints and mark post-limit fallback searches."""

        query = str(result.get("query") or "").strip()
        if query:
            self.search_queries.append(query)

        is_fallback = self.fetch_blocked
        if is_fallback:
            self.fallback_search_used = True

        for item in result.get("results", []):
            if not isinstance(item, dict) or not item.get("url"):
                continue
            normalized = deep_search.normalize_url(str(item["url"]))
            item["normalized_url"] = normalized
            item["depth"] = 0
            if not is_fallback:
                self.entrypoint_urls.add(normalized)

        result["search_depth"] = 0
        result["fallback_after_deep_limit"] = is_fallback
        result["fetch_page_allowed"] = not self.fetch_blocked
        if self.block_reason:
            result["deep_search_limit_reason"] = self.block_reason
        return result

    def fetch(self, url: str, *, parent_url: Optional[str] = None) -> dict[str, Any]:
        normalized = deep_search.normalize_url(url)
        if normalized in self.visited:
            previous = self.visited[normalized]
            return {
                "ok": False,
                "error_code": "already_visited",
                "message": "this normalized URL was already fetched in the current run",
                "source_url": url,
                "normalized_url": normalized,
                "visited": self._visited_reference(previous),
                "budget": self.budget_status(),
            }

        if self.fetch_blocked:
            return self._limit_error(url, normalized)

        depth_result = self._resolve_depth(normalized, parent_url=parent_url)
        if not depth_result["ok"]:
            return {**depth_result, "source_url": url, "normalized_url": normalized, "budget": self.budget_status()}
        depth = int(depth_result["depth"])
        normalized_parent = depth_result.get("normalized_parent")

        if depth > self.max_depth:
            self.block_reason = "max_depth"
            return self._limit_error(url, normalized, attempted_depth=depth)
        if self.fetch_attempts >= self.max_pages:
            self.block_reason = "max_pages"
            return self._limit_error(url, normalized, attempted_depth=depth)

        self.fetch_attempts += 1
        result = deep_search.fetch_page(url)
        result["depth"] = depth
        result["parent_url"] = parent_url
        result["normalized_parent_url"] = normalized_parent

        record = {
            "url": url,
            "normalized_url": normalized,
            "final_url": result.get("final_url"),
            "depth": depth,
            "parent_url": parent_url,
            "normalized_parent_url": normalized_parent,
            "ok": result.get("ok") is True,
            "error_code": result.get("error_code"),
            "title": result.get("title"),
            "summary": result.get("summary"),
            "source_class": result.get("source_class"),
            "trust_level": result.get("trust_level"),
            "retrieved_at": result.get("retrieved_at"),
            "linked_urls": set(),
        }
        if result.get("ok"):
            for link in result.get("links", []):
                if not isinstance(link, dict) or not link.get("normalized_url"):
                    continue
                linked = str(link["normalized_url"])
                record["linked_urls"].add(linked)
                candidate_depth = depth + 1
                existing_depth = self.discovered_depths.get(linked)
                if existing_depth is None or candidate_depth < existing_depth:
                    self.discovered_depths[linked] = candidate_depth
                self.discovered_parents.setdefault(linked, set()).add(normalized)

        self.visited[normalized] = record
        self.visit_order.append(normalized)

        if depth >= self.max_depth:
            self.block_reason = "max_depth"
        elif self.fetch_attempts >= self.max_pages:
            self.block_reason = "max_pages"

        result["budget"] = self.budget_status()
        return result

    def budget_status(self) -> dict[str, Any]:
        return {
            "max_depth": self.max_depth,
            "max_pages": self.max_pages,
            "pages_used": self.fetch_attempts,
            "fetch_blocked": self.fetch_blocked,
            "limit_reason": self.block_reason,
        }

    def visited_path(self) -> list[dict[str, Any]]:
        return [
            {
                "url": self.visited[key].get("final_url") or self.visited[key]["url"],
                "normalized_url": key,
                "depth": self.visited[key]["depth"],
                "parent_url": self.visited[key].get("parent_url"),
                "ok": self.visited[key]["ok"],
                "source_class": self.visited[key].get("source_class"),
                "retrieved_at": self.visited[key].get("retrieved_at"),
            }
            for key in self.visit_order
        ]

    def successful_source_urls(self) -> list[str]:
        return list(
            dict.fromkeys(
                str(self.visited[key].get("final_url") or self.visited[key]["url"])
                for key in self.visit_order
                if self.visited[key]["ok"]
            )
        )

    def _resolve_depth(self, normalized: str, *, parent_url: Optional[str]) -> dict[str, Any]:
        if parent_url:
            normalized_parent = deep_search.normalize_url(parent_url)
            parent = self.visited.get(normalized_parent)
            if not parent or not parent["ok"]:
                return {
                    "ok": False,
                    "error_code": "invalid_parent",
                    "message": "parent_url must be a successfully fetched page in the current run",
                }
            if normalized not in parent["linked_urls"]:
                return {
                    "ok": False,
                    "error_code": "parent_link_mismatch",
                    "message": "the requested URL was not extracted from parent_url",
                }
            return {
                "ok": True,
                "depth": int(parent["depth"]) + 1,
                "normalized_parent": normalized_parent,
            }

        if normalized in self.entrypoint_urls:
            return {"ok": True, "depth": 1, "normalized_parent": None}
        if normalized in self.discovered_depths:
            return {
                "ok": True,
                "depth": self.discovered_depths[normalized],
                "normalized_parent": None,
            }
        return {"ok": True, "depth": 1, "normalized_parent": None}

    def _limit_error(
        self,
        url: str,
        normalized: str,
        *,
        attempted_depth: Optional[int] = None,
    ) -> dict[str, Any]:
        return {
            "ok": False,
            "error_code": "deep_search_limit_reached",
            "message": (
                "deep-search fetch budget is exhausted; use ordinary web_search "
                "for supplemental result summaries and do not call fetch_page again"
            ),
            "source_url": url,
            "normalized_url": normalized,
            "attempted_depth": attempted_depth,
            "budget": self.budget_status(),
        }

    @staticmethod
    def _visited_reference(record: dict[str, Any]) -> dict[str, Any]:
        return {
            "url": record.get("final_url") or record.get("url"),
            "normalized_url": record.get("normalized_url"),
            "depth": record.get("depth"),
            "ok": record.get("ok"),
            "error_code": record.get("error_code"),
            "title": record.get("title"),
            "summary": record.get("summary"),
            "source_class": record.get("source_class"),
            "trust_level": record.get("trust_level"),
            "retrieved_at": record.get("retrieved_at"),
        }
