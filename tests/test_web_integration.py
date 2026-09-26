from __future__ import annotations

from unittest.mock import patch
from fastapi.testclient import TestClient

from valuationagent.api.main import create_app


def test_web_is_served_with_api_and_private_files_are_not_exposed(
    tmp_path, monkeypatch
):
    web = tmp_path / "web"
    web.mkdir()
    (web / "index.html").write_text(
        "<html><title>ValuationAgent</title></html>", encoding="utf-8"
    )
    (tmp_path / "private.json").write_text("private", encoding="utf-8")
    monkeypatch.setenv("VALUATION_WEB_DIR", str(web))
    app = create_app(tmp_path / "runtime")
    with TestClient(app) as client:
        assert "ValuationAgent" in client.get("/").text
        assert client.get("/health").json()["service"] == "valuationagent"
        assert client.get("/api/runs").json() == []
        assert client.get("/private.json").status_code == 404
        assert client.get("/var/runs.sqlite3").status_code == 404
        assert client.get("/api/unknown").status_code == 404


def test_reconnecting_a_model_does_not_execute_or_revise_the_task(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        accepted = client.post(
            "/api/runs",
            json={
                "request": {
                    "company": {"name": "Web demo"},
                    "valuation_date": "2026-09-12",
                    "language": "en-US",
                    "mode": "demo",
                }
            },
        )
        assert accepted.status_code == 202
        rid = accepted.json()["run_id"]
        original = client.get(f"/api/runs/{rid}").json()
        session = client.post(
            "/api/model-sessions",
            json={"model": "mock", "api_key": "TEST_ONLY_NOT_A_REAL_KEY"},
        ).json()
        with patch.object(
            app.state.runner, "execute", side_effect=AssertionError("must not execute")
        ):
            response = client.post(
                f"/api/runs/{rid}/model-session",
                json={"model_session_id": session["session_id"]},
            )
        assert response.status_code == 204
        attached = app.state.runner._run_clients[rid]
        assert attached.config.model == "mock"
        assert not attached.revoked.is_set()
        assert client.get(f"/api/runs/{rid}").json() == original
        assert client.post(f"/api/runs/{rid}/model-session", json={}).status_code == 422
        assert (
            client.post(
                f"/api/runs/{rid}/model-session", json={"model_session_id": "missing"}
            ).status_code
            == 404
        )
        assert (
            client.post(
                "/api/runs/missing/model-session",
                json={"model_session_id": session["session_id"]},
            ).status_code
            == 404
        )
        assert app.state.store.acquire(rid, "test-owner")
        assert (
            client.post(
                f"/api/runs/{rid}/model-session",
                json={"model_session_id": session["session_id"]},
            ).status_code
            == 409
        )
        app.state.store.release(rid, "test-owner")
        assert (
            client.delete(f"/api/model-sessions/{session['session_id']}").status_code
            == 204
        )
        assert attached.revoked.is_set()


def test_web_conversation_revision_charts_and_export_contract(tmp_path):
    app = create_app(tmp_path)
    with TestClient(app) as client:
        rid = client.post(
            "/api/runs",
            json={
                "request": {
                    "company": {"name": "Browser research"},
                    "valuation_date": "2026-09-12",
                    "language": "en-US",
                    "mode": "demo",
                    "forecast_years": 7,
                }
            },
        ).json()["run_id"]
        original = client.get(f"/api/runs/{rid}/results").json()
        reply = client.post(
            f"/api/runs/{rid}/messages", json={"content": "Set WACC to 8%"}
        ).json()
        child = reply["related_run_id"]
        result = client.get(f"/api/runs/{child}/results").json()
        assert result["language"] == "en-US"
        assert result["dcf"]["per_share_value"] != original["dcf"]["per_share_value"]
        assert len(result["forecast"]) == 7
        assert len(result["sensitivity"]) > 1
        assert client.get(f"/api/runs/{rid}/results").json() == original
        events = client.get(f"/api/runs/{child}/events/history").json()
        artifacts = client.get(f"/api/runs/{child}/artifacts").json()
        assert any(e["type"] == "tool.cached" for e in events)
        completed = [
            e for e in events if e["type"] in ("tool.cached", "tool.completed")
        ]
        assert all(
            any(a["artifact_id"] == e["payload"]["artifact_id"] for a in artifacts)
            for e in completed
        )
        record = client.get(f"/api/runs/{child}").json()
        assert record["request"]["language"] == "en-US"
        assert record["parent_run_id"] == rid
        assert len(client.get(f"/api/runs/{child}/revisions").json()) == 2


def test_research_report_preserves_summary_pending_review_and_escaped_text(tmp_path):
    from valuationagent.schemas.research import ResearchChoice, ResearchQuestion

    app = create_app(tmp_path)
    session = app.state.research.create()
    session.summary = "资料摘要 <script>alert(1)</script>"
    session.question = ResearchQuestion(
        question_id="review_test", kind="clarification", title="确认 <行业口径>",
        options=[ResearchChoice(id="confirm", label="使用 <合并口径>", description="保留 & 原文依据")],
    )
    app.state.store.save_research(session)
    with TestClient(app) as client:
        response = client.get(f"/api/research-sessions/{session.session_id}/export?format=html")
        assert response.status_code == 200
        assert response.headers["content-disposition"].endswith('.html"')
        assert "资料摘要 &lt;script&gt;" in response.text
        assert "确认 &lt;行业口径&gt;" in response.text
        assert "使用 &lt;合并口径&gt;" in response.text
        assert "保留 &amp; 原文依据" in response.text
        assert '<script>' not in response.text
        assert 'name="viewport"' in response.text
        assert '方法适用性与最小输入' in response.text
        assert client.get(f"/api/research-sessions/{session.session_id}/export?format=pdf").status_code == 200
