import pytest

from valuationagent.llm.intent import interpret_intent
from valuationagent.schemas.research import ResearchTurn
from test_evidence_recovery import configured_service
from test_research_sessions import ScriptedModel


@pytest.mark.parametrize("message", [
    "开始自动化DCF估值。", "开始自动化 DCF 估值，附件优先。",
    "请执行PE估值", "请对伊利股份进行 DCF 与 PE 估值",
    "开始现金流折现估值", "开始 EV/EBITDA 估值", "Run the DCF valuation",
])
def test_named_method_does_not_drop_explicit_valuation_goal(message):
    assert interpret_intent(message).intent == "run_valuation"


@pytest.mark.parametrize("message", ["不要开始DCF估值", "为什么不能执行 PE 估值？", "先不进行现金流折现估值"])
def test_named_method_respects_negation_and_explanations(message):
    assert interpret_intent(message).intent != "run_valuation"


def test_real_upload_command_records_goal_and_report_without_batch_confirmation(tmp_path):
    model = ScriptedModel([("finish_response", {"answer": "所需证据暂不可得", "deliver_outcome": True})])
    service, session, _ = configured_service(tmp_path, model)
    state = service.turn(session.session_id, ResearchTurn(content="开始自动化DCF估值。已有附件优先，缺数交付说明。"))
    assert state["session"]["pending_action"] == "valuation"
    assert state["session"]["question"] is None
    assert state["result_document"]
