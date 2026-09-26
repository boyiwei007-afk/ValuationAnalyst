"""Opt-in, bounded live LLM + public-source check with zero supplied files.

Stops at the combined review; never auto-confirms real-company financial facts.
Credentials are requested with echo disabled and are never written to disk.
"""
import argparse
import getpass
import json
import time
from datetime import date
from pathlib import Path

from valuationagent.application.research import ResearchService
from valuationagent.application.research_export import build_research_export
from valuationagent.llm.client import OpenAICompatibleClient
from valuationagent.schemas.models import ModelConnectionInput
from valuationagent.schemas.research import ResearchDraft, ResearchTurn
from valuationagent.search.providers import TavilySearchProvider
from valuationagent.storage.sqlite import SQLiteRunStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("var/live-no-upload-20260926"))
    parser.add_argument("--budget", type=int, default=240)
    args = parser.parse_args()
    if not args.live:
        parser.error("Explicit --live is required because this calls paid model/search services")
    model_key = getpass.getpass("Model key (hidden): ")
    search_key = getpass.getpass("Tavily key (hidden, optional): ")
    llm = OpenAICompatibleClient(ModelConnectionInput(base_url="https://api.deepseek.com", model="deepseek-flash",
                                                     api_key=model_key, timeout_seconds=45))
    service = ResearchService(SQLiteRunStore(args.output), search_provider=TavilySearchProvider(search_key) if search_key else None)
    session = service.create(llm=llm, data_source_preference="web")
    session.draft = ResearchDraft(company="贵州茅台", ticker="600519.SH", industry="食品饮料 / 白酒",
        methods=["dcf", "pe"], valuation_date=date(2025, 6, 30), objective="零上传公开资料研究验收；未获用户确认不得提交真实数值估值")
    service.store.save_research(session)
    started = time.monotonic()
    state = service.turn(session.session_id, ResearchTurn(content="开始自动化估值，使用公开正式披露自动补齐必要输入。先聚焦2024年度基期，历史不足可提出有据可查的预测假设。缺数时交付说明报告，已有方法能算就进入最终方案确认。", time_budget_seconds=args.budget))
    for format in ("html", "json", "pdf"):
        body, _ = build_research_export(service, session.session_id, format)
        (args.output / f"outcome.{format}").write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    events = service.store.list_events(session.session_id)
    summary = {"session_id": session.session_id, "elapsed_seconds": round(time.monotonic() - started, 2),
               "prompt_version": state["session"]["prompt_version"],
               "documents": len(state["session"]["documents"]), "facts": len(state["session"]["facts"]),
               "confirmed": sum(f["status"] == "confirmed" for f in state["session"]["facts"]),
               "searches": len(state["session"]["search_history"]),
               "question_kind": (state["session"].get("question") or {}).get("kind"),
               "issue": (state["session"].get("last_issue") or {}).get("code"),
               "outcome": state["result_document"]["status"] if state.get("result_document") else "report_missing",
               "tool_calls": sum(e.type == "tool.started" for e in events),
               "scope": "Real public-source acquisition; no automatic confirmation or investment conclusion"}
    (args.output / "acceptance.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
