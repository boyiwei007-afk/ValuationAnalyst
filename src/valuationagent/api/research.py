import json
from fastapi import BackgroundTasks, HTTPException, Query
from fastapi.responses import Response
from pydantic import Field, SecretStr
from valuationagent.application.research_export import build_research_export
from valuationagent.market import TushareApiClient, TushareDataProvider
from valuationagent.search.providers import TavilySearchProvider
from valuationagent.schemas.models import ApiModel, ResumeInput
from valuationagent.schemas.research import ResearchCreate, ResearchTurn
from valuationagent.schemas.agent import SearchQuery


class DataServicesInput(ApiModel):
    verify_search: bool = False
    tavily_api_key: SecretStr | None = Field(default=None)
    tushare_token: SecretStr | None = Field(default=None)

    def has_values(self):
        return bool(
            (self.tavily_api_key and self.tavily_api_key.get_secret_value().strip())
            or (self.tushare_token and self.tushare_token.get_secret_value().strip())
        )


def register_research_routes(app, research, sessions, runner, execute_background):
    def get_session(session_id):
        try:
            return research.store.get_research(session_id)
        except KeyError:
            raise HTTPException(404, "research session not found") from None

    def get_client(model_session_id):
        try:
            return sessions.client(model_session_id)
        except KeyError:
            raise HTTPException(404, "model session not found") from None

    @app.post("/api/research-sessions", status_code=201)
    def create(body: ResearchCreate):
        session = research.create(body.language, get_client(body.model_session_id) if body.model_session_id else None,
                                  data_source_preference=body.data_source_preference)
        return research.snapshot(session.session_id)

    @app.get("/api/research-sessions")
    def list_sessions():
        return [{"session_id": s.session_id, "draft": s.draft, "language": s.language,
                 "updated_at": s.updated_at, "status": s.status, "revision": s.revision}
                for s in research.store.list_research()]

    @app.get("/api/research-sessions/{session_id}")
    def snapshot(session_id: str, compact: bool = False, after: int | None = Query(default=None, ge=0)):
        get_session(session_id)
        return research.snapshot(session_id, compact=compact, after=after)

    def execute_turn(session_id, body):
        try:
            result = research.turn(session_id, body, reserved=True, compact_result=True)
            if result.get("action", {}).get("type") == "submit_valuation":
                record = research.submit_valuation(session_id, runner)
                if str(record.status) == "created":
                    execute_background(record.run_id)
        except Exception:
            research.store.update_research_job(session_id, body.request_id, status="failed", stage="interrupted")
            research.store.append_event(session_id, type="turn.interrupted", stage="research", status="failed",
                                        summary="执行中断，已保存资料；可以继续本次任务。")

    @app.post("/api/research-sessions/{session_id}/turns", status_code=202)
    def enqueue_turn(session_id: str, body: ResearchTurn, background_tasks: BackgroundTasks):
        get_session(session_id)
        try:
            request_id, created = research.reserve_turn(session_id, body)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        if created:
            background_tasks.add_task(execute_turn, session_id, body.model_copy(update={"request_id": request_id}))
        return {"request_id": request_id, "execution": research.execution_state(session_id)}

    @app.post("/api/research-sessions/{session_id}/cancel")
    def cancel(session_id: str):
        get_session(session_id)
        return research.cancel_turn(session_id)

    @app.post("/api/research-sessions/{session_id}/resume-turn", status_code=202)
    def resume_turn(session_id: str, background_tasks: BackgroundTasks):
        session = get_session(session_id)
        job = research.store.research_job(session_id)
        if not job:
            raise HTTPException(409, "没有可恢复的任务")
        body = json.loads(job["body_json"])
        if body.get("option_id") and (not session.question or session.question.question_id != body.get("question_id")):
            body.update(question_id=None, option_id=None, content="继续完成上一项研究任务，复用已完成的资料。")
        if session.question and session.question.kind == "recovery":
            body.update(question_id=session.question.question_id, option_id="retry", content="")
        return enqueue_turn(session_id, ResearchTurn(**body), background_tasks)

    @app.get("/api/research-sessions/{session_id}/messages")
    def message_page(session_id: str, before: int | None = None):
        get_session(session_id)
        return research.store.message_page(session_id, before)

    @app.get("/api/research-sessions/{session_id}/events/{sequence}")
    def event_detail(session_id: str, sequence: int):
        get_session(session_id)
        try:
            return research.store.event_detail(session_id, sequence)
        except KeyError:
            raise HTTPException(404, "event not found") from None

    def launch_valuation(session_id: str, background_tasks: BackgroundTasks):
        record = research.submit_valuation(session_id, runner)
        if str(record.status) == "created":
            background_tasks.add_task(execute_background, record.run_id)
        return record

    @app.post("/api/research-sessions/{session_id}/messages")
    def turn(session_id: str, body: ResearchTurn, background_tasks: BackgroundTasks):
        get_session(session_id)
        try:
            result = research.turn(session_id, body)
            if result.get("action", {}).get("type") == "submit_valuation":
                record = launch_valuation(session_id, background_tasks)
                result = research.snapshot(session_id)
                result["action"] = {
                    "type": "valuation_submitted",
                    "run_id": record.run_id,
                    "status": record.status,
                }
            return result
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.post("/api/research-sessions/{session_id}/model-session", status_code=204)
    def attach(session_id: str, body: ResumeInput):
        get_session(session_id)
        if not body.model_session_id:
            raise HTTPException(422, "model_session_id is required")
        try:
            research.attach(session_id, get_client(body.model_session_id))
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None

    @app.get("/api/research-sessions/{session_id}/data-services")
    def data_services(session_id: str):
        get_session(session_id)
        return research.data_service_status(session_id, default_market=runner.data)

    @app.post("/api/research-sessions/{session_id}/data-services")
    def attach_data_services(session_id: str, body: DataServicesInput):
        get_session(session_id)
        if not body.has_values():
            raise HTTPException(422, "至少填写一个 Tavily Key 或 Tushare Token。")
        try:
            if body.tavily_api_key and body.tavily_api_key.get_secret_value().strip():
                provider = TavilySearchProvider(body.tavily_api_key.get_secret_value())
                if body.verify_search:
                    result = provider.search(SearchQuery(query="上市公司 年度报告 官方公告", purpose="other", candidate_limit=1))
                    if result.status not in {"completed", "no_results"}:
                        raise HTTPException(502, result.error_message or "搜索连接验证失败")
                    provider.connection_verified = True
                research.attach_search(
                    session_id,
                    provider,
                )
            if body.tushare_token and body.tushare_token.get_secret_value().strip():
                research.attach_market(
                    session_id,
                    TushareDataProvider(TushareApiClient(body.tushare_token.get_secret_value())),
                )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return research.data_service_status(session_id, default_market=runner.data)

    @app.post("/api/research-sessions/{session_id}/valuation", status_code=202)
    def submit_valuation(session_id: str, background_tasks: BackgroundTasks):
        get_session(session_id)
        try:
            record = launch_valuation(session_id, background_tasks)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from None
        return {"run_id": record.run_id, "status": record.status}

    @app.get("/api/research-sessions/{session_id}/events")
    def events(session_id: str, after: int = 0):
        get_session(session_id)
        return research.store.list_events(session_id, max(0, after))

    @app.get("/api/research-sessions/{session_id}/sources/{file_id}")
    def sources(session_id: str, file_id: str, offset: int = 0):
        get_session(session_id)
        try:
            blocks = research.store.research_blocks(session_id, file_id)
        except ValueError:
            raise HTTPException(404, "source not found in this research session") from None
        return {"total": len(blocks), "blocks": blocks[max(0, offset):max(0, offset) + 12]}

    @app.get("/api/research-sessions/{session_id}/export")
    def export(session_id: str, format: str = "json"):
        get_session(session_id)
        try:
            content, media_type = build_research_export(research, session_id, format)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        return Response(content, media_type=media_type, headers={
            "Content-Disposition": f'attachment; filename="{session_id}.{format}"'})
