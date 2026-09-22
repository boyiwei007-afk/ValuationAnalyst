import json
from io import BytesIO
from unittest.mock import patch
import httpx
import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from typer.testing import CliRunner
from valuationagent.api.main import create_app
from valuationagent.application.research import ResearchService
from valuationagent.application.research_export import build_research_export
from valuationagent.cli.main import app, interactive
from valuationagent.core.documents import parse_document
from valuationagent.core.tools import NoArguments, ToolSpec
from valuationagent.llm.client import LlmError, OpenAICompatibleClient
from valuationagent.schemas.models import ModelConnectionInput
from valuationagent.schemas.research import ResearchTurn
from valuationagent.storage.sqlite import SQLiteRunStore


class ScriptedModel:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(json.loads(json.dumps(messages)))
        name, args = next(self.actions)
        return {"tool_calls": [{"id": f"call_{len(self.calls)}", "type": "function",
            "function": {"name": name, "arguments": json.dumps(args, ensure_ascii=False)}}],
            "reasoning_content": "PRIVATE_TEST_REASONING"}


@pytest.fixture
def service(tmp_path):
    return ResearchService(SQLiteRunStore(tmp_path))


def candidate(file_id, **changes):
    return {"metric": "revenue", "raw_value": "12000", "unit": "万元", "period": "2025",
        "scope": "consolidated", "role": "historical", "block_id": file_id + ":1",
        "quote": "2025 年合并报表，单位万元。营业收入：12000。", **changes}


def upload(store):
    return store.save_upload("年报摘录.txt", "historical_financials", "text/plain",
        "2025 年合并报表，单位万元。营业收入：12000。".encode())["file_id"]


def test_research_starts_incomplete_and_requires_explicit_selection(service):
    session = service.create()
    result = service.turn(session.session_id, ResearchTurn(content="/company 测试公司"))
    question = result["session"]["question"]
    assert result["session"]["draft"]["company"] == ""
    result = service.turn(session.session_id, ResearchTurn(content="先不要确认，我想再看看"))
    assert result["session"]["draft"]["company"] == ""
    result = service.turn(session.session_id, ResearchTurn(question_id=question["question_id"], option_id="accept"))
    assert result["session"]["draft"]["company"] == "测试公司"
    assert service.store.list_runs() == []
    before = len(result["messages"])
    with pytest.raises(ValueError, match="失效"):
        service.turn(session.session_id, ResearchTurn(question_id=question["question_id"], option_id="accept"))
    assert len(service.store.list_messages(session.session_id)) == before


def test_text_corrections_cannot_accept_old_values(service):
    session = service.create()
    state = service.turn(session.session_id, ResearchTurn(content="/company 旧名称"))
    with pytest.raises(ValueError, match="不能同时确认"):
        service.turn(session.session_id, ResearchTurn(question_id=state["session"]["question"]["question_id"], option_id="accept", content="其实应改为新名称"))
    assert service.store.get_research(session.session_id).draft.company == ""


def test_free_text_supersedes_the_exact_question_without_accepting_it(service):
    session = service.create()
    first = service.turn(session.session_id, ResearchTurn(content="/company 旧名称"))
    old_question = first["session"]["question"]
    state = service.turn(session.session_id, ResearchTurn(
        content="/company 新名称", question_id=old_question["question_id"]
    ))
    assert state["session"]["draft"]["company"] == ""
    assert state["session"]["question"]["question_id"] != old_question["question_id"]
    assert state["session"]["question"]["proposed_draft"]["company"] == "新名称"
    with pytest.raises(ValueError, match="失效"):
        service.turn(session.session_id, ResearchTurn(
            question_id=old_question["question_id"], option_id="accept"
        ))


def test_file_llm_tool_evidence_and_confirmation_survive_restart(service):
    fid = upload(service.store)
    model = ScriptedModel([("read_document", {"file_id": fid}), ("propose_facts", {"candidates": [candidate(fid)]})])
    session = service.create(llm=model)
    state = service.turn(session.session_id, ResearchTurn(content="提取营业收入", file_ids=[fid]))
    fact = state["session"]["facts"][0]
    assert fact["status"] == "proposed" and fact["normalized_value"] == "120000000"
    assert state["session"]["documents"][0]["block_count"] == 1
    assert len(state["session"]["documents"][0]["sha256"]) == 64
    assert "PRIVATE_TEST_REASONING" not in json.dumps(state)
    # Reasoning survives only within the ephemeral protocol loop.
    assert any(m.get("reasoning_content") == "PRIVATE_TEST_REASONING" for m in model.calls[-1])
    qid = state["session"]["question"]["question_id"]
    fresh = ResearchService(SQLiteRunStore(service.store.data_dir))
    state = fresh.turn(session.session_id, ResearchTurn(question_id=qid, option_id="accept"))
    assert state["session"]["facts"][0]["status"] == "confirmed"
    assert fresh.store.research_blocks(session.session_id, fid)[0]["text"] == fact["quote"]
    assert "正式金融模型" in build_research_export(fresh, session.session_id, "html")[0]


def test_long_conversation_keeps_explicit_durable_memory_after_restart(service):
    first = ScriptedModel([
        ("update_memory", {
            "updates": [{
                "key": "scope.statement_basis",
                "kind": "constraint",
                "content": "本任务后续分析统一采用合并报表口径；发现母公司口径时先询问用户。",
            }],
        }),
        ("finish_response", {
            "answer": "已记住本任务统一采用合并口径。",
        }),
    ])
    session = service.create(llm=first)
    service.turn(session.session_id, ResearchTurn(content="后续统一采用合并口径，遇到母公司口径先问我。"))
    for index in range(30):
        service.store.add_message(session.session_id, "user", f"临时讨论 {index}", "research")
        service.store.add_message(session.session_id, "assistant", f"临时回复 {index}", "research")

    fresh = ResearchService(SQLiteRunStore(service.store.data_dir))
    second = ScriptedModel([("finish_response", {"answer": "我会继续遵循已保存口径。"})])
    fresh.attach(session.session_id, second)
    state = fresh.turn(session.session_id, ResearchTurn(content="继续之前的任务"))

    system_prompt = second.calls[0][0]["content"]
    assert "scope.statement_basis" in system_prompt
    assert "统一采用合并报表口径" in system_prompt
    assert state["session"]["memory"][0]["source_message_id"].startswith("msg_")


def test_credentials_are_redacted_from_messages_memory_and_tool_events(service):
    secret = "sk-testsecret1234567890"
    model = ScriptedModel([("finish_response", {
        "answer": f"不会保存 {secret}",
        "memory_updates": [{
            "key": "preference.secret",
            "kind": "preference",
            "content": secret,
        }],
    })])
    session = service.create(llm=model)
    state = service.turn(session.session_id, ResearchTurn(content=f"请记住 {secret}"))
    serialized = json.dumps(state, ensure_ascii=False)
    assert secret not in serialized
    assert "REDACTED_CREDENTIAL" in serialized
    assert state["session"]["memory"] == []
    assert any(event["type"] == "security.credential_redacted" for event in state["events"])


def test_model_failure_becomes_recoverable_user_choice(service):
    class FailingModel:
        def chat(self, messages, **kwargs):
            raise LlmError("LLM_HTTP_503: 供应商暂时不可用，请稍后恢复。")

    session = service.create(llm=FailingModel())
    state = service.turn(session.session_id, ResearchTurn(content="继续整理资料"))
    question = state["session"]["question"]
    assert question["kind"] == "recovery"
    assert {item["id"] for item in question["options"]} >= {"retry", "revise", "defer"}
    assert state["session"]["last_issue"]["code"] == "LLM_HTTP_503"
    assert "没有继续猜测" in state["messages"][-1]["content"]
    assert any(event["type"] == "agent.recovery_required" for event in state["events"])

    service.attach(session.session_id, ScriptedModel([("finish_response", {"answer": "重试成功，已从原上下文继续。"})]))
    state = service.turn(session.session_id, ResearchTurn(
        question_id=question["question_id"], option_id="retry"
    ))
    assert state["session"]["question"] is None
    assert state["session"]["last_issue"]["status"] == "resolved"
    assert "重试成功" in state["messages"][-1]["content"]


def test_registered_tool_provider_joins_same_audit_loop(tmp_path):
    class FinanceProbeProvider:
        provider_id = "finance-probe"
        version = "test-1"

        def tool_specs(self, session):
            return [ToolSpec(
                "inspect_finance_contract",
                "读取未来金融插件的能力契约，不执行估值。",
                NoArguments,
                lambda _: {"available": False, "reason": "正式模型待接入"},
            )]

    model = ScriptedModel([
        ("inspect_finance_contract", {}),
        ("finish_response", {"answer": "已检查金融插件契约；正式模型仍待接入。"}),
    ])
    service = ResearchService(
        SQLiteRunStore(tmp_path / "provider-runtime"),
        tool_providers=[FinanceProbeProvider()],
    )
    session = service.create(llm=model)
    state = service.turn(session.session_id, ResearchTurn(content="检查金融工具是否可用"))
    completed = [event for event in state["events"] if event["type"] == "tool.completed"]
    assert any(event["tool"] == "inspect_finance_contract" for event in completed)
    agent_start = next(event for event in state["events"] if event["type"] == "agent.started")
    assert agent_start["payload"]["tool_extensions"][0]["provider_id"] == "finance-probe"


def test_invented_number_is_rejected_and_tool_feedback_reaches_model(service):
    fid = upload(service.store)
    model = ScriptedModel([
        ("read_document", {"file_id": fid}),
        ("propose_facts", {"candidates": [candidate(fid, raw_value="999999")]}),
        ("finish_response", {"answer": "原文不支持这个数值，需要重新核对。"}),
    ])
    session = service.create(llm=model)
    result = service.turn(session.session_id, ResearchTurn(content="提取", file_ids=[fid]))
    assert result["session"]["facts"] == []
    assert any(e["type"] == "tool.failed" for e in result["events"])
    assert "原文不支持" in result["messages"][-1]["content"]


def test_repeated_identical_tool_failure_stops_and_asks_user(service):
    fid = upload(service.store)
    invalid = {"candidates": [candidate(fid, raw_value="999999")]}
    model = ScriptedModel([
        ("propose_facts", invalid),
        ("propose_facts", invalid),
    ])
    session = service.create(llm=model)
    state = service.turn(session.session_id, ResearchTurn(content="提取", file_ids=[fid]))
    assert state["session"]["facts"] == []
    assert state["session"]["last_issue"]["code"] == "AGENT_NO_PROGRESS"
    assert state["session"]["question"]["kind"] == "recovery"
    assert any(event["type"] == "tool.failed" for event in state["events"])


def test_uncertain_fields_are_not_blanket_confirmed(service):
    fid = upload(service.store)
    model = ScriptedModel([("propose_facts", {"candidates": [candidate(fid, unit="unknown")]})])
    session = service.create(llm=model)
    result = service.turn(session.session_id, ResearchTurn(content="提取", file_ids=[fid]))
    qid = result["session"]["question"]["question_id"]
    result = service.turn(session.session_id, ResearchTurn(question_id=qid, option_id="accept"))
    assert result["session"]["facts"][0]["status"] == "proposed"


def test_pasted_correction_requires_confirmation_and_preserves_old_record(service):
    fid = upload(service.store)
    model = ScriptedModel([("propose_facts", {"candidates": [candidate(fid)]})])
    session = service.create(llm=model)
    state = service.turn(session.session_id, ResearchTurn(content="提取", file_ids=[fid]))
    state = service.turn(session.session_id, ResearchTurn(question_id=state["session"]["question"]["question_id"], option_id="accept"))
    old_id = state["session"]["facts"][0]["fact_id"]
    note = "我更正数据：2025 年合并报表，单位万元。营业收入：13000。"
    service.store.add_message(session.session_id, "user", note, "research")
    # The same message IDs are exposed to the model through inspect_context.
    block_id = next(b["block_id"] for b in service._blocks(session).values() if b["text"] == note)
    correction = candidate(fid, raw_value="13000", block_id=block_id, quote=note)
    service.attach(session.session_id, ScriptedModel([
        ("inspect_context", {}),
        ("propose_facts", {"candidates": [correction], "replaces": [old_id]}),
    ]))
    state = service.turn(session.session_id, ResearchTurn(content="请按上述补充更正"))
    assert [f["status"] for f in state["session"]["facts"]] == ["confirmed", "proposed"]
    assert state["session"]["facts"][1]["source_type"] == "user_note"
    state = service.turn(session.session_id, ResearchTurn(question_id=state["session"]["question"]["question_id"], option_id="accept"))
    assert [f["status"] for f in state["session"]["facts"]] == ["rejected", "confirmed"]
    assert state["session"]["facts"][0]["raw_value"] == "12000"


def test_partial_scope_update_preserves_other_fields_and_refreshes_gaps(service):
    session = service.create()
    state = service.turn(session.session_id, ResearchTurn(content="/company 测试公司"))
    service.turn(session.session_id, ResearchTurn(question_id=state["session"]["question"]["question_id"], option_id="accept"))
    service.attach(session.session_id, ScriptedModel([("propose_task", {"draft": {"valuation_date": "2026-09-14"}})]))
    state = service.turn(session.session_id, ResearchTurn(content="估值日用今天"))
    assert state["session"]["question"]["proposed_draft"]["company"] == "测试公司"
    service.turn(session.session_id, ResearchTurn(question_id=state["session"]["question"]["question_id"], option_id="accept"))
    state = service.turn(session.session_id, ResearchTurn(content="/prepare"))
    assert "估值基准日尚未确认" not in state["session"]["gaps"]
    assert "估值方法尚未确认" in state["session"]["gaps"]
    service.attach(session.session_id, ScriptedModel([
        ("update_data_gaps", {"missing": ["可比公司不足"], "reason": "重新核对当前资料"}),
        ("finish_response", {"answer": "已更新资料缺口。"}),
    ]))
    state = service.turn(session.session_id, ResearchTurn(content="重新检查缺口", language="en-US"))
    assert state["session"]["gaps"] == ["可比公司不足"]
    assert state["session"]["language"] == "en-US"


def test_search_gap_does_not_fabricate_peers(service):
    model = ScriptedModel([("search_sources", {"query": "可比公司", "reason": "可比公司不足"})])
    session = service.create(llm=model)
    result = service.turn(session.session_id, ResearchTurn(content="帮我补充同业"))
    assert result["session"]["question"]["kind"] == "search_unavailable"
    assert result["session"]["facts"] == []
    assert "没有发出网络请求" in result["messages"][-1]["content"]


def test_llm_receives_source_ambiguity_contract_and_free_text_guidance(service):
    model = ScriptedModel([("finish_response", {
        "answer": "表头中的 2024 与相邻页的 2023 无法可靠对应，暂未提取该列。",
        "question": "这列应按哪个报告期处理？",
        "options": ["按表头原样保留为 2024", "暂不采用这列，等待补充资料"],
    })])
    session = service.create(llm=model)
    result = service.turn(session.session_id, ResearchTurn(content="请提取这份表格"))

    system_prompt = model.calls[0][0]["content"]
    assert "不得静默修正或合理化" in system_prompt
    assert "不得把数值平移到相邻年份" in system_prompt
    assert "实际发现了什么" in system_prompt
    assert "用户可以直接输入补充或修改要求" in system_prompt
    assert result["session"]["question"]["kind"] == "clarification"
    assert [option["label"] for option in result["session"]["question"]["options"]] == [
        "按表头原样保留为 2024",
        "暂不采用这列，等待补充资料",
    ]
    assert "直接输入你的判断" in result["messages"][-1]["content"]


def test_excel_locations_and_formulas_are_not_executed(service):
    book = Workbook()
    book.active.title = "历史财务"
    book.active.append(["营业收入", 12000, "万元"])
    book.active.append(["外部公式", '=WEBSERVICE("https://invalid.example")'])
    data = BytesIO()
    book.save(data)
    meta = service.store.save_upload("财务.xlsx", "historical_financials", None, data.getvalue())
    blocks, _ = parse_document(service.store.get_file(meta["file_id"]))
    assert blocks[0]["location"] == {"sheet": "历史财务", "row": 1}
    assert "B1: 12000" in blocks[0]["text"]
    assert "公式，未求值" in blocks[1]["text"]
    assert "https://" not in blocks[1]["text"]


def test_api_research_sources_are_scoped_and_exports_are_safe(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        first = client.post("/api/research-sessions", json={"language": "en-US"}).json()["session"]["session_id"]
        second = client.post("/api/research-sessions", json={}).json()["session"]["session_id"]
        fid = client.post("/api/files", data={"role": "evidence"}, files={"file": ("policy.txt", b'<script>alert(1)</script>', "text/plain")}).json()["file_id"]
        result = client.post(f"/api/research-sessions/{first}/messages", json={"file_ids": [fid]}).json()
        assert result["session"]["documents"][0]["name"] == "policy.txt"
        assert client.get(f"/api/research-sessions/{first}/sources/{fid}").status_code == 200
        assert client.get(f"/api/research-sessions/{second}/sources/{fid}").status_code == 404
        assert client.post(f"/api/research-sessions/{first}/messages", json={}).status_code == 422
        assert client.get(f"/api/research-sessions/{first}/export?format=json").json()["report_kind"] == "research_preparation"
        html = client.get(f"/api/research-sessions/{first}/export?format=html").text
        assert '<script>' not in html and "Financial model not connected" in html


def test_cli_opens_conversation_and_confirms_options(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("VALUATION_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VALUATION_LLM_API_KEY", raising=False)
    monkeypatch.setattr("valuationagent.cli.main.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("valuationagent.cli.research.configure_model", lambda language: (None, ""))
    answers = iter(["/company Research Company", "/quit"])
    monkeypatch.setattr("valuationagent.cli.main.text_input", lambda *args, **kwargs: next(answers))
    monkeypatch.setattr("valuationagent.cli.main.select", lambda *args, **kwargs: "accept")
    interactive(language="en-US")
    assert "What can I do?" in capsys.readouterr().out
    assert "research" in CliRunner().invoke(app, ["--help"]).output
    assert SQLiteRunStore(tmp_path).list_research()[0].draft.company == "Research Company"


@pytest.mark.parametrize("thinking,expected", [("auto", "required"), ("enabled", "auto")])
def test_deepseek_tool_payload_and_redacted_diagnostics(thinking, expected):
    recorded = []
    secret = "TEST_KEY_NOT_REAL"
    def handler(request):
        recorded.append(json.loads(request.content))
        return httpx.Response(400, json={"error": {"message": "tool_choice rejected " + secret}})
    original = httpx.Client
    config = ModelConnectionInput(base_url="https://api.deepseek.com", model="deepseek-v4-flash", api_key=secret, thinking=thinking)
    with patch("valuationagent.llm.client.httpx.Client", side_effect=lambda **kwargs: original(transport=httpx.MockTransport(handler), **kwargs)):
        with pytest.raises(Exception) as caught:
            OpenAICompatibleClient(config).chat([{"role": "user", "content": "hello"}], tools=[{"type": "function"}], tool_choice="required")
    assert recorded[0]["tool_choice"] == expected
    assert "temperature" not in recorded[0]
    assert "tool_choice" in str(caught.value) and secret not in str(caught.value)
