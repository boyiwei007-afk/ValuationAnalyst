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
