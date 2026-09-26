"""One-plan real-LLM upload -> extraction -> synthetic PE -> report check.

Only this generated synthetic fixture is automatically approved. No real-company
facts or assumptions are ever automatically approved by this acceptance script.
"""
import argparse
import getpass
import json
import time
from datetime import date
from pathlib import Path

from valuationagent.application.research import ResearchService
from valuationagent.application.research_export import build_research_export
from valuationagent.application.reporting import ValuationReportExporter
from valuationagent.application.runner import ValuationRunner
from valuationagent.application.reproducibility import build_valuation_bundle, replay_bundle
from valuationagent.finance.team_model import FinanceTeamModel
from valuationagent.llm.client import OpenAICompatibleClient
from valuationagent.schemas.models import ModelConnectionInput
from valuationagent.schemas.research import ResearchDraft, ResearchTurn
from valuationagent.storage.sqlite import SQLiteRunStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=Path("var/live-upload-20260926"))
    args = parser.parse_args()
    if not args.live:
        parser.error("--live required: this calls a paid model service")
    llm = OpenAICompatibleClient(ModelConnectionInput(base_url="https://api.deepseek.com", model="deepseek-flash", api_key=getpass.getpass("Model key (hidden): "), timeout_seconds=45))
    store = SQLiteRunStore(args.output)
    service = ResearchService(store)
    session = service.create(llm=llm, data_source_preference="upload")
    session.draft = ResearchDraft(company="验收科技", ticker="TEST000", industry="电子", methods=["pe"], valuation_date=date(2026, 6, 30), objective="纯合成软件验收，非真实证券")
    store.save_research(session)
    material = "合成软件验收资料，非真实证券。\n验收科技 TEST000 2025年合并报表，单位：元。\n归母净利润：200元。\n普通股股数：100股。\n以下均为同类电子行业业务的合成同业，2026-06-30定价，年度FY静态市盈率，单位：倍。\n"
    material += "\n".join(f"验收同业{label} TEST00{i}，2026-06-30，FY，市盈率{value}倍。" for i, (label, value) in enumerate(zip("甲乙丙丁戊", (10, 12, 14, 16, 18)), 1))
    meta = store.save_upload("合成估值验收.txt", "historical_financials", "text/plain", material.encode())
    started = time.monotonic()
    state = service.turn(session.session_id, ResearchTurn(content="开始自动化估值。仅依据附件提取目标公司归母净利润、普通股股数及五家同业的FY市盈率，完成PE区间、敏感性和报告。通过核验后集中确认整套方案，不逐批询问、不联网。", file_ids=[meta["file_id"]], time_budget_seconds=180))
    question = state["session"].get("question") or {}
    accepted = bool(question.get("valuation_review"))
    record = None
    if accepted:
        state = service.turn(session.session_id, ResearchTurn(question_id=question["question_id"], option_id="accept"))
        if state.get("action", {}).get("type") == "submit_valuation":
            runner = ValuationRunner(store, FinanceTeamModel())
            record = service.submit_valuation(session.session_id, runner)
            record = runner.execute(record.run_id)
    summary = {"scope": "synthetic input; real LLM extraction and tools; not investment research", "session_id": session.session_id,
               "elapsed_seconds": round(time.monotonic() - started, 2), "one_combined_review": accepted,
               "completed": bool(record and record.result), "searches": len(state["session"]["search_history"])}
    if record and record.result:
        result = record.result.relative[0]
        assert [str(result.range_low), str(result.per_share_value), str(result.range_high)] == ["24.0000", "28.0000", "32.0000"]
        summary.update(run_id=record.run_id, prices=[str(result.range_low), str(result.per_share_value), str(result.range_high)], replay=replay_bundle(build_valuation_bundle(store, record)))
        for format in ("pdf", "xlsx", "json"):
            content, _, _ = ValuationReportExporter().export(record, format, store=store)
            (args.output / f"synthetic-valuation.{format}").write_bytes(content)
    for format in ("html", "pdf", "json"):
        content, _ = build_research_export(service, session.session_id, format)
        (args.output / f"outcome.{format}").write_bytes(content if isinstance(content, bytes) else content.encode())
    (args.output / "acceptance.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False))
    return 0 if summary["completed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
