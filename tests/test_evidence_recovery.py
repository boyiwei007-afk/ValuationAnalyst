"""Company-independent regressions for real annual-report layouts and recovery.

Names and amounts below are synthetic, not financial data about real issuers.
"""
import json
from datetime import date

import pytest

from valuationagent.application.reporting import ValuationReportExporter
from valuationagent.application.research import (
    CandidateInput,
    ProposeFacts,
    ResearchService,
    _explicit_fact_acceptance,
    _valuation_finish_violation,
)
from valuationagent.application.research_valuation import (
    METRIC_ALIASES,
    normalize_financial_metric,
)
from valuationagent.application.runner import ValuationRunner
from valuationagent.core.evidence import bind_evidence, evidence_context, numeric_tokens
from valuationagent.finance.team_model import FinanceTeamModel
from valuationagent.schemas.models import required_financial_metrics
from valuationagent.schemas.research import (
    DocumentSummary,
    FactCandidate,
    ResearchDraft,
    ResearchTurn,
)
from valuationagent.search.providers import MockSearchProvider
from valuationagent.storage.sqlite import SQLiteRunStore


def draft(name="样本制造公司", ticker="600123"):
    return ResearchDraft(company=name, ticker=ticker, industry="电子", methods=["dcf"], valuation_date=date(2026, 9, 24))


def block(text, index=1, **location):
    return {"block_id": f"file_test:{index}", "file_id": "file_test", "text": text, "location": location}


def item(metric="营业收入", value="100", **changes):
    return CandidateInput.model_validate({"metric": metric, "raw_value": value, "unit": "万元", "period": "2025",
        "scope": "consolidated", "block_id": "file_test:1", "quote": f"{metric} {value}", **changes})


def bind(candidate, source, sources=None, company=None):
    sources = sources or [source]
    aliases = METRIC_ALIASES.get(normalize_financial_metric(candidate.metric), ())
    return bind_evidence(candidate, source, evidence_context(source, sources, candidate.context_block_ids),
                         company or draft(), aliases, identity_text="\n".join(b["text"] for b in sources[:2]))


@pytest.mark.parametrize("name,ticker,separator", [
    ("样本制造公司", "600123", "  "), ("样本消费公司", "000321", " | "),
    ("样本医药公司", "300123", "\t"), ("样本新能源公司", "688123", "      "),
])
def test_exact_source_metric_wins_over_similar_rows_for_any_company(name, ticker, separator):
    rows = [separator.join(row) for row in [
        ["项目", "附注", "2025年度", "2024年度"],
        ["一、营业总收入", "", "110", "95"],
        ["其中：营业收入", "44", "100", "90"],
        ["利息支出", "45", "30", "20"],
        ["其中：利息费用", "", "3", "2"],
    ]]
    source = block(f"{name} {ticker}\n合并利润表\n单位：万元\n" + "\n".join(rows))
    for metric, value, row in [("营业收入", "100", rows[2]), ("利息费用", "3", rows[4])]:
        warnings, checks = bind(item(metric, value, quote=row), source, company=draft(name, ticker))
        assert not warnings, warnings
        assert checks["year_column"] == 2025
        assert checks["source_row"] == row
    assert bind(item(quote=rows[2]), source, company=draft(name, ticker))[1]["note_column_excluded"]


def test_revenue_never_aliases_a_different_statement_concept():
    source = block("样本制造公司600123 合并报表\n单位：万元\n项目 2025年 2024年\n营业总收入 110 95\n营业收入 100 90")
    assert not bind(item("revenue", quote=source["text"]), source)[0]
    assert not bind(item("revenue", quote="营业收入 100 90"), source)[0]
    assert bind(item("revenue", value="110", quote="营业总收入 110 95"), source)[0]
    assert normalize_financial_metric("营业总收入") == "total_revenue"
    assert normalize_financial_metric("主营业务收入") == "main_business_revenue"


def test_distinct_revenue_lines_do_not_create_an_artificial_confirmation_conflict(tmp_path):
    service, session, _ = configured_service(tmp_path)
    session.draft.methods = ["ps"]
    session.facts = [FactCandidate(**item(metric, str(value), unit=unit).model_dump(),
                                  fact_id=f"f_{index}", status="confirmed", normalized_value=str(value))
                     for index, (metric, value, unit) in enumerate([
                         ("营业收入", 100, "元"), ("营业总收入", 110, "元"),
                         ("主营业务收入", 90, "元"), ("普通股股数", 10, "股")])]
    snapshot = service.valuation_assembler._structured_financials(session)[0]
    assert snapshot.revenue == 100
    assert snapshot.statement_items["total_revenue"] == 110
    assert snapshot.statement_items["main_business_revenue"] == 90


@pytest.mark.parametrize("headers,values,year,value", [
    ("2025年度 2024年度", "100 90", "2025", "100"),
    ("2024年 2025年", "90 100", "2025", "100"),
    ("2025年 2024年 本年比上年增减(%) 2023年", "100 90 11.11 80", "2023", "80"),
    ("2025年 2024年 本年比上年增减(%) 2023年", "100 90 11.11% 80", "2023", "80"),
])
def test_explicit_year_columns_and_comparison_columns(headers, values, year, value):
    source = block(f"样本制造公司600123 合并报表\n单位：万元\n项目 附注 {headers}\n营业收入 7 {values}")
    candidate = item(value=value, period=year, quote=f"营业收入 7 {values}")
    assert not bind(candidate, source)[0]
    wrong = candidate.model_copy(update={"period": "2024"})
    assert any("年度列冲突" in w for w in bind(wrong, source)[0])


def test_missing_column_does_not_shift_a_value_into_a_different_year():
    source = block("样本制造公司600123 合并报表\n单位：万元\n项目 2025年 2024年\n营业收入     90")
    assert bind(item(value="90", quote="营业收入     90"), source)[0]


def test_report_year_alone_does_not_identify_multiple_amount_columns():
    source = block("样本制造公司600123 2025年合并报表\n单位：万元\n项目 期初余额 期末余额\n营业收入 100 90")
    assert any("不能默认首列" in w for w in bind(item(), source)[0])


def test_metric_name_cannot_match_a_different_longer_concept():
    source = block("样本制造公司600123 合并报表\n单位：万元\n项目 2025年 2024年\n其他业务营业收入 100 90")
    assert bind(item(), source)[0]


def test_amounts_resembling_years_are_not_used_as_headers():
    source = block("样本制造公司600123 合并报表\n单位：万元\n项目 2024年 2025年\n营业成本 2025 2024\n营业收入 100 90")
    assert any("年度列冲突" in w for w in bind(item(quote="营业收入 100 90"), source)[0])


def test_long_worksheet_retains_its_own_headers():
    sources = [block("样本制造公司600123 合并报表", 1, sheet="合并"),
               block("单位：万元", 2, sheet="合并"),
               block("项目 2025年 2024年", 3, sheet="合并")]
    sources += [block(f"其他科目{i} 10 9", i, sheet="合并") for i in range(4, 30)]
    sources.append(block("营业收入 100 90", 30, sheet="合并"))
    assert not bind(item(block_id=sources[-1]["block_id"], quote=sources[-1]["text"]), sources[-1], sources)[0]


def test_adjacent_negative_pdf_amounts_keep_their_sign():
    assert list(map(str, numeric_tokens("-815.24-1470.21"))) == ["-815.24", "-1470.21"]


def test_concatenated_pdf_money_columns_are_split_without_guessing_decimals():
    text = "29,444,936,771.4130,303,850,168.56"
    assert list(map(str, numeric_tokens(text))) == ["29444936771.41", "30303850168.56"]
    assert list(map(str, numeric_tokens("比率 0.123456"))) == ["0.123456"]


def test_wrapped_statement_label_and_concatenated_columns_bind_to_the_requested_year():
    row = "购建固定资产、无形资产和其3,127,594,916.414,678,712,053.56\n他长期资产支付的现金"
    source = block(
        "样本制造公司600123\n合并现金流量表\n单位：元\n项目 2025年 2024年\n" + row
    )
    candidate = item(
        "购建固定资产、无形资产和其他长期资产支付的现金",
        "3,127,594,916.41",
        unit="元",
        quote=row,
    )
    warnings, checks = bind(candidate, source)
    assert not warnings, warnings
    assert checks["year_column"] == 2025
    assert checks["source_row"].startswith("购建固定资产、无形资产和其")
    assert checks["source_row"].endswith("他长期资产支付的现金")


@pytest.mark.parametrize("answer,question,options,reason", [
    ("DCF基期已补全，预测假设已提出。", "确认整套估值方案后进入正式计算", ["确认方案并计算", "修改"], "齐备"),
    ("仍缺资本开支。", "如何继续？", ["接受缺失项并输出有限DCF", "继续查找"], "DCF"),
    ("DCF所需基期与预测输入已提取齐备。", "请选择如何处理股数", ["提供公告", "先结束"], "齐备"),
    ("股数未通过来源校验。", "请选择如何处理", ["剔除依赖股数的口径，按现有基期与预测提交整套估值方案", "补充资料"], "确认卡"),
    ("股数未通过来源校验。", "请选择如何处理", ["不需股数即可完成DCF估值区间与敏感性", "补充资料"], "必需股数"),
    ("股数未通过来源校验。", "请选择如何处理", ["采信普通股股数为123,456,789股（面值1元/股）", "补充资料"], "来源"),
    ("仍在准备。", "PE锚定采用哪一天的收盘价？", ["使用目标公司收盘价", "提供总市值"], "PE"),
    (
        "提交仍被同一条件拒绝。",
        "系统仍显示9条候选全部待确认、confirmed_fact_count=0，而字段级确认不在我的工具能力内。请选择下一步：",
        [
            "请你在界面候选卡片上执行确认/批准操作，完成后我立即重新提交并进入确定性计算",
            "只确认7条无警告候选",
            "先不推进估值",
        ],
        "控制器",
    ),
    (
        "本轮读取配额已用尽。",
        "下一轮我应如何收口剩余几项？",
        ["继续修正资本开支并清理needs_repair候选", "直接并入模型", "先推进相对估值"],
        "内部执行",
    ),
    (
        "现有年报有两个完整年度。",
        "是否需要在建模前补取2023年年度报告作为第三个完整年度列？",
        ["使用显式十年预测继续估值", "先补取2023年年报"],
        "内部执行",
    ),
])
def test_model_cannot_impersonate_controller_readiness_or_offer_invalid_valuation_paths(
    answer, question, options, reason
):
    assert reason in _valuation_finish_violation(answer, question, options)
    assert not _valuation_finish_violation(
        "所得税费用的年度列仍无法核对。",
        "请提供所得税费用原表截图，还是允许我改查年报附注？",
        ["提供截图", "改查附注"],
    )


def test_cross_page_header_follows_physical_pages_not_append_order():
    title = block("样本制造公司600123\n合并利润表\n单位：万元\n项目 附注 2025年 2024年", 20, page=8)
    row = block("营业收入 7 100 90", 2, page=9)
    later = block("母公司利润表\n单位：元\n项目 2025年 2024年", 3, page=10)
    warnings, checks = bind(item(block_id=row["block_id"], quote=row["text"]), row, [row, later, title])
    assert not warnings and title["block_id"] in checks["context_block_ids"]


def test_statement_header_can_span_three_consecutive_loaded_pages():
    header = block("样本制造公司600123\n合并资产负债表\n单位：万元\n项目 附注 2025年 2024年", 1, page=8)
    continuation = block("应付账款 7 10 9", 2, page=9)
    row = block("长期借款 8 100 90", 3, page=10)
    warnings, _ = bind(item("长期借款", block_id=row["block_id"], quote=row["text"]), row, [header, continuation, row])
    assert not warnings
    assert bind(item("长期借款", block_id=row["block_id"]), row, [header, row])[0]


def test_parent_statement_cannot_borrow_consolidated_scope_or_unit():
    first = block("样本制造公司600123\n合并利润表\n单位：万元\n项目 2025年 2024年", 1, page=8)
    second = block("母公司利润表\n项目 2025年 2024年\n营业收入 100 90", 2, page=9)
    warnings, _ = bind(item(block_id=second["block_id"], quote="营业收入 100 90"), second, [first, second])
    assert any("口径冲突" in w for w in warnings)
    assert any("单位缺少" in w for w in warnings)


def test_distant_or_future_or_other_sheet_header_is_not_authority():
    for location, wrong_location in [({"page": 9}, {"page": 6}), ({"page": 9}, {"page": 10}),
                                     ({"sheet": "经营"}, {"sheet": "母公司"})]:
        header = block("样本制造公司600123 合并报表\n单位：万元\n项目 2025年 2024年", 1, **wrong_location)
        row = block("营业收入 100 90", 2, **location)
        warnings, _ = bind(item(block_id=row["block_id"], context_block_ids=[header["block_id"]]), row, [header, row])
        assert any("单位缺少" in w for w in warnings)


@pytest.mark.parametrize("period,amount", [("2025", "-100"), ("2024", "-90")])
def test_notes_section_scope_and_relative_columns_with_wrapped_labels(period, amount):
    sources = [
        block("样本制造公司600123 2025年年度报告\n七、合并财务报表项目注释", 1, page=3),
        block("61、现金流量表补充资料\n单位：万元", 2, page=4),
        block("样本制造公司2025年年度报告\n补充资料 本期金额 上期金额\n"
              "经营性应收项目的减少（增加以“－”号    -100    -90\n填列）", 3, page=5),
    ]
    metric = "经营性应收项目的减少（增加以“－”号填列）"
    assert normalize_financial_metric(metric) == "operating_receivables_decrease"
    candidate = item(metric, amount, period=period, block_id=sources[-1]["block_id"], quote=sources[-1]["text"])
    warnings, checks = bind(candidate, sources[-1], sources)
    assert not warnings, warnings
    assert checks["year_column"] == int(period)
    assert checks["scope"] == "consolidated"
    # Removing an intervening page prevents unjustified section inheritance.
    assert any("口径缺少" in w for w in bind(candidate, sources[-1], [sources[0], sources[-1]])[0])


def configured_service(tmp_path, llm=None):
    service = ResearchService(SQLiteRunStore(tmp_path))
    session = service.create(llm=llm)
    session.draft = draft()
    session.data_source_preference = "web"
    session.documents.append(DocumentSummary(file_id="file_test", name="synthetic-annual.txt", role="historical_financials", block_count=1))
    source = block("样本制造公司600123 合并报表\n单位：万元\n项目 2025年 2024年\n营业收入 100 90")
    service.store.save_research_blocks(session.session_id, "file_test", [source])
    service.store.save_research(session)
    return service, session, source


class RepeatingRetrievalModel:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        return {"tool_calls": [{
            "id": str(self.calls),
            "type": "function",
            "function": {
                "name": "search_sources",
                "arguments": json.dumps({
                    "query": f"样本公司 缺失财务字段 正式披露 尝试{self.calls}",
                    "reason": "补齐DCF基期字段",
                    "purpose": "financials",
                }, ensure_ascii=False),
            },
        }]}


def test_missing_search_data_stops_polling_and_offers_one_fallback_decision(tmp_path):
    model = RepeatingRetrievalModel()
    service, session, _ = configured_service(tmp_path, model)
    session.draft.ticker = ""  # Keep the synthetic test off the real official catalogue.
    session.pending_action = "valuation"
    service.search_provider = MockSearchProvider()
    service.store.save_research(session)

    state = service.turn(session.session_id, ResearchTurn(content="继续自动估值"))

    assert model.calls == 4
    question = state["session"]["question"]
    assert question["kind"] == "clarification"
    assert [option["id"] for option in question["options"]] == [
        "report", "upload", "change_methods",
    ]
    assert "不再自动重复搜索" in question["title"]
    assert "结果说明报告" in state["messages"][-1]["content"]
    assert state["result_document"]["status"] == "insufficient_data"


class RepairModel:
    def __init__(self):
        self.calls = []

    def chat(self, messages, **kwargs):
        self.calls.append(messages)
        candidate = item(quote="营业收入 100 90", unit="unknown" if len(self.calls) == 1 else "万元")
        args = {"candidates": [candidate.model_dump()]}
        if len(self.calls) > 1:
            result = json.loads(messages[-1]["content"])
            assert result["status"] == "candidate_evidence_needs_repair"
            args["replaces"] = [result["candidates"][0]["fact_id"]]
        return {"tool_calls": [{"id": str(len(self.calls)), "type": "function", "function":
                {"name": "propose_facts", "arguments": json.dumps(args)}}]}


def test_llm_receives_repair_feedback_and_returns_confirmable_batch_in_same_turn(tmp_path):
    model = RepairModel()
    service, session, _ = configured_service(tmp_path, model)
    state = service.turn(session.session_id, ResearchTurn(content="提取营业收入"))
    assert len(model.calls) == 2
    question = state["session"]["question"]
    assert question["kind"] == "facts" and question["options"][0]["id"] == "accept"
    accepted = service.turn(session.session_id, ResearchTurn(question_id=question["question_id"], option_id="accept"))
    assert [f["status"] for f in accepted["session"]["facts"]] == ["rejected", "confirmed"]


def test_repeat_warned_candidates_are_deduplicated_and_never_present_impossible_confirmation(tmp_path):
    service, session, _ = configured_service(tmp_path)
    for _ in range(5):
        result = service._facts(session, ProposeFacts(candidates=[item(quote="营业收入 100 90", unit="unknown")]))
        assert result["status"] == "candidate_evidence_needs_repair"
        assert not result.get("_terminal") and session.question is None
    assert len(session.facts) == 1 and session.facts[0].status == "proposed"


def test_skip_failed_evidence_really_quarantines_the_candidate(tmp_path):
    service, session, _ = configured_service(tmp_path)
    failed = FactCandidate(
        **item(unit="unknown").model_dump(),
        fact_id="failed_income",
        warnings=["单位待确认"],
    )
    session.facts.append(failed)
    service._recover(
        session,
        ValueError("EVIDENCE_REPAIR_EXHAUSTED: 无法可靠确认营业收入"),
        "document",
        context={"candidate_ids": [failed.fact_id]},
    )
    acknowledgement = service._answer_question(
        session,
        ResearchTurn(question_id=session.question.question_id, option_id="defer"),
    )
    assert failed.status == "rejected"
    assert session.last_issue.status == "resolved"
    assert "已隔离 1 个" in acknowledgement
    assert any(event.type == "facts.quarantined" for event in service.store.list_events(session.session_id))


class ExhaustedEvidenceModel:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= 3:
            candidate = item(unit="unknown")
            return {"tool_calls": [{"id": str(self.calls), "type": "function", "function": {
                "name": "propose_facts",
                "arguments": json.dumps({"candidates": [candidate.model_dump(mode="json")]}, ensure_ascii=False),
            }}]}
        return {"tool_calls": [{"id": "ask", "type": "function", "function": {
            "name": "finish_response",
            "arguments": json.dumps({
                "answer": "当前引文无法可靠确认营业收入，失败候选已隔离。",
                "question": "你希望上传包含营业收入表头的截图，还是允许改查另一份正式披露？",
                "options": ["上传表头截图", "改查另一份正式披露"],
            }, ensure_ascii=False),
        }}]}


def test_repeated_bad_evidence_is_quarantined_without_blocking_the_whole_valuation(tmp_path):
    model = ExhaustedEvidenceModel()
    service, session, _ = configured_service(tmp_path, model)
    state = service.turn(session.session_id, ResearchTurn(content="开始估值"))
    assert model.calls == 4
    assert state["session"]["question"]["kind"] == "clarification"
    assert state["session"]["facts"][0]["status"] == "rejected"
    assert state["session"]["last_issue"] is None
    assert any(event["type"] == "facts.quarantined" for event in state["events"])


class RetrievalBudgetProgressModel:
    def __init__(self):
        self.calls = 0
        self.post_refresh_remaining = None

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls <= 6 or self.calls == 8:
            name = "read_document"
            arguments = {"file_id": "file_test", "query": "营业收入", "limit": 1}
        elif self.calls == 7:
            name = "propose_facts"
            arguments = {
                "candidates": [
                    item(quote="营业收入 100 90").model_dump(mode="json")
                ]
            }
        else:
            read_result = json.loads(messages[-1]["content"])
            assert not read_result.get("retrieval_budget_exhausted")
            self.post_refresh_remaining = read_result["retrieval_budget_remaining"]
            name = "finish_response"
            arguments = {
                "answer": "现有文件没有可核验的资本开支原表，DCF仍缺必要输入。",
                "question": "请上传包含资本开支的正式报表，还是将本次方法改为仅做PE？",
                "options": ["上传正式报表", "改为仅做PE"],
            }
        return {"tool_calls": [{
            "id": str(self.calls),
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }]}


def test_new_clean_evidence_refreshes_targeted_reads_without_user_continue_turn(tmp_path):
    model = RetrievalBudgetProgressModel()
    service, session, _ = configured_service(tmp_path, model)
    state = service.turn(session.session_id, ResearchTurn(content="开始估值"))
    assert model.calls == 9
    assert model.post_refresh_remaining == 5
    assert state["session"]["facts"][0]["status"] == "proposed"
    assert state["session"]["question"]["kind"] == "clarification"


class PendingValuationPartialBatchModel:
    def __init__(self):
        self.calls = 0
        self.proposal_status = ""

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            name = "propose_facts"
            arguments = {
                "candidates": [
                    item(quote="营业收入 100 90").model_dump(mode="json")
                ]
            }
        else:
            self.proposal_status = json.loads(messages[-1]["content"])["status"]
            name = "finish_response"
            arguments = {
                "answer": "营业收入候选已由工具暂存；现有资料仍缺资本开支原表。",
                "question": "请上传包含资本开支的正式报表，还是将本次方法改为仅做PE？",
                "options": ["上传正式报表", "改为仅做PE"],
            }
        return {"tool_calls": [{
            "id": str(self.calls),
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(arguments, ensure_ascii=False)},
        }]}


def test_pending_valuation_persists_valid_partial_batch_from_option_wording(tmp_path):
    model = PendingValuationPartialBatchModel()
    service, session, _ = configured_service(tmp_path, model)
    session.pending_action = "valuation"
    service.store.save_research(session)
    state = service.turn(
        session.session_id,
        ResearchTurn(content="请补营业收入和资本开支并继续估值"),
    )
    assert model.proposal_status == "valuation_inputs_staged"
    assert any(
        fact["metric"] == "营业收入" and not fact["warnings"]
        for fact in state["session"]["facts"]
    )


class FalseReadyModel:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            payload = {
                "answer": "DCF基期已补全，预测假设也已齐备。",
                "question": "请确认整套估值方案后进入正式计算。",
                "options": ["确认方案并计算", "修改方案"],
            }
        else:
            assert "VALUATION_STATE_MISMATCH" in messages[-1]["content"]
            payload = {
                "answer": "系统准备检查尚未通过，当前没有已确认的完整财务基期。",
                "question": "你希望上传年报，还是允许我继续检索正式披露？",
                "options": ["上传年报", "继续检索正式披露"],
            }
        return {"tool_calls": [{"id": str(self.calls), "type": "function", "function": {
            "name": "finish_response",
            "arguments": json.dumps(payload, ensure_ascii=False),
        }}]}


def test_false_ready_narrative_cannot_create_a_fake_plan_confirmation(tmp_path):
    model = FalseReadyModel()
    service, session, _ = configured_service(tmp_path, model)
    state = service.turn(session.session_id, ResearchTurn(content="开始估值"))
    assert model.calls == 2
    question = state["session"]["question"]
    assert question is None
    assert state["result_document"]["status"] == "insufficient_data"
    assert "无需确认继续多搜" in state["messages"][-1]["content"]
    assert "基期已补全" not in state["messages"][-1]["content"]


class FalseStagingModel:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            answer = "本轮已找到折旧与营运资本数据，并已完成来源校验；相关字段已核验并暂存。"
        else:
            assert "本轮没有新的无警告候选写入会话" in messages[-1]["content"]
            answer = "当前原文已经定位，但候选尚未通过工具写入，不能视为已暂存。"
        return {"tool_calls": [{
            "id": str(self.calls),
            "type": "function",
            "function": {
                "name": "finish_response",
                "arguments": json.dumps({
                    "answer": answer,
                    "question": "请上传包含资本开支的正式报表，还是将本次方法改为仅做PE？",
                    "options": ["上传正式报表", "改为仅做PE"],
                }, ensure_ascii=False),
            },
        }]}


def test_model_cannot_claim_new_staged_facts_when_tool_persisted_none(tmp_path):
    model = FalseStagingModel()
    service, session, _ = configured_service(tmp_path, model)
    state = service.turn(session.session_id, ResearchTurn(content="开始估值"))
    assert model.calls == 2
    assert "已核验并暂存" not in state["messages"][-1]["content"]
    assert state["session"]["question"]["kind"] == "clarification"


class FalseCandidateCountModel:
    def __init__(self):
        self.calls = 0

    def chat(self, messages, **kwargs):
        self.calls += 1
        if self.calls == 1:
            answer = "当前共有候选数据，其中0条需返工。"
        else:
            assert "需修复候选数量与控制器状态不一致" in messages[-1]["content"]
            answer = "当前仍有带警告候选，不会进入估值；资本开支原表仍缺。"
        return {"tool_calls": [{
            "id": str(self.calls),
            "type": "function",
            "function": {
                "name": "finish_response",
                "arguments": json.dumps({
                    "answer": answer,
                    "question": "请上传包含资本开支的正式报表，还是将本次方法改为仅做PE？",
                    "options": ["上传正式报表", "改为仅做PE"],
                }, ensure_ascii=False),
            },
        }]}


def test_model_cannot_report_candidate_counts_different_from_controller(tmp_path):
    model = FalseCandidateCountModel()
    service, session, _ = configured_service(tmp_path, model)
    session.facts.append(FactCandidate(
        **item(unit="unknown").model_dump(),
        fact_id="warned_candidate",
        warnings=["单位待确认"],
    ))
    service.store.save_research(session)
    state = service.turn(session.session_id, ResearchTurn(content="开始估值"))
    assert model.calls == 2
    assert "0条需返工" not in state["messages"][-1]["content"]


@pytest.mark.parametrize("content,accepts", [
    ("直接确认开始估值", True), ("确认本批无警告字段", True), ("确认并开始正式估值", True),
    ("不要确认", False), ("确认是什么意思", False), ("确认但是把收入改成200", False),
    ("先不确认，开始估值", False), ("‘直接确认开始估值’为什么没用", False),
])
def test_only_explicit_unconditional_affirmation_accepts_a_visible_card(content, accepts):
    assert _explicit_fact_acceptance(content) is accepts


def test_text_confirmation_accepts_only_clean_visible_facts_and_keeps_original_user_message(tmp_path):
    service, session, _ = configured_service(tmp_path)
    service._facts(session, ProposeFacts(candidates=[item(quote="营业收入 100 90")]))
    service.store.save_research(session)
    state = service.turn(session.session_id, ResearchTurn(content="直接确认开始估值"))
    assert state["session"]["facts"][0]["status"] == "confirmed"
    assert state["session"]["pending_action"] == "valuation"
    assert any(m["role"] == "user" and m["content"] == "直接确认开始估值" for m in state["messages"])
    assert state.get("action") is None  # Real DCF inputs are still missing.


def test_accepting_an_old_clean_card_revalidates_it_instead_of_using_stale_checks(tmp_path):
    service, session, _ = configured_service(tmp_path)
    # Simulate a legacy incorrectly clean value from the other year.
    session.facts.append(FactCandidate(**item(value="90", quote="营业收入 100 90").model_dump(), fact_id="legacy_wrong", normalized_value="900000"))
    service._question(session, "facts", "确认字段", [("accept", "确认"), ("defer", "稍后")], fact_ids=["legacy_wrong"])
    qid = session.question.question_id
    service.store.save_research(session)
    state = service.turn(session.session_id, ResearchTurn(question_id=qid, option_id="accept"))
    assert state["session"]["facts"][0]["status"] == "proposed"
    assert any("年度列冲突" in w for w in state["session"]["facts"][0]["warnings"])


def test_currency_share_capital_is_not_accepted_as_a_share_count(tmp_path):
    service, session, _ = configured_service(tmp_path)
    source = block("样本制造公司600123 2025年合并报表\n单位：万元\n总股本（万元）100")
    service.store.save_research_blocks(session.session_id, "file_test", [source])
    result = service._facts(session, ProposeFacts(candidates=[item("总股本", quote="总股本（万元）100")]))
    assert result["status"] == "candidate_evidence_needs_repair"
    assert any("维度" in w for w in session.facts[0].warnings)


@pytest.mark.parametrize("command", ["开始估值", "继续"])
def test_legacy_all_warning_card_is_revalidated_without_approving_values(tmp_path, command):
    service, session, _source = configured_service(tmp_path)
    old = FactCandidate(**item(quote="营业收入 100 90").model_dump(), fact_id="legacy", warnings=["旧解析器年度列警告"])
    session.facts.append(old)
    session.pending_action = "valuation"
    service._question(session, "facts", "零个可确认", [("defer", "稍后"), ("reject", "拒绝")], fact_ids=[old.fact_id])
    service.store.save_research(session)
    state = service.turn(session.session_id, ResearchTurn(content=command))
    assert state["session"]["facts"][0]["status"] == "proposed"
    assert not state["session"]["facts"][0]["warnings"]
    assert state["session"]["question"] is None  # Old partial cards no longer interrupt modeling.
    assert state["session"]["pending_action"] == "valuation"
    assert state.get("action") is None  # One revenue field is not a calculable model.
    assert any(event["type"] == "facts.revalidated" for event in state["events"])


def test_method_specific_pending_checks_preserve_conflicts_and_ignore_research_only_facts(tmp_path):
    service, session, _ = configured_service(tmp_path)
    session.draft.methods = ["pe"]
    def fact(metric, value="100", **changes):
        return FactCandidate(**{**item(metric, value).model_dump(), "fact_id": metric,
                               "normalized_value": value, **changes})
    session.facts = [fact("net_income_parent", status="confirmed"),
                     fact("财务费用", warnings=["待查"]), fact("revenue", warnings=["待查"])]
    assert not service.valuation_assembler.pending_blockers(session)
    session.facts.append(fact("net_income_parent", "90", fact_id="conflict"))
    assert [f.fact_id for f in service.valuation_assembler.pending_blockers(session)] == ["conflict"]
    session.facts[-1].normalized_value = "100"
    assert not service.valuation_assembler.pending_blockers(session)
    session.facts.append(fact("common_shares", unit="unknown"))
    assert service.valuation_assembler.pending_blockers(session)


def test_four_year_confirmed_dcf_starts_despite_unrelated_pending_note_and_exports_report(tmp_path):
    from test_finance_team_model import history
    service, session, _ = configured_service(tmp_path)
    fields = sorted(required_financial_metrics(["dcf"]))
    sources = []
    for index, row in enumerate(history()[-4:], 1):
        year = str(row.period_end.year)
        units = {key: "ratio" if key in {"ebit_margin", "tax_rate"} else "股" if key == "common_shares" else "元" for key in fields}
        lines = {key: f"{key}（{units[key]}） {getattr(row, key)}" for key in fields}
        source = block(f"样本制造公司600123 {year}年合并报表\n单位：元\n" + "\n".join(lines.values()), index)
        sources.append(source)
        service.store.save_research_blocks(session.session_id, "file_test", sources)
        candidates = [item(key, str(getattr(row, key)), period=year, unit=units[key],
                           block_id=source["block_id"], quote=lines[key]) for key in fields]
        service._facts(session, ProposeFacts(candidates=candidates))
        assert not any(f.warnings for f in session.facts), [(f.metric, f.warnings) for f in session.facts if f.warnings]
        service._answer_question(session, ResearchTurn(question_id=session.question.question_id, option_id="accept"))
    session.facts.append(FactCandidate(**item("财务费用", "9").model_dump(), fact_id="unused", warnings=["缺少原文"] ))
    service.store.save_research(session)
    state = service.turn(session.session_id, ResearchTurn(content="开始估值"))
    assert state["action"] == {"type": "submit_valuation"}, state["session"]["gaps"]
    request = service.valuation_assembler.build(service.store.get_research(session.session_id))
    record = ValuationRunner(service.store, FinanceTeamModel()).run(request)
    assert record.result and record.result.dcf and record.result.sensitivity
    assert ValuationReportExporter().xlsx(record).startswith(b"PK")
