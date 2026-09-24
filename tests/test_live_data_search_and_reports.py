import json
from datetime import date
from decimal import Decimal as D
from io import BytesIO

import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import load_workbook
from pypdf import PdfReader

from valuationagent.api.main import create_app
from valuationagent.application.reporting import ValuationReportExporter
from valuationagent.application.research import ResearchService
from valuationagent.application.runner import ValuationRunner
from valuationagent.finance.team_model import FinanceTeamModel
from valuationagent.market.tushare import (
    TushareApiClient,
    TushareDataProvider,
    map_tushare_industry,
    normalize_a_share_ticker,
)
from valuationagent.schemas.agent import SearchQuery
from valuationagent.schemas.models import (
    CompanyInput,
    FinancialSnapshot,
    ValuationRequest,
)
from valuationagent.search.providers import TavilySearchProvider
from valuationagent.storage.sqlite import SQLiteRunStore


def _request():
    history = []
    for year, revenue in enumerate(range(20, 30), 2016):
        amount = D(revenue) * D("100000000")
        history.append(FinancialSnapshot(
            period_end=date(year, 12, 31), revenue=amount, ebit_margin="0.20",
            tax_rate="0.15", depreciation_amortization=amount * D("0.04"),
            capital_expenditure=amount * D("0.05"), change_operating_nwc=amount * D("0.01"),
            cash_and_non_operating_assets="1000000000", interest_bearing_debt="200000000",
            common_shares="400000000", net_income_parent=amount * D("0.16"),
            ebitda=amount * D("0.24"), statement_items={"market_cap": "50000000000"},
        ))
    return ValuationRequest(
        company=CompanyInput(ticker="603893.SH", name="测试公司", industry="电子"),
        valuation_date=date(2026, 9, 23), forecast_years=10, discount_policy="year_end",
        methods=["dcf"], financials=history[-1], historical_financials=history[:-1],
    )


def test_tavily_provider_uses_cutoff_and_preserves_url_without_leaking_key():
    captured = {}

    def handler(request):
        captured["authorization"] = request.headers["Authorization"]
        captured["payload"] = __import__("json").loads(request.content)
        return httpx.Response(200, json={"results": [{
            "title": "Company annual report", "url": "https://example.com/report",
            "content": "Audited annual figures", "score": 0.9, "published_date": "2025-04-01",
        }]})

    provider = TavilySearchProvider("secret-test-key", transport=httpx.MockTransport(handler))
    result = provider.search(SearchQuery(
        query="测试公司 年报", purpose="financials", information_cutoff=date(2025, 12, 31)
    ))
    assert result.status == "completed"
    assert result.hits[0].url == "https://example.com/report"
    assert captured["payload"]["end_date"] == "2025-12-31"
    assert captured["authorization"] == "Bearer secret-test-key"
    assert "secret-test-key" not in result.model_dump_json()


def test_tavily_auth_failure_is_distinct_and_never_leaks_key():
    def handler(request):
        return httpx.Response(401, json={"detail": "invalid credential"})

    secret = "secret-test-key"
    provider = TavilySearchProvider(secret, transport=httpx.MockTransport(handler))
    result = provider.search(SearchQuery(query="测试公司 年报", purpose="financials"))
    assert result.status == "failed"
    assert result.error_code == "SEARCH_AUTH_FAILED"
    assert "重新配置" in result.error_message
    assert secret not in result.model_dump_json()


def test_tushare_client_and_industry_mapping_are_deterministic():
    def handler(request):
        body = __import__("json").loads(request.content)
        assert body["token"] == "token-value"
        return httpx.Response(200, json={"code": 0, "data": {
            "fields": ["ts_code", "name"], "items": [["600519.SH", "贵州茅台"]]
        }})

    client = TushareApiClient("token-value", transport=httpx.MockTransport(handler))
    assert client.query("stock_basic")[0]["name"] == "贵州茅台"
    assert normalize_a_share_ticker("600519") == "600519.SH"
    label, warning = map_tushare_industry("白酒")
    assert label == "轻工制造"
    assert "显式映射" in warning


def test_peer_selection_excludes_financial_and_uses_cash_in_enterprise_value():
    class FakeClient:
        def query(self, api_name, *, params=None, fields=None):
            if api_name == "stock_basic":
                return [
                    {"ts_code": "A.SZ", "name": "同行A", "industry": "软件服务"},
                    {"ts_code": "B.SZ", "name": "同行B", "industry": "软件服务"},
                    {"ts_code": "C.SZ", "name": "同行C", "industry": "软件服务"},
                    {"ts_code": "F.SH", "name": "银行F", "industry": "银行"},
                ]
            if api_name == "daily_basic":
                return [{"ts_code": code, "trade_date": "20260923", "pe_ttm": 20,
                         "ps_ttm": 4, "total_mv": 1000000}
                        for code in ("A.SZ", "B.SZ", "C.SZ")]
            if api_name == "fina_indicator":
                return [{"ts_code": params["ts_code"], "ann_date": "20260401",
                         "end_date": "20251231", "or_yoy": 10, "ebit_of_gr": 20,
                         "ebitda": 1000000000, "interestdebt": 200000000}]
            if api_name == "balancesheet":
                return [{"ts_code": params["ts_code"], "ann_date": "20260401",
                         "end_date": "20251231", "money_cap": 100000000}]
            raise AssertionError(api_name)

    provider = TushareDataProvider(FakeClient(), peer_limit=3)
    peers, warnings = provider._select_peers(
        CompanyInput(ticker="TARGET.SZ", industry="软件服务"),
        {"trade_date": "20260923", "total_mv": 1000000}, date(2026, 9, 23),
    )
    assert not warnings
    assert [peer.ticker for peer in peers] == ["A.SZ", "B.SZ", "C.SZ"]
    # total_mv uses ten-thousand yuan; EV=(10bn+0.2bn-0.1bn)/1bn=10.1x.
    assert peers[0].ev_ebitda == D("10.1")
    assert peers[0].market_cap == D("10000000000")
    assert peers[0].revenue_growth == D("0.1")
    assert peers[0].ebit_margin == D("0.2")
    assert peers[0].peer_tier == "core"


def test_market_cap_statistics_preserve_average_and_period_endpoints():
    class FakeClient:
        def query(self, api_name, *, params=None, fields=None):
            assert api_name == "daily_basic"
            rows = []
            for prefix, total_mv in (
                ("202510", 100),
                ("202601", 200),
                ("202604", 300),
                ("202607", 400),
            ):
                rows.extend({
                    "ts_code": "TARGET.SZ",
                    "trade_date": f"{prefix}{day:02d}",
                    "total_mv": total_mv,
                } for day in range(1, 21))
            return rows

    statistics = TushareDataProvider(FakeClient())._market_cap_statistics(
        "TARGET.SZ", date(2026, 9, 23)
    )
    assert statistics["quarterly_average_market_cap"] == D("2500000")
    assert statistics["annual_average_market_cap"] == D("2500000")
    assert statistics["market_cap_period_low"] == D("1000000")
    assert statistics["market_cap_period_high"] == D("4000000")


def test_research_ticker_handoff_creates_traceable_run(tmp_path):
    store = SQLiteRunStore(tmp_path / "handoff")
    research = ResearchService(store)
    runner = ValuationRunner(store, FinanceTeamModel())
    session = research.create()
    session.data_source_preference = "online"
    session.draft = session.draft.model_copy(update={
        "company": "贵州茅台", "ticker": "600519.SH", "valuation_date": date(2026, 9, 23),
        "methods": ["dcf", "pe", "ps", "ev_ebitda"],
    })
    store.save_research(session)
    session_provider = TushareDataProvider(TushareApiClient("SESSION_ONLY_TOKEN"))
    research.attach_market(session.session_id, session_provider)
    record = research.submit_valuation(session.session_id, runner)
    saved = store.get_research(session.session_id)
    assert record.request.data_source == "ticker"
    assert record.request.forecast_years == 10
    assert saved.valuation_run_id == record.run_id
    assert runner._run_data[record.run_id] is session_provider
    assert any(event.type == "valuation.submitted" for event in store.list_events(session.session_id))


def test_web_data_services_are_session_scoped_and_secrets_are_not_persisted(tmp_path):
    app = create_app(tmp_path / "data-services")
    session = app.state.research.create()
    tavily_key = "tvly-SESSION_ONLY_TEST_KEY"
    tushare_token = "TUSHARE_SESSION_ONLY_TEST_TOKEN"
    with TestClient(app) as client:
        before = client.get(f"/api/research-sessions/{session.session_id}/data-services")
        connected = client.post(
            f"/api/research-sessions/{session.session_id}/data-services",
            json={"tavily_api_key": tavily_key, "tushare_token": tushare_token},
        )
        after = client.get(f"/api/research-sessions/{session.session_id}/data-services")
    assert before.status_code == 200
    assert before.json()["search"]["available"] is False
    assert before.json()["market"]["available"] is False
    assert connected.status_code == 200
    assert after.json()["search"] == {"available": True, "provider": "tavily"}
    assert after.json()["market"]["available"] is True
    persisted = json.dumps(app.state.research.snapshot(session.session_id), ensure_ascii=False)
    assert tavily_key not in persisted
    assert tushare_token not in persisted


def test_upload_preference_never_silently_falls_back_to_ticker(tmp_path):
    store = SQLiteRunStore(tmp_path / "upload-preference")
    research = ResearchService(store)
    runner = ValuationRunner(store, FinanceTeamModel())
    session = research.create()
    session.data_source_preference = "upload"
    session.draft = session.draft.model_copy(update={
        "company": "贵州茅台",
        "ticker": "600519.SH",
        "valuation_date": date(2026, 9, 23),
        "methods": ["dcf"],
    })
    store.save_research(session)
    with pytest.raises(ValueError, match="已选择自行上传"):
        research.submit_valuation(session.session_id, runner)


def test_formal_xlsx_and_pdf_reports_are_readable(tmp_path):
    store = SQLiteRunStore(tmp_path / "reports")
    record = ValuationRunner(store, FinanceTeamModel()).run(_request())
    assert record.result is not None
    exporter = ValuationReportExporter()
    xlsx = exporter.xlsx(record)
    workbook = load_workbook(BytesIO(xlsx), data_only=False)
    assert {"估值摘要", "关键假设", "预测与FCFF", "敏感性分析", "来源与风险"} <= set(workbook.sheetnames)
    assert str(workbook["预测与FCFF"]["I2"].value).startswith("=")
    pdf = exporter.pdf(record)
    reader = PdfReader(BytesIO(pdf))
    assert len(reader.pages) >= 2


def test_api_exposes_research_handoff_and_formal_exports(tmp_path):
    app = create_app(tmp_path / "api")
    record = app.state.runner.run(_request())
    research_session = app.state.research.create()
    research_session.data_source_preference = "online"
    research_session.draft = research_session.draft.model_copy(update={
        "company": "贵州茅台", "ticker": "600519.SH", "valuation_date": date(2026, 9, 23),
        "methods": ["dcf"],
    })
    app.state.store.save_research(research_session)
    with TestClient(app) as client:
        handoff = client.post(f"/api/research-sessions/{research_session.session_id}/valuation")
        assert handoff.status_code == 202
        xlsx = client.get(f"/api/runs/{record.run_id}/export?format=xlsx")
        pdf = client.get(f"/api/runs/{record.run_id}/export?format=pdf")
    assert xlsx.status_code == 200 and xlsx.content.startswith(b"PK")
    assert pdf.status_code == 200 and pdf.content.startswith(b"%PDF")
