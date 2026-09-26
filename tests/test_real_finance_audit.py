"""Regression cases from real-statement audit; no live data or model required.

The four disclosed D&A rows in the first test are transcribed from page 116
of Kweichow Moutai's 2024 annual report, not a live investment valuation:
https://static.cninfo.com.cn/finalpage/2025-04-03/1222993920.PDF
All other financial amounts below are explicitly synthetic.
"""
from datetime import date
from decimal import Decimal as D

import pytest

from valuationagent.application.research_valuation import (
    ResearchValuationAssembler,
    normalize_financial_metric,
)
from valuationagent.schemas.research import FactCandidate
from valuationagent.schemas.research import ResearchDraft, ResearchSession
from valuationagent.finance.integrity import (
    EQUITY_BRIDGE_REVIEW_LABELS,
    validate_equity_bridge_inputs,
    verify_calculations,
)
from valuationagent.finance.team_model import FinanceTeamModel
from valuationagent.schemas.models import CompanyInput, FinancialSnapshot, ValuationRequest


def fact(metric, value, *, quote=None):
    row = quote or f"{metric} {value}"
    return FactCandidate(
        fact_id="test_" + metric, metric=metric, raw_value=value,
        normalized_value=value, unit="元", period="2024年度",
        scope="consolidated", block_id="test:statement", quote=row,
        status="confirmed", verification={"source_row": row},
    )


def component_values(rows):
    return {
        normalize_financial_metric(metric): (D(value), [fact(metric, value)])
        for metric, value in rows.items()
    }


def synthetic_components():
    return component_values({
        "固定资产折旧": "100", "无形资产摊销": "20",
        "长期待摊费用摊销": "5", "使用权资产摊销": "10",
        "租赁负债": "50",
    })


def test_real_report_separately_disclosed_lease_charge_is_included_once():
    rows = {
        "固定资产折旧、油气资产折耗、生产性生物资产折旧": "1721165327.14",
        "使用权资产摊销": "94492678.29",
        "无形资产摊销": "249170059.35",
        "长期待摊费用摊销": "20191550.34",
    }
    values, methods = component_values(rows), {}
    assembler = ResearchValuationAssembler()
    assembler._derive_period(values, methods)
    assembler._reconcile_period(date(2024, 12, 31), values, methods)
    assert values["depreciation_amortization"][0] == D("2085019615.12")
    assert "depreciation_right_of_use" in methods["depreciation_amortization"]
    assert len(values["depreciation_amortization"][1]) == 4


def test_synthetic_missing_lease_charge_is_not_assumed_zero():
    values = synthetic_components()
    values.pop("depreciation_right_of_use")
    with pytest.raises(ValueError, match="不能把缺失项当作零"):
        ResearchValuationAssembler()._derive_period(values, {})
    assert "depreciation_amortization" not in values


def test_synthetic_lease_gap_does_not_block_methods_without_da():
    values = synthetic_components()
    values.pop("depreciation_right_of_use")
    assembler = ResearchValuationAssembler()
    assembler._derive_period(values, {}, require_da=False)
    assembler._reconcile_period(date(2024, 12, 31), values, {}, require_da=False)
    assert "depreciation_amortization" not in values


def test_synthetic_complete_direct_total_does_not_need_partial_component_sum():
    values = synthetic_components()
    values.pop("depreciation_right_of_use")
    values.update(component_values({"折旧摊销": "135"}))
    methods = {"depreciation_amortization": "direct_confirmed_fact"}
    assembler = ResearchValuationAssembler()
    assembler._derive_period(values, methods)
    assembler._reconcile_period(date(2024, 12, 31), values, methods)
    assert values["depreciation_amortization"][0] == D("135")
    assert methods["depreciation_amortization"] == "direct_confirmed_fact"


def test_synthetic_direct_total_is_not_incremented_by_lease_charge_again():
    values = synthetic_components()
    values.update(component_values({"折旧摊销": "135"}))
    methods = {"depreciation_amortization": "direct_confirmed_fact"}
    assembler = ResearchValuationAssembler()
    assembler._derive_period(values, methods)
    assembler._reconcile_period(date(2024, 12, 31), values, methods)
    assert values["depreciation_amortization"][0] == D("135")
    assert "depreciation_right_of_use" in methods["reconciliation.depreciation_amortization"]


def test_synthetic_direct_total_must_reconcile_with_complete_independent_rows():
    values = synthetic_components()
    values.update(component_values({"折旧摊销": "125"}))
    with pytest.raises(ValueError, match="勾稽"):
        ResearchValuationAssembler()._reconcile_period(
            date(2024, 12, 31), values,
            {"depreciation_amortization": "direct_confirmed_fact"},
        )


@pytest.mark.parametrize("row", [
    "固定资产折旧（含使用权资产折旧）110",
    "depreciation_fixed_assets including right_of_use depreciation 110",
    "折旧总额110",
])
def test_synthetic_inclusive_or_ambiguous_fixed_charge_cannot_double_count(row):
    values = synthetic_components()
    values["depreciation_fixed_assets"] = (
        D("110"), [fact("depreciation_fixed_assets", "110", quote=row)],
    )
    with pytest.raises(ValueError, match="不能重复相加"):
        ResearchValuationAssembler()._derive_period(values, {})


def test_synthetic_same_fact_cannot_supply_two_independent_components():
    values = synthetic_components()
    values["depreciation_right_of_use"] = values["depreciation_fixed_assets"]
    with pytest.raises(ValueError, match="不能重复相加"):
        ResearchValuationAssembler()._derive_period(values, {})


def test_synthetic_explicit_zero_lease_charge_is_valid_not_missing():
    values = synthetic_components()
    values["depreciation_right_of_use"] = (
        D(0), [fact("使用权资产摊销", "0")],
    )
    ResearchValuationAssembler()._derive_period(values, {})
    assert values["depreciation_amortization"][0] == D("125")


@pytest.mark.parametrize("metric,expected", [
    ("普通股股数（总股本）", "common_shares"),
    ("总股本(普通股股数)", "common_shares"),
    ("common_shares（总股本）", "common_shares"),
    ("普通股股份总数", "common_shares"),
    ("普通股股份总额", "common_shares"),
    ("股份总数", "common_shares"),
    ("股本", None),
    ("营业收入（营业总收入）", None),
    ("营业总收入（营业收入）", None),
    ("普通股股数（实收资本）", None),
    ("固定资产折旧（含使用权资产折旧）", None),
])
def test_parenthesized_synonyms_must_have_the_same_exact_basis(metric, expected):
    assert normalize_financial_metric(metric) == expected


def structured_request(method, metric, amount="10"):
    snapshot = FinancialSnapshot(
        period_end=date(2024, 12, 31), revenue="1000", ebit_margin="0.2",
        tax_rate="0.25", depreciation_amortization="10", capital_expenditure="15",
        change_operating_nwc="5", cash_and_non_operating_assets="100",
        interest_bearing_debt="40", common_shares="100", ebitda="210",
        net_income_parent="140", statement_items={metric: D(amount)},
    )
    return ValuationRequest(
        company=CompanyInput(name="合成桥接门禁样本", industry="电子"),
        valuation_date=date(2025, 6, 30), methods=[method], financials=snapshot,
    )


@pytest.mark.parametrize("metric", list(EQUITY_BRIDGE_REVIEW_LABELS))
@pytest.mark.parametrize("method", ["dcf", "ev_ebitda"])
def test_explicit_complex_bridge_inputs_block_only_enterprise_value_methods(metric, method):
    request = structured_request(method, metric)
    findings = FinanceTeamModel().validate(request, request.financials)
    gate = next(f for f in findings if f.rule_id == "EQUITY_BRIDGE_COMPLEX_SCOPE")
    assert gate.severity == "blocking"
    assert EQUITY_BRIDGE_REVIEW_LABELS[metric] in gate.message
    assert "不能把调整默认为零" in gate.message
    assert request.financials.statement_items[metric] == D("10")
    with pytest.raises(ValueError, match="桥接需专项复核"):
        verify_calculations(request, request.financials, None, [], None, [])


@pytest.mark.parametrize("method", ["pe", "ps"])
def test_bridge_review_does_not_block_independent_equity_multiples(method):
    request = structured_request(method, "minority_interest")
    assert not validate_equity_bridge_inputs(request, request.financials)
    assert not any(f.rule_id == "EQUITY_BRIDGE_COMPLEX_SCOPE"
                   for f in FinanceTeamModel().validate(request, request.financials))


def test_explicit_zero_risk_fact_does_not_trigger_positive_exposure_gate():
    request = structured_request("dcf", "minority_interest", "0")
    assert not validate_equity_bridge_inputs(request, request.financials)


def test_research_bridge_review_retains_raw_evidence_and_allows_pe_subset():
    session = ResearchSession(session_id="test_finance_scope", draft=ResearchDraft(methods=["dcf"]))
    session.facts = [fact("少数股东权益", "10"), fact("归母净利润", "140"),
                     fact("common_shares", "100")]
    session.facts[-1].unit = "股"
    assembler = ResearchValuationAssembler()
    assert "少数股东权益=10" in assembler.structured_readiness_error(session)
    assert session.facts[0].metric == "少数股东权益"
    session.draft.methods = ["pe"]
    snapshot = assembler._structured_financials(session)[0]
    assert snapshot.net_income_parent == D("140")
    assert snapshot.statement_items["minority_interest"] == D("10")


def test_only_share_count_accepts_issuer_scope_alongside_consolidated_income():
    session = ResearchSession(session_id="test_share_scope", draft=ResearchDraft(methods=["pe"]))
    shares = fact("普通股股数（总股本）", "100")
    shares.scope, shares.unit = "issuer", "股"
    income = fact("归母净利润", "140")
    issuer_cash = fact("货币资金", "999")
    issuer_cash.scope = "issuer"
    session.facts = [income, shares, issuer_cash]
    snapshot = ResearchValuationAssembler()._structured_financials(session)[0]
    assert snapshot.common_shares == D("100")
    assert snapshot.net_income_parent == D("140")
    assert snapshot.cash_and_non_operating_assets is None
    assert "cash_and_non_operating_assets" not in snapshot.statement_items
    assert shares.metric == "普通股股数（总股本）"


def test_generic_total_shares_requires_issuer_scope_not_a_statement_amount():
    session = ResearchSession(session_id="test_total_shares", draft=ResearchDraft(methods=["pe"]))
    shares = fact("股份总数", "100")
    shares.unit = "股"
    session.facts = [fact("归母净利润", "140"), shares]
    assembler = ResearchValuationAssembler()
    assert "普通股股数" in assembler.structured_readiness_error(session)
    shares.scope = "issuer"
    assert assembler._structured_financials(session)[0].common_shares == D("100")
