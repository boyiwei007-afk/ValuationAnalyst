"""Convert confirmed research facts into the strict valuation contract."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import date
from decimal import Decimal

from valuationagent.schemas.models import (
    AssumptionInputs,
    CompanyInput,
    EvidenceRef,
    FinancialSnapshot,
    ValuationRequest,
)


D = Decimal


METRIC_ALIASES = {
    "revenue": {"revenue", "营业收入", "营业总收入", "主营业务收入"},
    "ebit": {"ebit", "息税前利润"},
    "ebit_margin": {"ebit_margin", "ebit率", "息税前利润率"},
    "tax_rate": {"tax_rate", "所得税率", "实际税率"},
    "depreciation_amortization": {
        "depreciation_amortization", "折旧摊销", "折旧与摊销", "折旧及摊销",
    },
    "capital_expenditure": {"capital_expenditure", "capex", "资本性支出", "资本开支"},
    "change_operating_nwc": {
        "change_operating_nwc", "经营性营运资本变动", "营运资本变动", "Δnwc", "delta_nwc",
    },
    "cash_and_non_operating_assets": {
        "cash_and_non_operating_assets", "现金及非经营性资产", "货币资金",
    },
    "interest_bearing_debt": {"interest_bearing_debt", "有息负债", "带息债务"},
    "common_shares": {"common_shares", "总股本", "普通股股数"},
    "net_income_parent": {"net_income_parent", "归母净利润", "归属于母公司股东的净利润"},
    "ebitda": {"ebitda", "息税折旧摊销前利润"},
}

ASSUMPTION_ALIASES = {
    "wacc": {"wacc", "加权平均资本成本"},
    "terminal_growth": {"terminal_growth", "永续增长率", "终值增长率"},
    "terminal_tax_rate": {"terminal_tax_rate", "永续期税率"},
    "risk_free_rate": {"risk_free_rate", "无风险利率"},
    "equity_risk_premium": {"equity_risk_premium", "股权风险溢价", "市场风险溢价"},
    "beta": {"beta", "贝塔", "贝塔系数"},
    "debt_cost": {"debt_cost", "债务成本"},
    "market_cap": {"market_cap", "市值", "总市值"},
}


def _normalized_metric(value: str, aliases: dict[str, set[str]]) -> str | None:
    key = re.sub(r"[\s/／、·（）()_-]+", "", value).casefold()
    for canonical, names in aliases.items():
        if key in {re.sub(r"[\s/／、·（）()_-]+", "", item).casefold() for item in names}:
            return canonical
    return None


def _period(value: str) -> date | None:
    raw = value.strip()
    match = re.search(r"(?<!\d)(20\d{2})(?:[-年/.](\d{1,2}))?(?:[-月/.](\d{1,2}))?", raw)
    if not match:
        return None
    year = int(match.group(1))
    month = int(match.group(2) or 12)
    day = int(match.group(3) or (31 if month == 12 else 1))
    try:
        return date(year, month, day)
    except ValueError:
        return None


class ResearchValuationAssembler:
    """Strict handoff: only confirmed, scoped and normalized facts are accepted."""

    def _evidence(self, session, fact) -> EvidenceRef:
        document = next(
            (doc for doc in session.documents if fact.block_id.startswith(doc.file_id + ":")),
            None,
        )
        return EvidenceRef(
            evidence_id=fact.fact_id,
            source=document.name if document else "用户在研究会话中确认",
            file_id=document.file_id if document else None,
            note=f"block_id={fact.block_id}; quote={fact.quote[:500]}",
        )

    def _structured_financials(self, session) -> list[FinancialSnapshot]:
        rows: dict[date, dict[str, tuple[Decimal, object]]] = defaultdict(dict)
        for fact in session.facts:
            if fact.status != "confirmed" or fact.role != "historical":
                continue
            metric = _normalized_metric(fact.metric, METRIC_ALIASES)
            period = _period(fact.period)
            if not metric or not period or fact.normalized_value is None:
                continue
            if fact.scope != "consolidated":
                continue
            rows[period][metric] = (D(fact.normalized_value), fact)

        required = {
            "revenue", "tax_rate", "depreciation_amortization", "capital_expenditure",
            "change_operating_nwc", "cash_and_non_operating_assets", "interest_bearing_debt",
            "common_shares", "net_income_parent", "ebitda",
        }
        snapshots = []
        incomplete = []
        for period, values in sorted(rows.items()):
            if "ebit_margin" not in values and "ebit" in values and "revenue" in values:
                values["ebit_margin"] = (values["ebit"][0] / values["revenue"][0], values["ebit"][1])
            missing = sorted((required | {"ebit_margin"}) - set(values))
            if missing:
                incomplete.append(f"{period.isoformat()}: {', '.join(missing)}")
                continue
            evidence = {
                metric: [self._evidence(session, item[1])]
                for metric, item in values.items()
                if metric in FinancialSnapshot.model_fields
            }
            snapshots.append(
                FinancialSnapshot(
                    period_end=period,
                    revenue=values["revenue"][0],
                    ebit_margin=values["ebit_margin"][0],
                    tax_rate=values["tax_rate"][0],
                    depreciation_amortization=values["depreciation_amortization"][0],
                    capital_expenditure=abs(values["capital_expenditure"][0]),
                    change_operating_nwc=values["change_operating_nwc"][0],
                    cash_and_non_operating_assets=values["cash_and_non_operating_assets"][0],
                    interest_bearing_debt=values["interest_bearing_debt"][0],
                    common_shares=values["common_shares"][0],
                    net_income_parent=values["net_income_parent"][0],
                    ebitda=values["ebitda"][0],
                    source_label="研究会话已确认字段",
                    evidence=evidence,
                )
            )
        if rows and not snapshots:
            detail = "；".join(incomplete[:5])
            raise ValueError(f"已确认字段尚不能组成完整年度快照：{detail}")
        return snapshots

    def _assumptions(self, session) -> tuple[AssumptionInputs, dict[str, list[EvidenceRef]]]:
        values, evidence = {}, {}
        for fact in session.facts:
            if fact.status != "confirmed" or fact.role != "assumption":
                continue
            metric = _normalized_metric(fact.metric, ASSUMPTION_ALIASES)
            if metric and fact.normalized_value is not None:
                values[metric] = D(fact.normalized_value)
                evidence[metric] = [self._evidence(session, fact)]
        return AssumptionInputs.model_validate(values), evidence

    def build(self, session) -> ValuationRequest:
        if session.question is not None:
            raise ValueError("仍有待确认问题，请先选择选项或输入修改要求。")
        if not (session.draft.company or session.draft.ticker):
            raise ValueError("请先确认公司名称或A股代码。")
        if session.draft.valuation_date is None:
            raise ValueError("请先确认估值基准日。")
        if not session.data_source_preference:
            raise ValueError("请先确认资料来源：联网获取或自行上传。")

        methods = session.draft.methods or ["dcf", "pe", "ps", "ev_ebitda"]
        use_ticker = bool(
            session.data_source_preference == "online" and session.draft.ticker
        )
        if not use_ticker and any(fact.status == "proposed" for fact in session.facts):
            raise ValueError("仍有待确认候选字段，请先确认、拒绝或更正。")
        # An explicit online+ticker choice uses the point-in-time structured
        # market provider. Search snippets remain research evidence and cannot
        # silently override Tushare statement data, whether proposed or confirmed.
        snapshots = [] if use_ticker else self._structured_financials(session)
        assumptions, assumption_evidence = self._assumptions(session)
        if not use_ticker and not snapshots:
            if session.data_source_preference == "upload":
                raise ValueError("已选择自行上传，但尚未形成可提交的完整财务快照。")
            raise ValueError("没有可提交的完整财务快照；请补齐确认字段，或使用A股代码联网取数。")
        if not use_ticker and not session.draft.industry:
            raise ValueError("结构化资料估值需要确认非金融行业，以匹配金融小组参数库。")

        return ValuationRequest(
            company=CompanyInput(
                ticker=session.draft.ticker or None,
                name=session.draft.company or None,
                industry=session.draft.industry or None,
            ),
            valuation_date=session.draft.valuation_date,
            language=session.language,
            data_source="ticker" if use_ticker else "structured",
            assumption_source="manual" if assumptions.model_dump(exclude_none=True) else "automatic",
            mode="snapshot",
            forecast_years=10,
            methods=methods,
            financials=snapshots[-1] if snapshots else None,
            historical_financials=snapshots[:-1],
            assumptions=assumptions,
            assumption_evidence=assumption_evidence,
            discount_policy="year_end",
            user_goal=session.draft.objective or "完成可追溯的企业估值并解释关键假设",
        )
