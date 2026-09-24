from __future__ import annotations
from decimal import Decimal

from fastapi.testclient import TestClient

from valuationagent.api.main import create_app


def demo_body():
    return {
        "request": {
            "company": {"name": "API 演示公司", "currency": "CNY"},
            "valuation_date": "2026-09-12",
            "data_source": "structured",
            "assumption_source": "automatic",
            "mode": "demo",
            "forecast_years": 5,
            "methods": ["dcf", "pe", "ev_ebitda"],
            "assumptions": {},
            "peers": [],
            "file_ids": [],
            "user_goal": "通过 Web API 完成演示估值",
        }
    }


def test_api_run_events_results_and_conversation(tmp_path):
    app = create_app(tmp_path / "api-runtime")
    with TestClient(app) as client:
        response = client.post("/api/runs", json=demo_body())
        assert response.status_code == 202
        run_id = response.json()["run_id"]

        record = client.get(f"/api/runs/{run_id}")
        assert record.status_code == 200
        assert record.json()["status"] == "completed"
        assert len(record.json()["input_hash"]) == 64

        history = client.get(f"/api/runs/{run_id}/events/history")
        assert history.status_code == 200
        assert any(event["type"] == "run.completed" for event in history.json())

        results = client.get(f"/api/runs/{run_id}/results")
        assert results.status_code == 200
        assert Decimal(results.json()["dcf"]["per_share_value"]) > 0

        answer = client.post(
            f"/api/runs/{run_id}/messages", json={"content": "请说明 WACC"}
        )
        assert answer.status_code == 200
        assert "WACC" in answer.json()["content"]

        messages = client.get(f"/api/runs/{run_id}/messages")
        assert messages.status_code == 200
        assert {item["role"] for item in messages.json()} >= {"user", "assistant"}

        with client.stream("GET", f"/api/runs/{run_id}/events") as stream:
            assert stream.status_code == 200
            lines = [line for line in stream.iter_lines() if line]
        assert any("event: run.completed" in line for line in lines)


def test_capabilities_report_runtime_adapters(tmp_path):
    app = create_app(tmp_path / "api-runtime")
    with TestClient(app) as client:
        capabilities = {
            item["capability_id"]: item
            for item in client.get("/api/capabilities").json()
        }
    assert capabilities["structured_financial_input"]["available"] is True
    assert capabilities["durable_conversation_context"]["available"] is True
    assert capabilities["interactive_agent_recovery"]["available"] is True
    assert capabilities["agent_tool_extensions"]["available"] is True
    assert capabilities["ticker_data_provider"]["available"] is True
    assert capabilities["pdf_excel_extraction"]["available"] is False


def test_upload_records_hash_but_does_not_claim_to_parse(tmp_path):
    app = create_app(tmp_path / "api-runtime")
    with TestClient(app) as client:
        response = client.post(
            "/api/files",
            data={"role": "historical_financials"},
            files={
                "file": (
                    "financials.xlsx",
                    b"placeholder workbook bytes",
                    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                )
            },
        )
        assert response.status_code == 201
        assert response.json()["sha256"]
        capabilities = {
            item["capability_id"]: item
            for item in client.get("/api/capabilities").json()
        }
        assert capabilities["pdf_excel_extraction"]["available"] is False
