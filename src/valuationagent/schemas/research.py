"""Research can start with incomplete material; confirmed valuation inputs stay strict."""

from datetime import date, datetime, timezone
from typing import Any, Literal
from pydantic import Field, model_validator
from valuationagent.schemas.models import ApiModel, Language


class ResearchDraft(ApiModel):
    company: str = Field(default="", max_length=200)
    ticker: str = Field(default="", max_length=20)
    industry: str = Field(default="", max_length=120)
    valuation_date: date | None = None
    objective: str = Field(default="", max_length=2000)
    methods: list[Literal["dcf", "pe", "ps", "ev_ebitda"]] = Field(default_factory=list)


class ResearchChoice(ApiModel):
    id: str = Field(min_length=1, max_length=80)
    label: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=400)


class ResearchMemoryItem(ApiModel):
    """Durable conversational context; never a substitute for financial facts."""

    key: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_.:-]+$")
    kind: Literal["goal", "preference", "constraint", "decision", "definition"]
    content: str = Field(min_length=1, max_length=600)
    source_message_id: str = Field(default="", max_length=120)
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ResearchIssue(ApiModel):
    """Latest recoverable interruption shown to the user and audit trail."""

    issue_id: str
    code: str = Field(min_length=1, max_length=100)
    stage: Literal["model", "tool", "document", "input", "agent", "unknown"]
    message: str = Field(min_length=1, max_length=1200)
    retryable: bool = True
    status: Literal["open", "retrying", "resolved", "deferred"] = "open"
    context: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ResearchQuestion(ApiModel):
    question_id: str
    kind: Literal[
        "task", "data_source", "facts", "clarification",
        "search_unavailable", "search_failed", "recovery",
    ]
    title: str = Field(min_length=1, max_length=600)
    options: list[ResearchChoice] = Field(min_length=1, max_length=4)
    fact_ids: list[str] = Field(default_factory=list)
    superseded_fact_ids: list[str] = Field(default_factory=list)
    proposed_draft: ResearchDraft | None = None


class FactCandidate(ApiModel):
    fact_id: str = ""
    metric: str = Field(min_length=1, max_length=120)
    raw_value: str = Field(min_length=1, max_length=100)
    unit: Literal["元", "万元", "亿元", "股", "万股", "亿股", "%", "ratio", "unknown"] = "unknown"
    normalized_value: str | None = None
    period: str = Field(default="unknown", max_length=60)
    scope: Literal["consolidated", "parent", "unknown"] = "unknown"
    role: Literal["historical", "assumption", "policy"] = "historical"
    block_id: str = Field(min_length=1, max_length=200)
    quote: str = Field(min_length=1, max_length=2400)
    source_type: Literal["document", "user_note"] = "document"
    status: Literal["proposed", "confirmed", "rejected"] = "proposed"
    warnings: list[str] = Field(default_factory=list)


class DocumentSummary(ApiModel):
    file_id: str
    name: str
    role: str
    block_count: int
    sha256: str = Field(default="", max_length=64)
    size_bytes: int = Field(default=0, ge=0)
    warnings: list[str] = Field(default_factory=list)


class ResearchSession(ApiModel):
    session_id: str
    revision: int = 1
    language: Language = Language.ZH_CN
    requires_model: bool = False
    data_source_preference: Literal["", "online", "upload"] = ""
    draft: ResearchDraft = Field(default_factory=ResearchDraft)
    status: Literal[
        "collecting", "waiting_confirmation", "awaiting_financial_model",
        "ready_for_valuation", "submitted"
    ] = "collecting"
    documents: list[DocumentSummary] = Field(default_factory=list)
    facts: list[FactCandidate] = Field(default_factory=list)
    gaps: list[str] = Field(default_factory=list)
    memory: list[ResearchMemoryItem] = Field(default_factory=list, max_length=80)
    question: ResearchQuestion | None = None
    last_issue: ResearchIssue | None = None
    summary: str = ""
    agent_protocol_version: str = "research-agent-v2"
    prompt_version: str = "research-2026-09-24.1"
    model_provider: str = ""
    model_name: str = ""
    valuation_run_id: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))


class ResearchCreate(ApiModel):
    language: Language = Language.ZH_CN
    model_session_id: str | None = None


class ResearchTurn(ApiModel):
    language: Language | None = None
    content: str = Field(default="", max_length=8000)
    file_ids: list[str] = Field(default_factory=list, max_length=8)
    question_id: str | None = None
    option_id: str | None = None

    @model_validator(mode="after")
    def meaningful_turn(self):
        if not self.content.strip() and not self.file_ids and not self.option_id:
            raise ValueError("请输入需求、上传文件或选择一个选项。")
        if self.option_id and not self.question_id:
            raise ValueError("选项必须关联当前确认问题。")
        return self
