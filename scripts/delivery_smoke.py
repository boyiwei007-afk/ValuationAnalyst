"""Bounded opt-in acceptance against live services; credentials stay in memory.

Run in economic_agent. No key is printed or written. Synthetic valuation is
separate from the real BYD source-location probe; never treat it as BYD value.
"""
import argparse
import json
import os
import sys
import time
from datetime import date
from pathlib import Path

from valuationagent.application.research import ResearchService, SearchSources, FetchSearchSource
from valuationagent.application.reproducibility import build_valuation_bundle, replay_bundle
from valuationagent.application.reporting import ValuationReportExporter
from valuationagent.application.runner import ValuationRunner
from valuationagent.finance.team_model import FinanceTeamModel
from valuationagent.llm.client import OpenAICompatibleClient
from valuationagent.schemas.models import ModelConnectionInput
from valuationagent.schemas.research import ResearchDraft, ResearchTurn
from valuationagent.search.providers import TavilySearchProvider, CninfoAnnouncementProvider
from valuationagent.schemas.agent import SearchQuery
from valuationagent.storage.sqlite import SQLiteRunStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--live", action="store_true", help="Call configured LLM and search services")
    parser.add_argument("--output", type=Path, default=Path("var/delivery-live"))
    args = parser.parse_args()
    if not args.live:
        parser.error("Requires explicit --live; use pytest for offline regressions")
    sys.stdout.reconfigure(encoding="utf-8")
    args.output.mkdir(parents=True, exist_ok=True)
    store = SQLiteRunStore(args.output)
    client = OpenAICompatibleClient(ModelConnectionInput(provider="openai_compatible",
        base_url=os.environ.get("VALUATION_LLM_BASE_URL", "https://api.deepseek.com"),
        model=os.environ.get("VALUATION_LLM_MODEL", "deepseek-flash"),
        api_key=os.environ["VALUATION_LLM_API_KEY"], timeout_seconds=60))
    service = ResearchService(store)
    session = service.create(llm=client)
    session.draft = ResearchDraft(company="验收科技", ticker="TEST000", industry="电子",
        valuation_date=date(2026, 6, 30), methods=["pe"], objective="合成数据的软件验收，不代表真实证券价值")
    session.data_source_preference = "upload"
    store.save_research(session)
    text = """合成验收资料，不是真实企业或投资建议。
验收科技 TEST000 2025年合并报表，单位：元。
归母净利润：200元。
普通股股数：100股。
以下为合成电子行业可比公司，同类产品业务，均为2026-06-30定价的年度FY静态市盈率，单位：倍。
验收同业甲 TEST001，2026-06-30，FY，市盈率10倍。
验收同业乙 TEST002，2026-06-30，FY，市盈率12倍。
验收同业丙 TEST003，2026-06-30，FY，市盈率14倍。
验收同业丁 TEST004，2026-06-30，FY，市盈率16倍。
验收同业戊 TEST005，2026-06-30，FY，市盈率18倍。
"""
    meta = store.save_upload("合成验收-目标与同业.txt", "historical_financials", "text/plain", text.encode())
    turn = ResearchTurn(content="请只依据上传的合成验收资料提取目标公司归母净利润、普通股股数，以及5家同业的年度FY市盈率。可比倍数用role=comparable。所有字段保留证据，按候选确认流程，不自行计算或联网。",
                        file_ids=[meta["file_id"]], time_budget_seconds=180)
    started = time.perf_counter()
    for index in range(10):
        result = service.turn(session.session_id, turn)
        saved = store.get_research(session.session_id)
        print(json.dumps({"turn": index + 1, "status": saved.status, "facts": len(saved.facts),
            "warnings": [w for f in saved.facts if f.status == "proposed" for w in f.warnings],
            "question": saved.question.kind if saved.question else None,
            "issue": saved.last_issue.code if saved.last_issue else None}, ensure_ascii=False), flush=True)
        if saved.question and saved.question.kind == "facts":
            # Safe automated acceptance ONLY for this script's synthetic fixture.
            clean = [f for f in saved.facts if f.status == "proposed" and not f.warnings]
            if not clean:
                turn = ResearchTurn(content="请用原文表头补齐当前警告候选的主体、单位、期间和口径证据，使用replaces替换，不能删除警告绕过校验。", time_budget_seconds=180)
            else:
                turn = ResearchTurn(question_id=saved.question.question_id, option_id="accept")
        elif len([f for f in saved.facts if f.status == "confirmed"]) >= 7:
            break
        elif saved.last_issue and saved.last_issue.status == "open":
            break
        else:
            turn = ResearchTurn(content="继续补齐尚未确认的目标归母净利润、普通股股数和5家同业FY市盈率，共7个字段；不重复已确认字段。quote只复制科目连续原文行，表头用context_block_ids。", time_budget_seconds=180)
    saved = store.get_research(session.session_id)
    report = {"session_id": saved.session_id, "model": client.config.model,
              "seconds": round(time.perf_counter() - started, 2), "fixture": "synthetic", "llm_to_report": False}
    if len([f for f in saved.facts if f.status == "confirmed"]) >= 7 and not saved.question:
        request = service.valuation_assembler.build(saved)
        runner = ValuationRunner(store, FinanceTeamModel())
        record = runner.run(request)
        if record.result:
            saved.valuation_run_id = record.run_id
            store.save_research(saved)
            exporter = ValuationReportExporter()
            for kind in ("json", "xlsx", "pdf"):
                content, _, _ = exporter.export(record, kind, store=store)
                (args.output / f"synthetic-report.{kind}").write_bytes(content)
            report.update(llm_to_report=True, run_id=record.run_id, status=str(record.status),
                          relative=record.result.relative[0].model_dump(mode="json"),
                          replay=replay_bundle(build_valuation_bundle(store, record)))
    # Separate read-only online discovery; no guessed numbers enter valuation.
    search_query = SearchQuery(query="比亚迪 002594 2024年年度报告", ticker="002594.SZ", company_name="比亚迪",
                              purpose="financials", as_of_date=date(2025, 6, 30), candidate_limit=3)
    official = CninfoAnnouncementProvider().search_annual_reports("002594.SZ", [2024], cutoff=date(2025, 6, 30), company_name="比亚迪")
    report["official_search"] = {"status": official.status, "hits": [h.model_dump(mode="json") for h in official.hits], "error": official.error_code}
    if os.environ.get("TAVILY_API_KEY"):
        tavily = TavilySearchProvider(os.environ["TAVILY_API_KEY"]).search(search_query)
        report["tavily"] = {"status": tavily.status, "hits": len(tavily.hits), "error": tavily.error_code}
    (args.output / "acceptance.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if report["llm_to_report"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
