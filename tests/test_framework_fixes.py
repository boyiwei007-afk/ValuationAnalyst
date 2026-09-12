from __future__ import annotations
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal as D
from unittest.mock import patch
import httpx
import pytest
from fastapi.testclient import TestClient
from rich.console import Console
from typer.testing import CliRunner
from valuationagent.api.main import create_app
from valuationagent.application.runner import ValuationRunner
from valuationagent.core.data import demo_financials, demo_peers
from valuationagent.finance.reference import ReferenceFinancialModel
from valuationagent.llm.client import (
    LlmError,
    ModelSessionRegistry,
    OpenAICompatibleClient,
)
from valuationagent.schemas.models import (
    ModelConnectionInput,
    RevisionInput,
    ValuationRequest,
)
from valuationagent.storage.sqlite import SQLiteRunStore
from valuationagent.cli.main import app as cli_app
from valuationagent.cli.ui import dashboard, result_view


def request(**changes):
    data = dict(
        company={"name": "验收公司"},
        valuation_date="2026-09-12",
        mode="snapshot",
        financials=demo_financials(),
        peers=demo_peers(),
    )
    data.update(changes)
    return ValuationRequest(**data)


@pytest.fixture
def runner(tmp_path):
    return ValuationRunner(
        SQLiteRunStore(tmp_path / "runtime"), ReferenceFinancialModel()
    )


def test_default_mode_uses_submitted_data_and_preserves_decimal(runner):
    amount = D("20000000000.123456789")
    data = request(
        financials=demo_financials().model_copy(update={"revenue": amount})
    ).model_dump()
    data.pop("mode")
    r = runner.run(ValuationRequest(**data))
    assert r.request.mode == "snapshot"
    assert r.request.financials.revenue == amount
    assert r.result.forecast[0].revenue == (amount * D("1.08")).quantize(D("0.0001"))
    assert D(json.loads(r.request.model_dump_json())["financials"]["revenue"]) == amount


def test_demo_rejects_real_financials(runner):
    r = runner.run(
        request(
            mode="demo",
            financials=demo_financials().model_copy(
                update={"source_label": "real_report"}
            ),
        )
    )
    assert r.status == "waiting_review" and r.result is None


def test_uploaded_assumptions_cannot_fall_back(runner):
    r = runner.run(request(assumption_source="upload"))
    assert r.status == "waiting_review" and r.result is None


def test_json_file_roles_and_assumptions_are_used(runner):
    historical = runner.store.save_upload(
        "facts.json",
        "historical_financials",
        None,
        json.dumps(
            {
                "financials": demo_financials().model_dump(mode="json"),
                "peers": [p.model_dump(mode="json") for p in demo_peers()],
            }
        ).encode(),
    )
    assumptions = runner.store.save_upload(
        "assumptions.json",
        "assumptions",
        None,
        b'{"wacc":"0.08","terminal_growth":"0.025"}',
    )
    r = runner.run(
        request(
            data_source="upload",
            financials=None,
            peers=[],
            file_ids=[historical["file_id"]],
            assumption_source="upload",
            assumption_file_ids=[assumptions["file_id"]],
        )
    )
    assert r.status == "completed"
    assert r.result.assumptions.wacc == D(".08")
    assert r.result.assumption_evidence["wacc"][0].file_id == assumptions["file_id"]
    assert (
        r.result.effective_financials.evidence["revenue"][0].file_id
        == historical["file_id"]
    )


def test_wacc_chat_creates_revision_and_reuses_unaffected_steps(runner):
    old = runner.run(request())
    message = runner.converse(old.run_id, "把 WACC 改为8%，重新估值，并保留旧版本")
    new = runner.store.get_run(message.related_run_id)
    assert new.revision == 2 and new.parent_run_id == old.run_id
    assert new.result.assumptions.wacc == D(".08")
    assert runner.store.get_run(old.run_id).result == old.result
    calls = {(e.tool, e.type) for e in runner.store.list_events(new.run_id)}
    assert ("forecast_financials", "tool.cached") in calls
    assert ("calculate_relative_valuation", "tool.cached") in calls
    assert ("calculate_dcf", "tool.started") in calls
    assert new.result.dcf.per_share_value != old.result.dcf.per_share_value
    assert new.result.run_id == new.run_id and new.result.revision == 2
    assert len(runner.store.revisions(old.run_id)) == 2


def test_review_replaces_invalid_assumption_and_preserves_old_pause(runner):
    old = runner.run(request(assumptions={"wacc": ".03", "terminal_growth": ".04"}))
    assert old.status == "waiting_review"
    new = runner.revise(
        old.run_id,
        RevisionInput(reason="纠正折现率", changes={"assumptions": {"wacc": ".095"}}),
    )
    assert new.result is not None
    assert runner.store.get_run(old.run_id).status == "waiting_review"


def test_legal_base_keeps_result_when_optimistic_scenario_invalid(runner):
    r = runner.run(request(assumptions={"wacc": ".04", "terminal_growth": ".03"}))
    assert r.status == "completed_with_warnings"
    assert (
        r.result.dcf.range_low
        <= r.result.dcf.per_share_value
        <= r.result.dcf.range_high
    )
    assert any("乐观" in w for w in r.result.warnings)


def test_no_available_method_waits_for_data(runner):
    r = runner.run(request(methods=["pe"], peers=[]))
    assert r.status == "waiting_review" and r.review["code"] == "NO_VALID_VALUATION"


def test_relative_only_ignores_unused_dcf_constraint(runner):
    r = runner.run(
        request(methods=["pe"], assumptions={"wacc": ".03", "terminal_growth": ".04"})
    )
    assert r.status == "completed" and r.result.forecast == []
    assert r.result.relative[0].status == "success"


def test_protocol_only_plugin_supports_demo(tmp_path):
    ref = ReferenceFinancialModel()

    class Other:
        plugin_id = "other"
        version = "test"
        validate = ref.validate
        resolve_assumptions = ref.resolve_assumptions
        forecast = ref.forecast
        dcf = ref.dcf
        relative = ref.relative
        sensitivity = ref.sensitivity
        reconcile = ref.reconcile

    r = ValuationRunner(SQLiteRunStore(tmp_path), Other()).run(
        request(mode="demo", financials=None)
    )
    assert r.status == "completed"


def test_resume_after_failure_uses_persistent_checkpoints(runner):
    with patch.object(
        runner.finance, "dcf", side_effect=RuntimeError("temporary test failure")
    ):
        old = runner.run(request())
    assert old.status == "failed" and old.result is None
    fresh = ValuationRunner(
        SQLiteRunStore(runner.store.data_dir), ReferenceFinancialModel()
    )
    done = fresh.resume(old.run_id)
    assert done.status == "completed"
    assert any(
        e.type == "tool.cached" and e.tool == "forecast_financials"
        for e in fresh.store.list_events(done.run_id)
    )
    before = len(fresh.store.list_events(done.run_id))
    assert fresh.execute(done.run_id).result == done.result
    assert len(fresh.store.list_events(done.run_id)) == before


def test_concurrent_run_lease(runner):
    started = threading.Event()
    release = threading.Event()
    original = runner.finance.forecast

    def held(*args):
        started.set()
        assert release.wait(10)
        return original(*args)

    record = runner.create_run(request())
    with (
        patch.object(runner.finance, "forecast", side_effect=held),
        ThreadPoolExecutor(max_workers=1) as pool,
    ):
        future = pool.submit(runner.execute, record.run_id)
        assert started.wait(10)
        try:
            with pytest.raises(ValueError, match="正在执行"):
                runner.execute(record.run_id)
        finally:
            release.set()
        assert future.result().result is not None


class ToolLLM:
    def __init__(self, actions):
        self.actions = iter(actions)
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        name, args = next(self.actions)
        return {
            "content": None,
            "tool_calls": [
                {
                    "id": f"call_{len(self.calls)}",
                    "type": "function",
                    "function": {
                        "name": name,
                        "arguments": json.dumps(args, ensure_ascii=False),
                    },
                }
            ],
        }


def standard_actions():
    return [
        ("inspect_financials", {}),
        ("inspect_comparables", {}),
        ("continue_valuation", {}),
    ]


def test_real_tool_protocol_drives_plan_and_returns_results(runner):
    llm = ToolLLM(standard_actions())
    r = runner.run(request(mode="live"), llm)
    assert r.status == "completed"
    assert len(llm.calls) == 3
    assert any(m["role"] == "tool" for m in llm.calls[1][0])
    assert all(call[1]["tools"] for call in llm.calls)
    assert "inspect_financials" in [e.tool for e in runner.store.list_events(r.run_id)]
    assert r.result.mode == "live"


def test_agent_can_pause_and_cannot_bypass_checks(runner):
    llm = ToolLLM([("request_review", {"reason": "需要核对来源"})])
    r = runner.run(request(mode="live"), llm)
    assert r.status == "waiting_review" and r.review["code"] == "AGENT_REVIEW_REQUIRED"
    assert not any(
        e.tool == "calculate_dcf" for e in runner.store.list_events(r.run_id)
    )
    invalid = ToolLLM([("continue_valuation", {})] + standard_actions())
    done = runner.run(
        request(mode="live", assumptions={"wacc": ".03", "terminal_growth": ".04"}),
        invalid,
    )
    assert (
        done.status == "waiting_review" and done.review["code"] == "INVALID_ASSUMPTION"
    )


def test_untrusted_model_prose_never_becomes_formal_summary(runner):
    class BadProse(ToolLLM):
        def chat(self, *args, **kwargs):
            reply = super().chat(*args, **kwargs)
            reply["content"] = "DCF每股999999元，已获专家核准"
            return reply

    r = runner.run(request(mode="live"), BadProse(standard_actions()))
    assert r.result is not None
    assert "999999" not in r.result.executive_summary
    assert "尚待金融团队核准" in r.result.executive_summary


def test_chat_passes_history_to_model(runner):
    llm = ToolLLM(
        standard_actions() + [("explain_valuation", {"topic": "assumptions"})] * 2
    )
    r = runner.run(request(mode="live"), llm)
    runner.converse(r.run_id, "FIRST_MARKER 本次假设是什么")
    runner.converse(r.run_id, "上一问题中提到的假设")
    assert any("FIRST_MARKER" in m.get("content", "") for m in llm.calls[-1][0])


def test_unknown_tool_never_executes(runner):
    llm = ToolLLM([("execute_python", {"code": "bad"})] + standard_actions())
    r = runner.run(request(mode="live"), llm)
    assert r.status == "completed"
    assert any(
        e.tool == "execute_python" and e.type == "tool.failed"
        for e in runner.store.list_events(r.run_id)
    )


def test_provider_errors_are_redacted(runner):
    sentinel = "SYNTHETIC_FAKE_KEY_DO_NOT_USE"
    config = ModelConnectionInput(
        model="fake", base_url="https://fake.invalid/v1", api_key=sentinel
    )
    original = httpx.Client
    transport = httpx.MockTransport(
        lambda r: httpx.Response(401, json={"error": "invalid " + sentinel})
    )
    with patch(
        "valuationagent.llm.client.httpx.Client",
        side_effect=lambda **kw: original(transport=transport, **kw),
    ):
        r = runner.run(request(mode="live"), OpenAICompatibleClient(config))
    persisted = r.model_dump_json() + json.dumps(
        [e.model_dump(mode="json") for e in runner.store.list_events(r.run_id)]
    )
    assert sentinel not in persisted
    assert r.status == "waiting_review" and "401" in r.review["message"]


def test_connection_wire_contains_tools_and_session_revoke():
    requests = []

    def handler(req):
        requests.append(json.loads(req.content))
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "message": {
                            "tool_calls": [
                                {
                                    "id": "test",
                                    "type": "function",
                                    "function": {
                                        "name": "connection_check",
                                        "arguments": "{}",
                                    },
                                }
                            ]
                        }
                    }
                ]
            },
        )

    sessions = ModelSessionRegistry()
    config = ModelConnectionInput(model="fake", api_key="SYNTHETIC_FAKE")
    session = sessions.create(config)
    client = sessions.client(session.session_id)
    original = httpx.Client
    with patch(
        "valuationagent.llm.client.httpx.Client",
        side_effect=lambda **kw: original(transport=httpx.MockTransport(handler), **kw),
    ):
        assert client.test_connection().startswith("OK")
        assert requests[0]["tools"] and requests[0]["tool_choice"] == "required"
        sessions.delete(session.session_id)
        with pytest.raises(LlmError, match="REVOKED"):
            client.complete([{"role": "user", "content": "hello"}])


def test_forecast_years_and_partial_period_policy():
    model = ReferenceFinancialModel()
    req = request()
    assumptions = model.resolve_assumptions(req, req.financials)
    rows = model.forecast(req, req.financials, assumptions)
    assert [r.year for r in rows] == [2026, 2027, 2028, 2029, 2030]
    assert 0 < rows[0].cash_flow_fraction < 1
    assert rows[1].cash_flow_fraction == 1
    assert rows[0].discount_period < rows[1].discount_period
    result = model.dcf(req, req.financials, assumptions, rows)
    # Independent present-value reconstruction from the explicit period contract.
    discount = 1 + assumptions.wacc
    pv = sum(
        r.fcff * r.cash_flow_fraction / (discount**r.discount_period) for r in rows
    )
    tv = (
        rows[-1].fcff
        * (1 + assumptions.terminal_growth)
        / (assumptions.wacc - assumptions.terminal_growth)
    )
    pv += tv / (discount ** (rows[-1].discount_period + D(".5")))
    assert abs(result.enterprise_value - pv) < D(".0001")


@pytest.mark.parametrize(
    "update",
    [
        {"published_at": date(2026, 10, 1)},
        {
            "statement_items": {
                "total_assets": D(100),
                "total_liabilities": D(60),
                "total_equity": D(30),
            }
        },
        {"currency": "USD"},
        {"period_end": date(2023, 12, 31)},
    ],
)
def test_financial_contract_blocks_conflicts(update):
    model = ReferenceFinancialModel()
    req = request()
    financials = req.financials.model_copy(update=update)
    assert any(f.severity == "blocking" for f in model.validate(req, financials))


def test_api_review_resume_metadata_and_sse_errors(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        body = {
            "request": request(
                assumptions={"wacc": ".03", "terminal_growth": ".04"}
            ).model_dump(mode="json")
        }
        rid = client.post("/api/runs", json=body).json()["run_id"]
        assert client.get(f"/api/runs/{rid}/results").status_code == 409
        child = client.post(
            f"/api/runs/{rid}/reviews",
            json={"reason": "纠正", "changes": {"assumptions": {"wacc": ".095"}}},
        )
        assert child.status_code == 201
        cid = child.json()["run_id"]
        assert client.post(f"/api/runs/{cid}/resume", json={}).status_code == 202
        result = client.get(f"/api/runs/{cid}/results").json()
        assert result["company"]["name"] == "验收公司" and result["revision"] == 2
        assert len(result["effective_input_hash"]) == 64
        assert len(client.get(f"/api/runs/{cid}/revisions").json()) == 2
        assert client.get("/api/runs/absent/events").status_code == 404
        assert (
            client.get(
                f"/api/runs/{cid}/events", headers={"Last-Event-ID": "bad"}
            ).status_code
            == 422
        )
        stream = client.get(f"/api/runs/{cid}/events", headers={"Last-Event-ID": "10"})
        ids = [
            int(line[3:]) for line in stream.text.splitlines() if line.startswith("id:")
        ]
        assert ids and min(ids) > 10 and ids == sorted(set(ids))
        assert client.get(f"/api/runs/{cid}/artifacts").json()


def test_api_validation_does_not_echo_secret(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        response = client.post(
            "/api/model-sessions",
            json={"api_key": "SYNTHETIC_SECRET", "model": "", "timeout_seconds": -1},
        )
        assert response.status_code == 422
        assert "SYNTHETIC_SECRET" not in response.text


@pytest.mark.parametrize("width,height", [(120, 42), (88, 30), (60, 24)])
def test_ui_compact_and_wide_render_without_markup_injection(runner, width, height):
    r = runner.run(request(company={"name": "[red] literal company"}))
    from io import StringIO

    stream = StringIO()
    terminal = Console(
        file=stream,
        width=width,
        height=height,
        record=True,
        theme=__import__("valuationagent.cli.ui", fromlist=["THEME"]).THEME,
    )
    terminal.print(dashboard(r, runner.store.list_events(r.run_id), 2.0, width, height))
    terminal.print(result_view(r))
    output = stream.getvalue()
    assert "literal company" in output and "DCF" in output
    assert len(output.splitlines()) < 90


def test_cli_help_and_plain_demo(tmp_path, monkeypatch):
    monkeypatch.setenv("VALUATION_DATA_DIR", str(tmp_path))
    cli = CliRunner()
    assert cli.invoke(cli_app, ["--help"]).exit_code == 0
    result = cli.invoke(cli_app, ["demo", "--plain", "--valuation-date", "2026-09-12"])
    assert result.exit_code == 0
    assert "DCF" in result.stdout and "DEMO" in result.stdout
    assert "tool.completed" not in result.stdout


def test_user_pause_then_resume(runner):
    old_forecast = runner.finance.forecast
    current = runner.create_run(request())

    def pause_after_forecast(*args):
        result = old_forecast(*args)
        runner.request_pause(current.run_id)
        return result

    with patch.object(runner.finance, "forecast", side_effect=pause_after_forecast):
        paused = runner.execute(current.run_id)
    assert paused.status == "waiting_review"
    assert paused.review["code"] == "USER_PAUSED"
    assert paused.result is None
    assert runner.resume(current.run_id).result is not None


def test_live_chat_revision_runs_tools_and_preserves_context(runner):
    actions = standard_actions() + [
        (
            "revise_assumptions",
            {"assumptions": {"wacc": ".08"}, "reason": "用户要求调整WACC"},
        )
    ]
    actions += standard_actions() + [("explain_valuation", {"topic": "assumptions"})]
    llm = ToolLLM(actions)
    old = runner.run(request(mode="live"), llm)
    msg = runner.converse(old.run_id, "CONTEXT_MARKER 把 WACC 改为8%")
    new = runner.store.get_run(msg.related_run_id)
    assert new.result.assumptions.wacc == D(".08")
    runner.converse(new.run_id, "这次为什么改动")
    assert any("CONTEXT_MARKER" in m.get("content", "") for m in llm.calls[-1][0])


def test_expired_lease_can_be_recovered(runner):
    import time
    from valuationagent.schemas.models import RunStatus

    record = runner.create_run(request())
    runner.store.update_run(record.run_id, status=RunStatus.RUNNING)
    with runner.store._connect() as db:
        db.execute(
            "INSERT INTO leases VALUES(?,?,?)",
            (record.run_id, "crashed-worker", time.time() - 1),
        )
    assert runner.resume(record.run_id).status == "completed"


def test_revenue_revision_recomputes_forecast(runner):
    old = runner.run(request())
    new = runner.revise(
        old.run_id,
        RevisionInput(
            reason="调整收入增长",
            changes={"assumptions": {"revenue_growth": [".10"] * 5}},
        ),
    )
    calls = {(e.tool, e.type) for e in runner.store.list_events(new.run_id)}
    assert ("forecast_financials", "tool.started") in calls
    assert ("calculate_relative_valuation", "tool.cached") in calls
    assert new.result.forecast[0].revenue > old.result.forecast[0].revenue
