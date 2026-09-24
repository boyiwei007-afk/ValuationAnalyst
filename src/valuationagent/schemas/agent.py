"""Stable application-layer contracts for the valuation agent.

These models describe what the LLM may plan and explain. They deliberately do
not contain financial calculations; numeric valuation outputs remain owned by
the deterministic finance plugins.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Any, Literal

from pydantic import Field, field_validator

from valuationagent.schemas.models import ApiModel, Language


IntentKind = Literal[
    "new_valuation",
    "provide_material",
    "ask_explanation",
    "revise_assumption",
    "compare_versions",
    "policy_analysis",
    "request_export",
    "resume_review",
]


class IntentResult(ApiModel):
    """Structured interpretation of one user turn."""

    intent: IntentKind
    confidence: float = Field(ge=0, le=1)
    slots: dict[str, Any] = Field(default_factory=dict)
    requires_confirmation: bool = False
    rationale: str = Field(default="", max_length=1000)


class ContextSnapshot(ApiModel):
    """Compact context sent to an LLM instead of replaying all raw files."""

    session_id: str = Field(min_length=1, max_length=120)
    revision: int = Field(default=1, ge=1)
    language: Language = Language.ZH_CN
    summary: str = Field(default="", max_length=4000)
    task_state: dict[str, Any] = Field(default_factory=dict)
    confirmed_fact_ids: list[str] = Field(default_factory=list, max_length=500)
    evidence_ids: list[str] = Field(default_factory=list, max_length=500)
    open_questions: list[str] = Field(default_factory=list, max_length=30)
    recent_turns: list[dict[str, str]] = Field(default_factory=list, max_length=30)


class EvidenceItem(ApiModel):
    """A traceable source fragment used by extraction or explanation."""

    evidence_id: str = Field(min_length=1, max_length=160)
    source_type: Literal[
        "document", "user_note", "url", "market_data", "search_result", "policy"
    ]
    source: str = Field(min_length=1, max_length=1000)
    title: str = Field(default="", max_length=300)
    quote: str = Field(default="", max_length=4000)
    locator: dict[str, Any] = Field(default_factory=dict)
    published_at: date | None = None
    retrieved_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: Literal["proposed", "confirmed", "rejected", "reference"] = "proposed"
    extraction_version: str = Field(default="", max_length=80)


class SearchQuery(ApiModel):
    """Provider-neutral search request with explicit scope and budget."""

    query: str = Field(min_length=1, max_length=500)
    ticker: str | None = Field(default=None, max_length=24)
    company_name: str | None = Field(default=None, max_length=200)
    industry: str | None = Field(default=None, max_length=160)
    purpose: Literal["company_profile", "financials", "comparables", "policy", "other"] = "other"
    as_of_date: date | None = None
    information_cutoff: date | None = None
    candidate_limit: int = Field(default=8, ge=1, le=50)
    allowed_domains: list[str] = Field(default_factory=list, max_length=30)

    @field_validator("query", "ticker", "company_name", "industry")
    @classmethod
    def strip_optional_text(cls, value):
        return value.strip() if isinstance(value, str) else value

    @field_validator("allowed_domains")
    @classmethod
    def normalize_domains(cls, value):
        cleaned = []
        for domain in value:
            item = domain.strip().lower().removeprefix("https://").removeprefix("http://").rstrip("/")
            if item and item not in cleaned:
                cleaned.append(item)
        return cleaned


class SearchHit(ApiModel):
    """A candidate source; it is not a confirmed financial fact."""

    source_id: str = Field(min_length=1, max_length=160)
    title: str = Field(min_length=1, max_length=300)
    url: str = Field(min_length=1, max_length=2000)
    domain: str = Field(default="", max_length=200)
    snippet: str = Field(default="", max_length=2000)
    published_at: date | None = None
    relevance: float = Field(default=0, ge=0, le=1)
    evidence_id: str | None = None


class SearchResult(ApiModel):
    """Auditable provider response with explicit no-result states."""

    query: SearchQuery
    provider: str = Field(min_length=1, max_length=100)
    provider_version: str = Field(default="", max_length=80)
    status: Literal["completed", "no_results", "failed", "not_configured"]
    hits: list[SearchHit] = Field(default_factory=list, max_length=50)
    error_code: str | None = Field(default=None, max_length=100)
    error_message: str = Field(default="", max_length=600)
    warnings: list[str] = Field(default_factory=list, max_length=30)
    searched_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class PolicyImpactCard(ApiModel):
    """Policy evidence is explanatory by default and needs finance approval."""

    impact_id: str = Field(min_length=1, max_length=160)
    topic: str = Field(min_length=1, max_length=300)
    applicable_scope: str = Field(default="", max_length=1000)
    effective_date: date | None = None
    source_ids: list[str] = Field(default_factory=list, max_length=30)
    facts: list[str] = Field(default_factory=list, max_length=20)
    channels: list[str] = Field(default_factory=list, max_length=20)
    direction: Literal["positive", "negative", "mixed", "unclear"] = "unclear"
    horizon: str = Field(default="", max_length=120)
    evidence_strength: Literal["high", "medium", "low", "unknown"] = "unknown"
    requires_finance_review: bool = True
    approved_mapping: bool = False
    notes: str = Field(default="", max_length=2000)


class ArtifactManifest(ApiModel):
    """A generated artifact points to one immutable task revision."""

    artifact_id: str = Field(min_length=1, max_length=160)
    format: Literal["json", "html", "xlsx", "pdf"]
    name: str = Field(min_length=1, max_length=300)
    revision: int = Field(ge=1)
    status: Literal["building", "ready", "failed"] = "building"
    path: str | None = None
    sha256: str | None = None
    warnings: list[str] = Field(default_factory=list, max_length=30)
