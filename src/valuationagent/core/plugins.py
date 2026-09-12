from __future__ import annotations

from typing import Any, Protocol

from valuationagent.schemas.models import (
    AssumptionSet,
    DcfResult,
    FinancialSnapshot,
    ForecastYear,
    MultipleResult,
    PeerCompany,
    ReconciliationResult,
    SensitivityCell,
    ValidationFinding,
    ValuationRequest,
)


class FinancialModelPlugin(Protocol):
    plugin_id: str
    version: str

    def validate(
        self, request: ValuationRequest, financials: FinancialSnapshot
    ) -> list[ValidationFinding]: ...

    def resolve_assumptions(
        self, request: ValuationRequest, financials: FinancialSnapshot
    ) -> AssumptionSet: ...

    def forecast(
        self,
        request: ValuationRequest,
        financials: FinancialSnapshot,
        assumptions: AssumptionSet,
    ) -> list[ForecastYear]: ...

    def dcf(
        self,
        request: ValuationRequest,
        financials: FinancialSnapshot,
        assumptions: AssumptionSet,
        forecast: list[ForecastYear],
    ) -> DcfResult: ...

    def relative(
        self,
        request: ValuationRequest,
        financials: FinancialSnapshot,
        peers: list[PeerCompany],
    ) -> list[MultipleResult]: ...

    def sensitivity(
        self,
        request: ValuationRequest,
        financials: FinancialSnapshot,
        assumptions: AssumptionSet,
    ) -> list[SensitivityCell]: ...

    def reconcile(
        self, dcf: DcfResult | None, relative: list[MultipleResult]
    ) -> ReconciliationResult: ...


class DocumentExtractor(Protocol):
    plugin_id: str
    version: str

    def extract(self, files: list[dict], schema: dict) -> dict[str, Any]: ...


class RevenueForecaster(Protocol):
    plugin_id: str
    version: str

    def forecast_revenue(
        self, history: list[FinancialSnapshot], assumptions: AssumptionSet
    ) -> list[ForecastYear]: ...


class CapitalCostModel(Protocol):
    plugin_id: str
    version: str

    def calculate(self, inputs: dict[str, Any]) -> dict[str, Any]: ...


class PeerSelector(Protocol):
    plugin_id: str
    version: str

    def select(self, request: ValuationRequest) -> list[PeerCompany]: ...


class ReportExporter(Protocol):
    plugin_id: str
    version: str

    def export(self, result: dict[str, Any], destination: str) -> dict[str, Any]: ...
