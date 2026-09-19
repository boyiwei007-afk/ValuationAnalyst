from fastapi import HTTPException
from fastapi.responses import Response
from valuationagent.application.research_export import build_research_export
from valuationagent.schemas.models import ResumeInput
from valuationagent.schemas.research import ResearchCreate, ResearchTurn


def register_research_routes(app, research, sessions):
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
        session = research.create(body.language, get_client(body.model_session_id) if body.model_session_id else None)
        return research.snapshot(session.session_id)

    @app.get("/api/research-sessions")
    def list_sessions():
        return [{"session_id": s.session_id, "draft": s.draft, "language": s.language,
                 "updated_at": s.updated_at, "status": s.status, "revision": s.revision}
                for s in research.store.list_research()]

    @app.get("/api/research-sessions/{session_id}")
    def snapshot(session_id: str):
        get_session(session_id)
        return research.snapshot(session_id)

    @app.post("/api/research-sessions/{session_id}/messages")
    def turn(session_id: str, body: ResearchTurn):
        get_session(session_id)
        try:
            return research.turn(session_id, body)
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
