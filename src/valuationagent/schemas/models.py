'''
这份文件用 Pydantic 定义了一整套**企业估值系统的数据契约**——
把"公司、财务、假设、估值方法、运行状态、API 请求响应"等所有数据都规定成严格的类型模型，
让 CLI、API、Agent 三方共享同一套"合法数据长什么样"的规矩，
**它不负责计算，只负责把关和定型**。
'''
from __future__ import annotations

from datetime import date, datetime, timezone
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PlainSerializer,
    SecretStr,
    field_validator,
    model_validator,
)


JsonDecimal = Annotated[
    Decimal,
    PlainSerializer(lambda value: str(value), return_type=str, when_used="json"),
]


class ApiModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        use_enum_values=True,
    )


class DataSourceType(StrEnum):
    STRUCTURED = "structured"
    TICKER = "ticker"
    UPLOAD = "upload"


class AssumptionSourceType(StrEnum):
    AUTOMATIC = "automatic"
    MANUAL = "manual"
    UPLOAD = "upload"


class RunMode(StrEnum):
    DEMO = "demo"
    LIVE = "live"
    SNAPSHOT = "snapshot"


class Language(StrEnum):
    ZH_CN = "zh-CN"
    EN_US = "en-US"


class RunStatus(StrEnum):
    CREATED = "created"
    RUNNING = "running"
    WAITING_REVIEW = "waiting_review"
    COMPLETED = "completed"
    COMPLETED_WITH_WARNINGS = "completed_with_warnings"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ValuationMethod(StrEnum):
    DCF = "dcf"
    PE = "pe"
    EV_EBITDA = "ev_ebitda"


class CompanyInput(ApiModel):
    ticker: str | None = Field(default=None, max_length=24)
    name: str | None = Field(default=None, max_length=200)
    exchange: str | None = Field(default=None, max_length=32)
    industry: str | None = Field(default=None, max_length=120)
    currency: str = Field(default="CNY", min_length=3, max_length=3)

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str | None) -> str | None:
        return value.strip().upper() if value else value


class EvidenceRef(ApiModel):
    evidence_id: str
    source: str
    file_id: str | None = None
    page: int | None = Field(default=None, ge=1)
    sheet: str | None = None
    cell: str | None = None
    published_at: date | None = None
    note: str = ""


class FinancialSnapshot(ApiModel):
    period_end: date
    revenue: JsonDecimal = Field(gt=0)
    ebit_margin: JsonDecimal = Field(ge=Decimal("-1"), le=Decimal("1"))
    tax_rate: JsonDecimal = Field(ge=0, le=Decimal("0.6"))
    depreciation_amortization: JsonDecimal = Field(ge=0)
    capital_expenditure: JsonDecimal = Field(ge=0)
    change_operating_nwc: JsonDecimal
    cash_and_non_operating_assets: JsonDecimal = Field(ge=0)
    interest_bearing_debt: JsonDecimal = Field(ge=0)
    common_shares: JsonDecimal = Field(gt=0)
    net_income_parent: JsonDecimal
    ebitda: JsonDecimal
    source_label: str = "user_structured_input"
    currency: str = "CNY"
    unit: Literal["base_currency"] = "base_currency"
    statement_scope: Literal["consolidated", "parent"] = "consolidated"
    published_at: date | None = None
    evidence: dict[str, list[EvidenceRef]] = Field(default_factory=dict)
    statement_items: dict[str, JsonDecimal] = Field(default_factory=dict)


class PeerCompany(ApiModel):
    ticker: str
    name: str
    pe: JsonDecimal | None = Field(default=None, gt=0)
    ev_ebitda: JsonDecimal | None = Field(default=None, gt=0)
    rationale: str = "user-provided comparable"


class AssumptionInputs(ApiModel):
    revenue_growth: list[JsonDecimal] | None = None
    ebit_margin: list[JsonDecimal] | None = None
    wacc: JsonDecimal | None = Field(default=None, gt=0, lt=Decimal("0.5"))
    terminal_growth: JsonDecimal | None = Field(
        default=None, ge=Decimal("-0.1"), lt=Decimal("0.2")
    )

    @field_validator("revenue_growth", "ebit_margin")
    @classmethod
    def require_nonempty_series(
        cls, value: list[JsonDecimal] | None
    ) -> list[JsonDecimal] | None:
        if value is not None and not value:
            raise ValueError("series must contain at least one value")
        return value


class ValuationRequest(ApiModel):
    company: CompanyInput
    valuation_date: date
    language: Language = Language.ZH_CN
    data_source: DataSourceType = DataSourceType.STRUCTURED
    assumption_source: AssumptionSourceType = AssumptionSourceType.AUTOMATIC
    mode: RunMode = RunMode.SNAPSHOT
    forecast_years: int = Field(default=5, ge=3, le=10)
    methods: list[ValuationMethod] = Field(
        default_factory=lambda: [
            ValuationMethod.DCF,
            ValuationMethod.PE,
            ValuationMethod.EV_EBITDA,
        ]
    )
    financials: FinancialSnapshot | None = None
    historical_financials: list[FinancialSnapshot] = Field(default_factory=list)
    assumptions: AssumptionInputs = Field(default_factory=AssumptionInputs)
    peers: list[PeerCompany] = Field(default_factory=list)
    file_ids: list[str] = Field(default_factory=list)
    assumption_file_ids: list[str] = Field(default_factory=list)
    assumption_evidence: dict[str, list[EvidenceRef]] = Field(default_factory=dict)
    discount_policy: Literal["annual_midyear_remaining"] = "annual_midyear_remaining"
    user_goal: str = "完成可追溯的企业估值并解释关键假设"

    def agent_parameters(self) -> dict[str, Any]:
        """Stable, JSON-safe task choices shared by CLI, API and Agent tools.

        Financial plugins still receive this complete typed request. Credentials
        are configured separately and are never part of these task parameters.
        """
        return self.model_dump(
            mode="json",
            include={
                "company",
                "valuation_date",
                "language",
                "mode",
                "data_source",
                "assumption_source",
                "forecast_years",
                "methods",
                "discount_policy",
                "assumptions",
                "file_ids",
                "assumption_file_ids",
            },
        )

    @field_validator("methods")
    @classmethod
    def methods_are_unique(cls, value: list[ValuationMethod]) -> list[ValuationMethod]:
        if not value:
            raise ValueError("at least one valuation method is required")
        if len(set(value)) != len(value):
            raise ValueError("valuation methods must be unique")
        return value

    @model_validator(mode="after")
    def source_inputs_are_present(self) -> "ValuationRequest":
        if self.data_source == DataSourceType.TICKER and not self.company.ticker:
            raise ValueError("ticker data source requires company.ticker")
        if self.data_source == DataSourceType.UPLOAD and not self.file_ids:
            raise ValueError("upload data source requires file_ids")
        return self


class ModelConnectionInput(ApiModel):
    provider: Literal["openai_compatible", "openai"] = "openai_compatible"
    base_url: str = "https://api.openai.com/v1"
    model: str = Field(min_length=1)
    api_key: SecretStr = Field(min_length=1)
    timeout_seconds: float = Field(default=45.0, gt=1, le=180)
    thinking: Literal["auto", "enabled", "disabled"] = "auto"

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value):
        from urllib.parse import urlsplit

        parsed = urlsplit(value)
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.netloc
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("base_url 必须是无账号、无查询参数的 HTTP(S) 接口地址")
        return value.rstrip("/")


class ModelSessionPublic(ApiModel):
    session_id: str
    provider: str
    base_url: str
    model: str
    created_at: datetime


class RunCreateBody(ApiModel):
    request: ValuationRequest
    model_session_id: str | None = None


class ForecastYear(ApiModel):
    year: int
    discount_period: JsonDecimal = Decimal("1")
    cash_flow_fraction: JsonDecimal = Decimal("1")
    revenue: JsonDecimal
    revenue_growth: JsonDecimal
    ebit_margin: JsonDecimal
    ebit: JsonDecimal
    nopat: JsonDecimal
    depreciation_amortization: JsonDecimal
    capital_expenditure: JsonDecimal
    change_operating_nwc: JsonDecimal
    fcff: JsonDecimal


class AssumptionSet(ApiModel):
    revenue_growth: list[JsonDecimal]
    ebit_margin: list[JsonDecimal]
    wacc: JsonDecimal
    terminal_growth: JsonDecimal
    source: str
    rationale: dict[str, str]


class DcfResult(ApiModel):
    status: Literal["success", "not_applicable"] = "success"
    enterprise_value: JsonDecimal
    equity_value: JsonDecimal
    per_share_value: JsonDecimal
    range_low: JsonDecimal
    range_high: JsonDecimal
    terminal_value_share: JsonDecimal
    bridge: dict[str, JsonDecimal]
    scenario_warnings: list[str] = Field(default_factory=list)


class MultipleResult(ApiModel):
    method: Literal["pe", "ev_ebitda"]
    status: Literal["success", "not_applicable"]
    per_share_value: JsonDecimal | None = None
    range_low: JsonDecimal | None = None
    range_high: JsonDecimal | None = None
    sample_size: int = 0
    reason: str | None = None


class SensitivityCell(ApiModel):
    wacc: JsonDecimal
    terminal_growth: JsonDecimal
    per_share_value: JsonDecimal | None
    valid: bool


class ReconciliationResult(ApiModel):
    dcf_range: tuple[JsonDecimal, JsonDecimal] | None
    relative_range: tuple[JsonDecimal, JsonDecimal] | None
    overlap_range: tuple[JsonDecimal, JsonDecimal] | None
    conclusion: str
    combined_range: None = None


class ValuationOutput(ApiModel):
    run_id: str = ""
    revision: int = 1
    company: CompanyInput | None = None
    valuation_date: date | None = None
    currency: str = "CNY"
    mode: RunMode = RunMode.SNAPSHOT
    language: Language = Language.ZH_CN
    input_hash: str = ""
    effective_input_hash: str = ""
    effective_financials: FinancialSnapshot | None = None
    effective_peers: list[PeerCompany] = Field(default_factory=list)
    assumption_evidence: dict[str, list[EvidenceRef]] = Field(default_factory=dict)
    discount_policy: str = "annual_midyear_remaining"
    model_version: str
    assumptions: AssumptionSet
    forecast: list[ForecastYear]
    dcf: DcfResult | None
    relative: list[MultipleResult]
    sensitivity: list[SensitivityCell]
    reconciliation: ReconciliationResult
    executive_summary: str
    warnings: list[str] = Field(default_factory=list)


class ValidationFinding(ApiModel):
    rule_id: str
    severity: Literal["info", "warning", "blocking"]
    message: str
    actual: JsonDecimal | str | None = None
    expected: JsonDecimal | str | None = None
    recommended_action: str | None = None


class RunEvent(ApiModel):
    run_id: str
    sequence: int
    timestamp: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    type: str
    stage: str | None = None
    status: str | None = None
    summary: str
    tool_call_id: str | None = None
    tool: str | None = None
    duration_ms: int | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class ChatMessage(ApiModel):
    message_id: str
    run_id: str
    role: Literal["user", "assistant", "system", "tool"]
    content: str
    created_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    stage: str | None = None
    related_run_id: str | None = None


class ChatInput(ApiModel):
    content: str = Field(min_length=1, max_length=8000)


class RunRecord(ApiModel):
    run_id: str
    status: RunStatus
    request: ValuationRequest
    input_hash: str
    workflow_version: str = "0.2.0"
    root_run_id: str | None = None
    parent_run_id: str | None = None
    revision: int = 1
    revision_reason: str | None = None
    created_at: datetime
    updated_at: datetime
    result: ValuationOutput | None = None
    error: dict[str, Any] | None = None
    review: dict[str, Any] | None = None


class RunAccepted(ApiModel):
    run_id: str
    status: RunStatus


class Capability(ApiModel):
    capability_id: str
    available: bool
    detail: str


class RevisionInput(ApiModel):
    reason: str = Field(min_length=1, max_length=2000)
    changes: dict[str, Any] = Field(default_factory=dict)


class ResumeInput(ApiModel):
    model_session_id: str | None = None
