"""Provider-neutral search adapters.

The mock provider is intentionally deterministic and performs no network I/O.
Real search services can implement the same protocol without changing Agent,
CLI, Web, or evidence contracts.
"""

from __future__ import annotations

import hashlib
from typing import Protocol, Sequence

from valuationagent.schemas.agent import SearchHit, SearchQuery, SearchResult


class SearchProvider(Protocol):
    provider_id: str
    version: str

    def search(self, query: SearchQuery) -> SearchResult: ...


class UnavailableSearchProvider:
    """Explicit placeholder used until a network provider is configured."""

    provider_id = "unavailable"
    version = "0.1"

    def search(self, query: SearchQuery) -> SearchResult:
        return SearchResult(
            query=query,
            provider=self.provider_id,
            provider_version=self.version,
            status="not_configured",
            warnings=["未配置联网搜索 provider；没有发出网络请求。"],
        )


class MockSearchProvider:
    """Deterministic fixture-backed provider for workflow and UI acceptance."""

    provider_id = "mock-search"
    version = "0.1"

    def __init__(self, hits: Sequence[SearchHit | dict] = ()):
        self._hits = [item if isinstance(item, SearchHit) else SearchHit.model_validate(item) for item in hits]

    @staticmethod
    def _query_id(query: SearchQuery) -> str:
        digest = hashlib.sha256(query.model_dump_json().encode("utf-8")).hexdigest()[:20]
        return f"search_{digest}"

    def search(self, query: SearchQuery) -> SearchResult:
        terms = [term.lower() for term in query.query.split() if term.strip()]
        ranked = []
        for hit in self._hits:
            haystack = f"{hit.title} {hit.domain} {hit.snippet}".lower()
            score = sum(term in haystack for term in terms) / max(len(terms), 1)
            if score or not terms:
                ranked.append(hit.model_copy(update={"relevance": round(score, 4)}))
        ranked.sort(key=lambda item: (-item.relevance, item.source_id))
        ranked = ranked[: query.candidate_limit]
        warnings = [
            f"mock provider；结果仅用于联调，不代表已完成公开资料核验。",
            f"query_id={self._query_id(query)}",
        ]
        return SearchResult(
            query=query,
            provider=self.provider_id,
            provider_version=self.version,
            status="completed" if ranked else "no_results",
            hits=ranked,
            warnings=warnings,
        )
