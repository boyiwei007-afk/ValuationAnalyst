"""Reproducible local failure/report acceptance. No secrets or paid services."""
import argparse
import json
from datetime import date
from pathlib import Path

from valuationagent.application.research import ResearchService
from valuationagent.application.research_export import build_research_export
from valuationagent.application.runner import ValuationRunner
from valuationagent.application.reporting import ValuationReportExporter
from valuationagent.finance.team_model import FinanceTeamModel
from valuationagent.schemas.models import CompanyInput, FinancialSnapshot, PeerCompany, ValuationRequest
from valuationagent.schemas.research import ResearchDraft, ResearchTurn
from valuationagent.storage.sqlite import SQLiteRunStore


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("var/production-check-20260926"))
    parser.add_argument("--output", type=Path, default=Path("output/acceptance-20260926"))
    args = parser.parse_args()
    store = SQLiteRunStore(args.data_dir)
    service = ResearchService(store)
    session = service.create(data_source_preference="web")
    session.draft = ResearchDraft(company="无数据验收样本（非真实证券）", methods=["dcf", "pe"], industry="电子",
                                  valuation_date=date(2026, 9, 26), objective="零资料场景的软件验收，不代表真实公司估值")
    store.save_research(session)
    state = service.turn(session.session_id, ResearchTurn(content="开始自动化估值"))
    args.output.mkdir(parents=True, exist_ok=True)
    for format in ("pdf", "html", "json"):
        body, _ = build_research_export(service, session.session_id, format)
        path = args.output / f"no-data-outcome.{format}"
        path.write_bytes(body if isinstance(body, bytes) else body.encode("utf-8"))
    pricing_day = date(2026, 9, 26)
    request = ValuationRequest(company=CompanyInput(name="合成算术验收公司", industry="电子"), valuation_date=pricing_day, methods=["pe"],
        financials=FinancialSnapshot(period_end=date(2025, 12, 31), common_shares="100", net_income_parent="200"),
        peers=[PeerCompany(ticker=f"TEST{i}", name=f"合成同业{i}", pe=str(value), as_of_date=pricing_day, multiple_basis="FY") for i, value in enumerate((10, 12, 14, 16, 18))])
    record = ValuationRunner(store, FinanceTeamModel()).run(request)
    assert record.result, record.review
    values = record.result.relative[0]
    assert (str(values.range_low), str(values.per_share_value), str(values.range_high)) == ("24.0000", "28.0000", "32.0000")
    diagnostic = ValuationRunner(store, FinanceTeamModel()).create_run(request)
    body, _, _ = ValuationReportExporter().export(diagnostic, "pdf", store=store)
    (args.output / "unfinished-run-diagnostic.pdf").write_bytes(body)
    acceptance = {"scope": "synthetic deterministic acceptance, not real-company investment analysis", "session_id": session.session_id,
                  "no_data_report": state["result_document"], "run_id": record.run_id,
                  "expected_pe": {"low": "24", "base": "28", "high": "32"}, "actual_pe": values.model_dump(mode="json"),
                  "calculation_checks": record.result.calculation_checks}
    (args.output / "acceptance.json").write_text(json.dumps(acceptance, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"passed": True, "session_id": session.session_id, "run_id": record.run_id,
                      "report": str((args.output / "no-data-outcome.pdf").resolve())}, ensure_ascii=False))


if __name__ == "__main__":
    main()
