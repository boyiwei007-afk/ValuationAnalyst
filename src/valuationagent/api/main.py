from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Annotated

from fastapi import (
    BackgroundTasks,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Request,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.sse import EventSourceResponse

from valuationagent.application.runner import ValuationRunner
from valuationagent.finance.reference import ReferenceFinancialModel
from valuationagent.llm.client import (
    LlmError,
    ModelSessionRegistry,
    OpenAICompatibleClient,
)
from valuationagent.schemas.models import (
    Capability,
    ChatInput,
    ChatMessage,
    ModelConnectionInput,
    ModelSessionPublic,
    RunAccepted,
    RunCreateBody,
    RunEvent,
    RunRecord,
    RunStatus,
    ValuationOutput,
    RevisionInput,
    ResumeInput,
)
from valuationagent.storage.sqlite import SQLiteRunStore


TERMINAL_STATUSES = {
    RunStatus.WAITING_REVIEW,
    RunStatus.COMPLETED,
    RunStatus.COMPLETED_WITH_WARNINGS,
    RunStatus.FAILED,
    RunStatus.CANCELLED,
}


def create_app(data_dir: Path | str | None = None) -> FastAPI:
    runtime_dir = Path(data_dir or os.getenv("VALUATION_DATA_DIR", "var"))
    store = SQLiteRunStore(runtime_dir)
    finance = ReferenceFinancialModel()
    runner = ValuationRunner(store, finance)
    sessions = ModelSessionRegistry()

    app = FastAPI(
        title="ValuationAgent API",
        version="0.2.0",
        description="Shared backend for agent conversation, valuation workflow, audit events and web visualization.",
    )
    origins = [
        item.strip()
        for item in os.getenv(
            "VALUATION_CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173"
        ).split(",")
        if item.strip()
    ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=origins,
        allow_credentials=False,
        allow_methods=["GET", "POST", "DELETE"],
        allow_headers=["Content-Type", "Last-Event-ID"],
    )

    app.state.store = store
    app.state.runner = runner
    app.state.sessions = sessions

    def execute_background(run_id):
        try:
            runner.execute(run_id)
        except ValueError:
            # A concurrent request may already own the execution lease.
            return

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, exc):
        return JSONResponse(
            status_code=422,
            content={
                "detail": [
                    {"loc": e["loc"], "type": e["type"], "msg": e["msg"]}
                    for e in exc.errors()
                ]
            },
        )

    def get_run_or_404(run_id: str) -> RunRecord:
        try:
            return store.get_run(run_id)
        except KeyError as exc:
            raise HTTPException(status_code=404, detail="run not found") from exc

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": "valuationagent", "version": "0.2.0"}

    @app.get("/api/capabilities", response_model=list[Capability])
    def capabilities() -> list[Capability]:
        return [
            Capability(
                capability_id="structured_financial_input",
                available=True,
                detail="结构化财务快照可完整运行",
            ),
            Capability(
                capability_id="synthetic_demo",
                available=True,
                detail="合成数据演示，结果明确标记为 demo",
            ),
            Capability(
                capability_id="agent_conversation",
                available=True,
                detail="运行消息与结果问答共用消息接口",
            ),
            Capability(
                capability_id="execution_events",
                available=True,
                detail="SQLite 审计事件 + SSE 增量流",
            ),
            Capability(
                capability_id="reference_dcf",
                available=True,
                detail="透明参考模型，正式使用前需金融团队核准",
            ),
            Capability(
                capability_id="reference_relative",
                available=True,
                detail="P/E 与 EV/EBITDA 参考实现",
            ),
            Capability(
                capability_id="review_resume_revisions",
                available=True,
                detail="复核更正、版本、检查点恢复与重算",
            ),
            Capability(
                capability_id="json_extraction",
                available=True,
                detail="按角色导入结构化 JSON，保留来源引用",
            ),
            Capability(
                capability_id="typed_agent_tools",
                available=True,
                detail="受限工具调用循环，要求模型支持 function calling",
            ),
            Capability(
                capability_id="ticker_data_provider",
                available=False,
                detail="A 股数据源适配器待接入",
            ),
            Capability(
                capability_id="pdf_excel_extraction",
                available=False,
                detail="文件接收已实现，财务解析待接入",
            ),
            Capability(
                capability_id="pdf_excel_reporting",
                available=False,
                detail="当前输出结构化 JSON；PDF/Excel 导出待接入",
            ),
        ]

    @app.get("/api/workflow-definition")
    def workflow_definition() -> dict:
        return {
            "version": "0.2.0",
            "stages": [
                {"id": "data_intake", "label": "数据输入", "order": 1, "tone": "cyan"},
                {
                    "id": "agent_planning",
                    "label": "Agent 规划",
                    "order": 2,
                    "tone": "violet",
                },
                {
                    "id": "financial_validation",
                    "label": "财务审核",
                    "order": 3,
                    "tone": "amber",
                },
                {
                    "id": "assumption_resolution",
                    "label": "假设形成",
                    "order": 4,
                    "tone": "violet",
                },
                {
                    "id": "financial_forecast",
                    "label": "经营预测",
                    "order": 5,
                    "tone": "cyan",
                },
                {
                    "id": "dcf_valuation",
                    "label": "DCF 估值",
                    "order": 6,
                    "tone": "green",
                },
                {
                    "id": "relative_valuation",
                    "label": "相对估值",
                    "order": 7,
                    "tone": "green",
                },
                {
                    "id": "sensitivity",
                    "label": "敏感性分析",
                    "order": 8,
                    "tone": "amber",
                },
                {
                    "id": "reconciliation",
                    "label": "区间验证",
                    "order": 9,
                    "tone": "violet",
                },
                {"id": "reporting", "label": "结果输出", "order": 10, "tone": "green"},
            ],
        }

    @app.post("/api/model-sessions", response_model=ModelSessionPublic, status_code=201)
    def create_model_session(config: ModelConnectionInput) -> ModelSessionPublic:
        return sessions.create(config)

    @app.post("/api/model-connections/test")
    def test_model_connection(config: ModelConnectionInput) -> dict[str, str]:
        try:
            reply = OpenAICompatibleClient(config).test_connection()
        except LlmError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return {"status": "ok", "model": config.model, "reply": reply[:60]}

    @app.delete("/api/model-sessions/{session_id}", status_code=204)
    def delete_model_session(session_id: str) -> None:
        try:
            sessions.delete(session_id)
        except KeyError as exc:
            raise HTTPException(
                status_code=404, detail="model session not found"
            ) from exc

    @app.post("/api/files", status_code=201)
    async def upload_file(
        file: Annotated[UploadFile, File(description="PDF, Excel, CSV or JSON")],
        role: Annotated[str, Form()] = "historical_financials",
    ) -> dict:
        content = await file.read(50 * 1024 * 1024 + 1)
        try:
            return store.save_upload(
                file.filename or "upload", role, file.content_type, content
            )
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

    @app.post("/api/runs", response_model=RunAccepted, status_code=202)
    def create_run(
        body: RunCreateBody, background_tasks: BackgroundTasks
    ) -> RunAccepted:
        llm = None
        if body.model_session_id:
            try:
                llm = sessions.client(body.model_session_id)
            except KeyError as exc:
                raise HTTPException(
                    status_code=404, detail="model session not found"
                ) from exc
        try:
            record = runner.create_run(body.request, llm)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc
        background_tasks.add_task(execute_background, record.run_id)
        return RunAccepted(run_id=record.run_id, status=record.status)

    @app.get("/api/runs", response_model=list[RunRecord])
    def list_runs(limit: int = 50) -> list[RunRecord]:
        return store.list_runs(limit)

    @app.get("/api/runs/{run_id}", response_model=RunRecord)
    def get_run(run_id: str) -> RunRecord:
        return get_run_or_404(run_id)

    @app.get("/api/runs/{run_id}/results", response_model=ValuationOutput)
    def get_results(run_id: str) -> ValuationOutput:
        record = get_run_or_404(run_id)
        if record.result is None or record.status not in {
            RunStatus.COMPLETED,
            RunStatus.COMPLETED_WITH_WARNINGS,
        }:
            raise HTTPException(
                status_code=409,
                detail={
                    "status": record.status,
                    "review": record.review,
                    "error": record.error,
                },
            )
        return record.result

    @app.get("/api/runs/{run_id}/events/history", response_model=list[RunEvent])
    def event_history(run_id: str, after: int = 0) -> list[RunEvent]:
        get_run_or_404(run_id)
        return store.list_events(run_id, after=max(after, 0))

    @app.get("/api/runs/{run_id}/events")
    async def stream_events(
        run_id: str,
        request: Request,
        last_event_id: Annotated[str | None, Header()] = None,
        after: int = 0,
    ):
        get_run_or_404(run_id)
        try:
            initial_cursor = max(0, after, int(last_event_id or 0))
        except ValueError:
            raise HTTPException(
                status_code=422, detail="Last-Event-ID must be an integer"
            ) from None

        async def events():
            cursor = initial_cursor
            while True:
                if await request.is_disconnected():
                    return
                for event in store.list_events(run_id, after=cursor):
                    cursor = event.sequence
                    yield f"id: {cursor}\nevent: {event.type}\ndata: {event.model_dump_json()}\nretry: 1500\n\n"
                if store.get_run(
                    run_id
                ).status in TERMINAL_STATUSES and not store.list_events(
                    run_id, after=cursor
                ):
                    return
                await asyncio.sleep(0.15)

        return EventSourceResponse(events())

    @app.get("/api/runs/{run_id}/messages", response_model=list[ChatMessage])
    def list_messages(run_id: str) -> list[ChatMessage]:
        get_run_or_404(run_id)
        return store.list_messages(run_id)

    @app.post("/api/runs/{run_id}/model-session", status_code=204)
    def attach_run_model(run_id: str, body: ResumeInput):
        """Reconnect a persisted task without executing or revising its valuation."""
        get_run_or_404(run_id)
        if not body.model_session_id:
            raise HTTPException(status_code=422, detail="model_session_id is required")
        if store.active(run_id):
            raise HTTPException(status_code=409, detail="run is already executing")
        try:
            runner.attach_model(run_id, sessions.client(body.model_session_id))
        except KeyError:
            raise HTTPException(
                status_code=404, detail="model session not found"
            ) from None

    @app.post("/api/runs/{run_id}/messages", response_model=ChatMessage)
    def add_message(run_id: str, body: ChatInput) -> ChatMessage:
        get_run_or_404(run_id)
        try:
            return runner.converse(run_id, body.content)
        except LlmError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.get("/api/runs/{run_id}/revisions", response_model=list[RunRecord])
    def revisions(run_id: str):
        get_run_or_404(run_id)
        return store.revisions(run_id)

    @app.get("/api/runs/{run_id}/artifacts")
    def artifacts(run_id: str):
        get_run_or_404(run_id)
        return store.artifacts(run_id)

    @app.post("/api/runs/{run_id}/reviews", response_model=RunRecord, status_code=201)
    def review(run_id: str, body: RevisionInput):
        get_run_or_404(run_id)
        try:
            return runner.revise(run_id, body, execute=False)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from None

    @app.post(
        "/api/runs/{run_id}/revisions", response_model=RunAccepted, status_code=202
    )
    def revise(run_id: str, body: RevisionInput, background_tasks: BackgroundTasks):
        child = review(run_id, body)
        background_tasks.add_task(execute_background, child.run_id)
        return RunAccepted(run_id=child.run_id, status=child.status)

    @app.post("/api/runs/{run_id}/resume", response_model=RunAccepted, status_code=202)
    def resume(
        run_id: str, background_tasks: BackgroundTasks, body: ResumeInput | None = None
    ):
        record = get_run_or_404(run_id)
        if store.active(run_id):
            raise HTTPException(status_code=409, detail="run is already executing")
        if body and body.model_session_id:
            try:
                runner.attach_model(run_id, sessions.client(body.model_session_id))
            except KeyError:
                raise HTTPException(
                    status_code=404, detail="model session not found"
                ) from None
        background_tasks.add_task(execute_background, run_id)
        return RunAccepted(run_id=run_id, status=record.status)

    # The built web app shares this origin and the same runner/database as CLI.
    # Mount only public build artifacts, never project files or uploaded data.
    web_dir = Path(
        os.getenv(
            "VALUATION_WEB_DIR",
            str(Path(__file__).resolve().parents[3] / "web" / "dist"),
        )
    )
    if (web_dir / "index.html").is_file():
        app.mount("/", StaticFiles(directory=web_dir, html=True), name="web")

    return app


app = create_app()
