from __future__ import annotations

from decimal import Decimal
from io import StringIO
import json

from fastapi.testclient import TestClient
from pydantic import ValidationError
import pytest
from prompt_toolkit.application import create_app_session
from prompt_toolkit.input import DummyInput
from prompt_toolkit.output import DummyOutput
from rich.cells import cell_len
from rich.console import Console
from typer.testing import CliRunner

from valuationagent.api.main import create_app
from valuationagent.application.runner import ValuationRunner
from valuationagent.cli.main import app, wizard
from valuationagent.cli.ui import THEME, welcome
from valuationagent.core.data import demo_financials, demo_peers
from valuationagent.finance.reference import ReferenceFinancialModel
from valuationagent.schemas.models import RevisionInput, ValuationRequest
from valuationagent.storage.sqlite import SQLiteRunStore


def request(**changes):
    return ValuationRequest(
        **{
            "company": {"name": "Example Company"},
            "valuation_date": "2026-09-12",
            "mode": "snapshot",
            "financials": demo_financials(),
            "peers": demo_peers(),
            **changes,
        }
    )


@pytest.mark.parametrize("width", [40, 60, 62, 70, 92, 94, 112, 160])
def test_welcome_fits_terminal_without_losing_workflow(width):
    stream = StringIO()
    terminal = Console(
        file=stream, width=width, height=40, legacy_windows=False, theme=THEME
    )
    terminal.print(welcome(width))
    lines = stream.getvalue().splitlines()
    assert all(cell_len(line) <= width for line in lines)
    assert "ValuationAgent" in stream.getvalue()
    assert "Data & Evidence" in stream.getvalue()
    assert "Report & Dialogue" in stream.getvalue()


def test_short_terminal_keeps_brand_and_language_prompt_visible():
    stream = StringIO()
    terminal = Console(
        file=stream, width=80, height=24, legacy_windows=False, theme=THEME
    )
    terminal.print(welcome(80, 24))
    # Leave ten rows for the language panel and its interactive choices.
    assert len(stream.getvalue().splitlines()) <= 14
    assert "ValuationAgent" in stream.getvalue()


def test_language_survives_restart_and_revision_without_changing_values(tmp_path):
    runner = ValuationRunner(SQLiteRunStore(tmp_path), ReferenceFinancialModel())
    original = runner.run(request())
    english = runner.revise(
        original.run_id,
        RevisionInput(
            reason="Use English",
            changes={"language": "en-US"},
        ),
    )
    assert english.result.language == "en-US"
    assert english.result.dcf == original.result.dcf
    assert english.result.sensitivity == original.result.sensitivity
    assert "DCF base" in english.result.executive_summary
    assert "financial team approval pending" in english.result.executive_summary
    assert runner.store.get_run(original.run_id).request.language == "zh-CN"

    fresh = ValuationRunner(SQLiteRunStore(tmp_path), ReferenceFinancialModel())
    assert "terminal growth" in fresh.answer(
        english.run_id, "What assumptions were used?"
    )
    assert "combinations" in fresh.answer(english.run_id, "Explain WACC sensitivity")
    message = fresh.converse(
        english.run_id, "Set WACC to 8% and terminal growth to 2.5%"
    )
    child = fresh.store.get_run(message.related_run_id)
    assert child.request.language == "en-US"
    assert child.result.assumptions.wacc == Decimal(".08")
    assert child.result.assumptions.terminal_growth == Decimal(".025")
    assert "Created version 3" in message.content


def test_api_language_roundtrip_and_validation(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        response = client.post(
            "/api/runs",
            json={"request": request(language="en-US").model_dump(mode="json")},
        )
        assert response.status_code == 202
        run_id = response.json()["run_id"]
        assert (
            client.get(f"/api/runs/{run_id}").json()["request"]["language"] == "en-US"
        )
        assert client.get(f"/api/runs/{run_id}/results").json()["language"] == "en-US"
        answer = client.post(
            f"/api/runs/{run_id}/messages", json={"content": "Explain the risks"}
        )
        assert "Review capital costs" in answer.json()["content"]
        invalid = request().model_dump(mode="json")
        invalid["language"] = "unsupported"
        assert client.post("/api/runs", json={"request": invalid}).status_code == 422


def test_agent_and_plugin_receive_selected_parameters(tmp_path):
    class Model:
        def __init__(self):
            self.calls = []
            self.actions = iter(
                [
                    ("inspect_financials", {}),
                    ("inspect_comparables", {}),
                    ("continue_valuation", {}),
                    ("explain_valuation", {"topic": "assumptions"}),
                ]
            )

        def chat(self, messages, **kwargs):
            self.calls.append(json.loads(json.dumps(messages)))
            name, arguments = next(self.actions)
            return {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": str(len(self.calls)),
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(arguments)},
                    }
                ],
            }

    class Finance(ReferenceFinancialModel):
        def forecast(self, request, *args):
            self.parameters = request.agent_parameters()
            return super().forecast(request, *args)

    finance, llm = Finance(), Model()
    runner = ValuationRunner(SQLiteRunStore(tmp_path), finance)
    req = request(
        language="en-US", mode="live", forecast_years=7, methods=["dcf", "pe"]
    )
    record = runner.run(req, llm)
    assert record.status == "completed"
    assert finance.parameters["language"] == "en-US"
    assert finance.parameters["forecast_years"] == 7
    assert finance.parameters["methods"] == ["dcf", "pe"]
    assert '"language": "en-US"' in llm.calls[0][0]["content"]
    financial_tool_message = next(m for m in llm.calls[1] if m["role"] == "tool")
    assert (
        json.loads(financial_tool_message["content"])["parameters"]["forecast_years"]
        == 7
    )
    runner.converse(record.run_id, "What assumptions were used?")
    assert '"language": "en-US"' in llm.calls[-1][0]["content"]


def test_cli_language_option_and_wizard_choices_reach_request(
    tmp_path, monkeypatch, capsys
):
    monkeypatch.setenv("VALUATION_DATA_DIR", str(tmp_path))
    cli = CliRunner()
    output = cli.invoke(
        app,
        ["demo", "--plain", "--language", "en-US", "--valuation-date", "2026-09-12"],
    )
    assert output.exit_code == 0, output.output
    assert "Valuation results" in output.output
    assert "Reference model" in output.output
    assert cli.invoke(app, ["demo", "--language", "fr"]).exit_code == 2

    # Exercise the real wizard orchestration with controlled terminal answers.
    answers = iter(
        [
            "en-US",
            "demo",
            "Welcome Test",
            "2026-09-12",
            "manual",
            "8",
            "2.5",
            ["dcf", "pe"],
            "7",
        ]
    )
    monkeypatch.setattr("valuationagent.cli.main.sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("valuationagent.cli.main.ask", lambda prompt: next(answers))
    monkeypatch.setattr(
        "valuationagent.cli.main.conversation", lambda *args, **kwargs: None
    )
    with create_app_session(input=DummyInput(), output=DummyOutput()):
        wizard(language=None)
    assert "Welcome to ValuationAgent" in capsys.readouterr().out
    record = next(
        r
        for r in SQLiteRunStore(tmp_path).list_runs()
        if r.request.company.name == "Welcome Test"
    )
    assert record.request.language == "en-US"
    assert record.request.assumption_source == "manual"
    assert record.request.assumptions.wacc == Decimal(".08")
    assert record.request.methods == ["dcf", "pe"]
    assert record.request.forecast_years == 7
    assert record.result.language == "en-US"


def test_cli_autocomplete_and_password_are_prompt_toolkit_compatible(monkeypatch):
    """Questionary forwards unsupported kwargs to PromptSession and crashes early."""

    from valuationagent.cli import main as cli_main

    received = []
    monkeypatch.setattr(
        cli_main.questionary,
        "autocomplete",
        lambda *args, **kwargs: received.append(("autocomplete", kwargs)) or object(),
    )
    monkeypatch.setattr(
        cli_main.questionary,
        "password",
        lambda *args, **kwargs: received.append(("password", kwargs)) or object(),
    )
    monkeypatch.setattr(cli_main, "ask", lambda prompt: "constructed")
    assert cli_main.autocomplete("Model", ["deepseek-flash"]) == "constructed"
    assert cli_main.secret_input("API Key") == "constructed"
    assert all("instruction" not in kwargs for _, kwargs in received)
    assert all(kwargs["bottom_toolbar"] for _, kwargs in received)


def test_model_picker_uses_a_clean_provider_menu_with_custom_last():
    from valuationagent.cli.research import _model_choices

    deepseek = _model_choices("deepseek")
    assert [choice.value for choice in deepseek] == [
        "deepseek-flash",
        "deepseek-v4-pro",
        "__custom__",
    ]
    assert deepseek[-1].title == "自定义模型…"
    assert all("直接输入" not in choice.title for choice in deepseek)

    openai = _model_choices("openai", en=True)
    assert [choice.value for choice in openai] == ["gpt-4o-mini", "__custom__"]
    assert openai[-1].title == "Custom model…"


def test_agent_question_menu_keeps_model_choices_and_chat_last():
    from valuationagent.cli.research import _agent_choices
    from valuationagent.schemas.research import ResearchChoice, ResearchQuestion

    question = ResearchQuestion(
        question_id="question_1",
        kind="clarification",
        title="选择收入预测口径",
        options=[
            ResearchChoice(id="history", label="采用历史增速"),
            ResearchChoice(id="industry", label="采用行业增速"),
        ],
    )
    choices = _agent_choices(question)
    assert [choice.value for choice in choices] == ["history", "industry", "__chat__"]
    assert choices[-1].title == "Chat"

    search_question = ResearchQuestion(
        question_id="question_2",
        kind="search_unavailable",
        title="当前会话未配置联网搜索服务，怎样继续？",
        options=[
            ResearchChoice(id="upload", label="我来上传资料"),
            ResearchChoice(id="defer", label="保留缺口"),
        ],
    )
    search_choices = _agent_choices(search_question)
    assert [choice.value for choice in search_choices] == [
        "__search__",
        "upload",
        "defer",
        "__chat__",
    ]
    assert search_choices[0].title == "连接 Tavily 搜索"


def test_startup_search_setup_offers_connection_before_chat(monkeypatch):
    from valuationagent.cli.research import configure_search_on_start

    sentinel = object()
    seen = {}

    def choose(title, choices, language="zh-CN"):
        seen["title"] = title
        seen["values"] = [choice.value for choice in choices]
        return "connect"

    monkeypatch.setattr("valuationagent.cli.main.select", choose)
    monkeypatch.setattr("valuationagent.cli.research.configure_search", lambda _: sentinel)
    assert configure_search_on_start("zh-CN") is sentinel
    assert seen == {
        "title": "开始前配置联网搜索吗？",
        "values": ["connect", "later"],
    }


def test_startup_online_services_can_be_configured_together(monkeypatch):
    from valuationagent.cli.research import configure_data_services_on_start

    search, market = object(), object()
    seen = {}

    def choose(title, choices, language="zh-CN"):
        seen["title"] = title
        seen["values"] = [choice.value for choice in choices]
        return "both"

    monkeypatch.setattr("valuationagent.cli.main.select", choose)
    monkeypatch.setattr("valuationagent.cli.research.configure_search", lambda _: search)
    monkeypatch.setattr("valuationagent.cli.research.configure_market_data", lambda _: market)
    assert configure_data_services_on_start(
        "zh-CN", need_search=True, need_market=True
    ) == (search, market)
    assert seen == {
        "title": "配置在线数据服务",
        "values": ["both", "market", "search", "later"],
    }


def test_legacy_requests_default_to_chinese():
    assert request().language == "zh-CN"
    with pytest.raises(ValidationError):
        request(language="unrecognized")


def test_research_cli_keeps_session_after_invalid_input_and_stale_turn(tmp_path, monkeypatch, capsys):
    from valuationagent.application.research import ResearchService
    from valuationagent.cli.research import launch_research

    monkeypatch.setenv("VALUATION_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VALUATION_LLM_API_KEY", raising=False)
    monkeypatch.setattr("valuationagent.cli.research.configure_model", lambda _: (None, ""))
    answers = iter(["x" * 8001, "first request", "please continue", "/quit"])
    monkeypatch.setattr("valuationagent.cli.main.text_input", lambda *args, **kwargs: next(answers))
    original_turn = ResearchService.turn
    calls = 0

    def conflicted_turn(service, session_id, payload):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise ValueError("Confirmation changed; please retry.")
        return original_turn(service, session_id, payload)

    monkeypatch.setattr(ResearchService, "turn", conflicted_turn)
    launch_research(language="en-US")
    output = capsys.readouterr().out
    assert "Confirmation changed" in output
    assert "8000" in output
    assert calls == 2
    store = SQLiteRunStore(tmp_path)
    session = store.list_research()[0]
    assert any(message.content == "please continue" for message in store.list_messages(session.session_id))
    assert session.session_id in output


def test_research_cli_configuration_and_export_failures_remain_recoverable(tmp_path, monkeypatch, capsys):
    from valuationagent.cli.research import launch_research
    from valuationagent.schemas.models import ModelConnectionInput

    monkeypatch.setenv("VALUATION_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("VALUATION_LLM_API_KEY", raising=False)

    def invalid_configuration(_):
        return ModelConnectionInput(model="test", api_key="TEST_ONLY_NOT_A_REAL_KEY",
                                    base_url="https://example.test/v1?key=never-echo-this")

    def blocked_export(*args):
        raise PermissionError("Cannot write the report; choose a writable folder.")

    monkeypatch.setattr("valuationagent.cli.research.configure_model", invalid_configuration)
    monkeypatch.setattr("valuationagent.cli.research.build_research_export", blocked_export)
    answers = iter(["/export html", "continue locally", "/quit"])
    monkeypatch.setattr("valuationagent.cli.main.text_input", lambda *args, **kwargs: next(answers))
    launch_research(language="en-US")
    output = capsys.readouterr().out
    assert "never-echo-this" not in output
    assert "Session preserved" in output
    assert "Cannot write the report" in output
    store = SQLiteRunStore(tmp_path)
    session = store.list_research()[0]
    assert any(message.content == "continue locally" for message in store.list_messages(session.session_id))
