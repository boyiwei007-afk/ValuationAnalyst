"""Provider-neutral search adapters.

The mock provider is intentionally deterministic and performs no network I/O.
Real search services can implement the same protocol without changing Agent,
CLI, Web, or evidence contracts.
"""

from __future__ import annotations

import hashlib
import os
from datetime import date
from typing import Protocol, Sequence
from urllib.parse import urlsplit

import httpx

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


class TavilySearchProvider:
    """Real-time search adapter with source URLs and an explicit cutoff date."""

    provider_id = "tavily"
    version = "2025-03-search-v1"

    def __init__(
        self,
        api_key: str,
        *,
        endpoint: str = "https://api.tavily.com/search",
        timeout_seconds: float = 30,
        transport=None,
    ):
        if not api_key.strip():
            raise ValueError("Tavily API Key不能为空。")
        parsed = urlsplit(endpoint)
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or parsed.password:
            raise ValueError("搜索接口必须是无账号信息的HTTPS地址。")
        self._api_key = api_key.strip()
        self.endpoint = endpoint.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self._transport = transport

    @staticmethod
    def _date(value: date | None) -> str | None:
        return value.isoformat() if value else None

    def search(self, query: SearchQuery) -> SearchResult:
        payload = {
            "query": query.query,
            "topic": "general",
            "search_depth": "advanced",
            "max_results": query.candidate_limit,
            "include_answer": False,
            "include_raw_content": False,
            "include_images": False,
            "include_domains": query.allowed_domains,
        }
        cutoff = query.information_cutoff or query.as_of_date
        if cutoff:
            payload["end_date"] = self._date(cutoff)
        try:
            with httpx.Client(
                timeout=self.timeout_seconds,
                transport=self._transport,
                follow_redirects=False,
            ) as client:
                response = client.post(
                    self.endpoint,
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json=payload,
                )
                response.raise_for_status()
                body = response.json()
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code in {401, 403}:
                error_code = "SEARCH_AUTH_FAILED"
                message = "Tavily API Key 无效或无权访问，请重新配置 Tavily Key。"
            elif status_code == 429:
                error_code = "SEARCH_RATE_LIMITED"
                message = "Tavily 请求达到额度或频率上限，请稍后重试或检查账户额度。"
            else:
                error_code = f"SEARCH_HTTP_{status_code}"
                message = f"Tavily 服务返回 HTTP {status_code}，本次检索未完成。"
            return SearchResult(
                query=query,
                provider=self.provider_id,
                provider_version=self.version,
                status="failed",
                error_code=error_code,
                error_message=message,
                warnings=[message],
            )
        except httpx.TimeoutException:
            message = "Tavily 检索超时，本次没有取得搜索结果。"
            return SearchResult(
                query=query, provider=self.provider_id,
                provider_version=self.version, status="failed",
                error_code="SEARCH_TIMEOUT", error_message=message,
                warnings=[message],
            )
        except httpx.RequestError:
            message = "无法连接 Tavily，请检查网络、代理或防火墙设置。"
            return SearchResult(
                query=query, provider=self.provider_id,
                provider_version=self.version, status="failed",
                error_code="SEARCH_CONNECTION_FAILED", error_message=message,
                warnings=[message],
            )
        except (ValueError, TypeError):
            message = "Tavily 返回了无法识别的响应，本次没有采用任何搜索结果。"
            return SearchResult(
                query=query, provider=self.provider_id,
                provider_version=self.version, status="failed",
                error_code="SEARCH_RESPONSE_INVALID", error_message=message,
                warnings=[message],
            )
        hits = []
        for index, item in enumerate(body.get("results") or []):
            url = str(item.get("url") or "").strip()
            title = str(item.get("title") or "").strip()
            if not url or not title:
                continue
            domain = urlsplit(url).netloc.lower()
            published = item.get("published_date")
            try:
                published_at = date.fromisoformat(str(published)[:10]) if published else None
            except ValueError:
                published_at = None
            digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:24]
            hits.append(
                SearchHit(
                    source_id=f"tavily_{digest}",
                    title=title,
                    url=url,
                    domain=domain,
                    snippet=str(item.get("content") or "")[:2000],
                    published_at=published_at,
                    relevance=max(0, min(1, float(item.get("score") or 0))),
                )
            )
        warnings = []
        if cutoff:
            warnings.append(f"检索截止日：{cutoff.isoformat()}；结果仍需核对网页发布日期。")
        return SearchResult(
            query=query,
            provider=self.provider_id,
            provider_version=self.version,
            status="completed" if hits else "no_results",
            hits=hits,
            warnings=warnings,
        )


def create_search_provider():
    """Build the configured real provider without persisting its credential."""

    selected = os.getenv("VALUATION_SEARCH_PROVIDER", "auto").strip().lower()
    key = os.getenv("TAVILY_API_KEY") or os.getenv("VALUATION_SEARCH_API_KEY")
    if selected in {"none", "off", "unavailable"}:
        return UnavailableSearchProvider()
    if selected in {"auto", "tavily"} and key:
        return TavilySearchProvider(key)
    if selected not in {"auto", "tavily"}:
        raise ValueError("VALUATION_SEARCH_PROVIDER当前仅支持tavily或none。")
    return UnavailableSearchProvider()
