from datetime import date
from unittest.mock import patch

import httpx
import pytest
from pydantic import ValidationError

from valuationagent.schemas.agent import (
    ArtifactManifest,
    ContextSnapshot,
    EvidenceItem,
    IntentResult,
    PolicyImpactCard,
    SearchQuery,
)
from valuationagent.llm.intent import interpret_intent
from valuationagent.search.providers import MockSearchProvider, UnavailableSearchProvider
from valuationagent.llm.client import OpenAICompatibleClient
from valuationagent.schemas.models import ModelConnectionInput


def test_agent_contracts_keep_intent_and_evidence_structured():
    intent = IntentResult(
        intent="new_valuation",
        confidence=0.92,
        slots={"ticker": "600519", "methods": ["dcf", "pe"]},
        requires_confirmation=True,
    )
    context = ContextSnapshot(
        session_id="research_demo",
        revision=2,
        summary="待确认公司的研究范围",
        task_state={"company": "贵州茅台"},
        confirmed_fact_ids=["fact_1"],
    )
    evidence = EvidenceItem(
        evidence_id="evidence_1",
        source_type="document",
        source="annual-report.pdf",
        quote="营业收入 100 亿元",
        locator={"page": 42},
        status="proposed",
    )
    assert intent.slots["ticker"] == "600519"
    assert context.revision == 2
    assert evidence.locator["page"] == 42


def test_search_query_normalizes_domains_and_mock_never_calls_network():
    query = SearchQuery(
        query="贵州茅台 可比公司",
        ticker=" 600519 ",
        purpose="comparables",
        allowed_domains=["https://example.com/", "example.com", "CNINFO.CN"],
        candidate_limit=2,
    )
    provider = MockSearchProvider(
        [
            {
                "source_id": "peer_1",
                "title": "贵州茅台行业资料",
                "url": "https://example.com/a",
                "domain": "example.com",
                "snippet": "可比公司与白酒行业",
            },
            {
                "source_id": "other_1",
                "title": "宏观资料",
                "url": "https://example.com/b",
                "domain": "example.com",
                "snippet": "宏观经济",
            },
        ]
    )
    result = provider.search(query)
    assert query.ticker == "600519"
    assert query.allowed_domains == ["example.com", "cninfo.cn"]
    assert result.status == "completed"
    assert result.hits[0].source_id == "peer_1"
    assert result.provider == "mock-search"


def test_search_unavailable_is_explicit_and_policy_needs_finance_review():
    query = SearchQuery(query="600519 年报", information_cutoff=date(2026, 9, 19))
    result = UnavailableSearchProvider().search(query)
    card = PolicyImpactCard(impact_id="policy_1", topic="监管政策", source_ids=["evidence_1"])
    artifact = ArtifactManifest(artifact_id="artifact_1", format="html", name="research.html", revision=1)
    assert result.status == "not_configured"
    assert result.hits == []
    assert card.requires_finance_review is True
    assert card.approved_mapping is False
    assert artifact.status == "building"


def test_search_query_rejects_empty_or_oversized_budget():
    with pytest.raises(ValidationError):
        SearchQuery(query="", candidate_limit=8)
    with pytest.raises(ValidationError):
        SearchQuery(query="ok", candidate_limit=51)


def test_openai_compatible_client_normalizes_object_tool_arguments():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "tool_calls": [
                                {
                                    "id": "call_1",
                                    "type": "function",
                                    "function": {
                                        "name": "connection_check",
                                        "arguments": {},
                                    },
                                }
                            ],
                        }
                    }
                ]
            },
        )

    original = httpx.Client
    config = ModelConnectionInput(model="mock", api_key="not-real")
    with patch(
        "valuationagent.llm.client.httpx.Client",
        side_effect=lambda **kwargs: original(
            transport=httpx.MockTransport(handler), **kwargs
        ),
    ):
        assert OpenAICompatibleClient(config).test_connection().startswith("OK")


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("我想研究 600519，先整理年报", "new_valuation"),
        ("请立即开始正式估值", "run_valuation"),
        ("/valuation", "run_valuation"),
        ("Run the formal valuation", "run_valuation"),
        ("请解释 WACC 和敏感性分析", "ask_explanation"),
        ("把 WACC 改为 8%", "revise_assumption"),
        ("分析这份监管政策", "policy_analysis"),
        ("/export html", "request_export"),
    ],
)
def test_intent_baseline_is_explicit_and_context_aware(message, expected):
    result = interpret_intent(message, {"company": "贵州茅台", "revision": 3})
    assert result.intent == expected
    assert result.slots["company"] == "贵州茅台"


def test_intent_extracts_company_name_immediately_after_a_share_code():
    result = interpret_intent("我想研究 600276 恒瑞医药的历史财务数据并估值（申万行业：医药生物—化学制药）")
    assert result.slots["ticker"] == "600276"
    assert result.slots["company"] == "恒瑞医药"
    assert result.slots["industry"] == "医药生物—化学制药"

    compact = interpret_intent("研究600276恒瑞医药（行业：医药生物）")
    assert compact.slots["company"] == "恒瑞医药"
    assert "company" not in interpret_intent("研究 600276 历史数据").slots

    parenthesized = interpret_intent("请对贵州茅台（600519.SH）开展估值")
    assert parenthesized.slots["ticker"] == "600519.SH"
    assert parenthesized.slots["company"] == "贵州茅台"


def test_intent_extracts_explicit_valuation_date_and_methods():
    result = interpret_intent(
        "研究 600276 恒瑞医药，估值日：2025-12-31，方法：DCF、PE"
    )

    assert result.slots["valuation_date"] == "2025-12-31"
    assert result.slots["methods"] == ["dcf", "pe"]

    trailing = interpret_intent("对贵州茅台（600519.SH）只使用 DCF 和 PE 方法")
    assert trailing.slots["methods"] == ["dcf", "pe"]


@pytest.mark.parametrize(
    "message",
    [
        "为什么不能执行正式估值？",
        "暂不开始估值，先核对数据",
        "系统提示中提到了 /valuation 命令，这是怎么回事？",
    ],
)
def test_valuation_mentions_are_not_mistaken_for_submission(message):
    assert interpret_intent(message).intent != "run_valuation"


def test_tushare_opt_out_overrides_previous_valuation_intent():
    result = interpret_intent("不使用 Tushare")
    assert result.intent == "provide_material"
    assert result.slots["data_source"] == "not_tushare"
