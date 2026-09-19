"""Shared, finance-independent research conversations for CLI and Web."""
import json
import re
import threading
import time
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from pydantic import Field
from valuationagent.core.documents import parse_document
from valuationagent.core.tools import NoArguments, ToolRegistry, ToolSpec, canonical
from valuationagent.llm.agent import run_tool_loop
from valuationagent.llm.client import LlmError
from valuationagent.llm.context import research_context
from valuationagent.llm.intent import interpret_intent
from valuationagent.schemas.models import ApiModel
from valuationagent.schemas.research import (
    DocumentSummary, FactCandidate, ResearchChoice, ResearchDraft,
    ResearchQuestion, ResearchSession, ResearchTurn,
)


class ReadDocument(ApiModel):
    file_id: str
    query: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6, ge=1, le=8)


class ProposeTask(ApiModel):
    draft: ResearchDraft


class CandidateInput(ApiModel):
    metric: str = Field(min_length=1, max_length=120)
    raw_value: str = Field(min_length=1, max_length=100)
    unit: str = "unknown"
    period: str = "unknown"
    scope: str = "unknown"
    role: str = "historical"
    block_id: str
    quote: str = Field(min_length=1, max_length=2400)


class ProposeFacts(ApiModel):
    candidates: list[CandidateInput] = Field(min_length=1, max_length=20)
    missing: list[str] = Field(default_factory=list, max_length=30)
    replaces: list[str] = Field(default_factory=list, max_length=20)


class FinishResponse(ApiModel):
    answer: str = Field(min_length=1, max_length=7000)
    evidence_ids: list[str] = Field(default_factory=list, max_length=12)
    question: str = Field(default="", max_length=600)
    options: list[str] = Field(default_factory=list, max_length=3)


class SearchSources(ApiModel):
    query: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)


class UpdateGaps(ApiModel):
    missing: list[str] = Field(default_factory=list, max_length=30)
    reason: str = Field(min_length=1, max_length=1000)


def _id(prefix):
    return prefix + uuid.uuid4().hex


def _text(session, zh, en):
    return en if session.language == "en-US" else zh


class ResearchService:
    def __init__(self, store):
        self.store = store
        self._clients = {}

    def create(self, language="zh-CN", llm=None):
        session = ResearchSession(session_id=_id("research_"), language=language, requires_model=llm is not None)
        self.store.create_research(session)
        if llm is not None:
            self._clients[session.session_id] = llm
        self._say(session, _text(session,
            "欢迎来到 ValuationAgent。可以先说公司和研究目标，或上传年报、财务表和政策资料。我会整理来源和缺口，需要确认时给你选项，也可以直接补充文字。",
            "Welcome to ValuationAgent. Describe a company and your goal, or upload reports, spreadsheets and policy material. I will organize sources and gaps, with choices and free-text clarification when needed."))
        return session

    def attach(self, session_id, llm):
        if self.store.active(session_id):
            raise ValueError("当前会话正在处理请求，请稍后连接。")
        session = self.store.get_research(session_id)
        self._clients[session_id] = llm
        session.requires_model = True
        self.store.save_research(session)

    def snapshot(self, session_id):
        session = self.store.get_research(session_id)
        return {"session": session.model_dump(mode="json"),
                "messages": [m.model_dump(mode="json") for m in self.store.list_messages(session_id)],
                "events": [e.model_dump(mode="json") for e in self.store.list_events(session_id)]}

    def _say(self, session, content):
        self.store.add_message(session.session_id, "assistant", content, "research")
        self.store.append_event(session.session_id, type="conversation.message", stage="research", summary=content, status="completed")

    def _tool(self, session, name, args, fn):
        call_id = _id("call_")
        started = time.monotonic()
        self.store.append_event(session.session_id, type="tool.started", stage="research", tool=name,
                                tool_call_id=call_id, status="running", summary=name,
                                payload={"arguments": args})
        try:
            result = fn()
        except Exception as exc:
            message = str(exc) if isinstance(exc, (ValueError, LlmError)) else "文件或工具处理失败，请检查资料后重试。"
            self.store.append_event(session.session_id, type="tool.failed", stage="research", tool=name,
                tool_call_id=call_id, status="failed", summary=message,
                duration_ms=int((time.monotonic() - started) * 1000))
            raise
        self.store.append_event(session.session_id, type="tool.completed", stage="research", tool=name,
            tool_call_id=call_id, status="completed", summary=name,
            duration_ms=int((time.monotonic() - started) * 1000), payload={"output": result})
        self.store.save_research(session)
        return result

    def _question(self, session, kind, title, choices, **kwargs):
        session.question = ResearchQuestion(question_id=_id("question_"), kind=kind, title=title,
            options=[ResearchChoice(id=key, label=label) for key, label in choices], **kwargs)
        session.status = "waiting_confirmation"
        self.store.append_event(session.session_id, type="review.required", stage="research", status="waiting_confirmation",
                                summary=title, payload={"question_id": session.question.question_id})

    def _propose_task(self, session, draft):
        draft = ResearchDraft.model_validate({**session.draft.model_dump(), **draft.model_dump(exclude_unset=True)})
        self._question(session, "task", _text(session, "请确认本次研究范围；也可以直接输入修改要求。", "Confirm the research scope, or describe changes."),
            [("accept", _text(session, "采用这些设置", "Use these settings")),
             ("revise", _text(session, "我想修改", "I want to change this")),
             ("defer", _text(session, "暂不确定，继续整理资料", "Decide later; keep gathering material"))], proposed_draft=draft)
        return {"_terminal": True, "answer": _text(session,
            "已整理研究范围。请核对下方的公司、估值日和方法后选择，确认前不会覆盖当前设置。",
            "Review the proposed company, date and methods below. Current settings remain until you confirm.")}

    def _blocks(self, session):
        blocks = {b["block_id"]: b for doc in session.documents
                  for b in self.store.research_blocks(session.session_id, doc.file_id)}
        for message in self.store.list_messages(session.session_id):
            if message.role == "user":
                block_id = "message:" + message.message_id
                blocks[block_id] = {"block_id": block_id, "text": message.content,
                                    "location": {"message_id": message.message_id, "source_type": "user_note"}}
        return blocks

    def _facts(self, session, args):
        blocks = self._blocks(session)
        old_facts = {f.fact_id: f for f in session.facts}
        if any(key not in old_facts for key in args.replaces):
            raise ValueError("更正引用了不存在的候选字段。")
        candidates = []
        for item in args.candidates:
            block = blocks.get(item.block_id)
            compact = lambda text: re.sub(r"\s+", "", text)
            if not block or compact(item.quote) not in compact(block["text"]):
                raise ValueError("候选字段必须引用当前资料中的连续原文。")
            clean_value = re.sub(r"[,，\s]", "", item.raw_value).replace("−", "-")
            quoted = re.sub(r"[,，\s]", "", item.quote).replace("−", "-")
            if not re.search(r"(?<![\d.])" + re.escape(clean_value) + r"(?![\d.])", quoted):
                raise ValueError("候选数值未出现在引用原文中，请保留原始数值。")
            fact = FactCandidate(**item.model_dump(), fact_id=_id("fact_"))
            if item.block_id.startswith("message:"):
                fact.source_type = "user_note"
            factors = {"元": "1", "万元": "10000", "亿元": "100000000", "股": "1", "万股": "10000", "亿股": "100000000", "%": "0.01", "ratio": "1"}
            try:
                amount = Decimal(clean_value)
                if not amount.is_finite():
                    raise InvalidOperation()
                if fact.unit in factors:
                    fact.normalized_value = str(amount * Decimal(factors[fact.unit]))
            except InvalidOperation:
                fact.warnings.append("数值格式需复核")
            if fact.unit == "unknown":
                fact.warnings.append("单位待确认")
            if fact.period == "unknown":
                fact.warnings.append("期间待确认")
            if fact.scope == "unknown" and fact.role == "historical":
                fact.warnings.append("合并/母公司口径待确认")
            candidates.append(fact)
        # A conflict is visible, and cannot be accepted by a blanket confirmation.
        def identity(fact):
            return fact.metric, fact.period, fact.role
        if any(not any(identity(new) == identity(old_facts[key]) for new in candidates) for key in args.replaces):
            raise ValueError("更正必须提供对应字段的新候选值。")
        for fact in candidates:
            for other in [*session.facts, *candidates]:
                if other.fact_id not in args.replaces and other.fact_id != fact.fact_id and other.status != "rejected" and (
                    other.metric, other.period, other.scope, other.role
                ) == (fact.metric, fact.period, fact.scope, fact.role):
                    different = (Decimal(other.normalized_value) != Decimal(fact.normalized_value)) if other.normalized_value is not None and fact.normalized_value is not None else (other.raw_value, other.unit) != (fact.raw_value, fact.unit)
                    if different:
                        fact.warnings.append("同字段同期间存在冲突，需先更正候选")
                        break
        session.facts.extend(candidates)
        session.gaps = list(dict.fromkeys([*session.gaps, *args.missing]))[:50]
        self._question(session, "facts", _text(session, "核对候选字段及原文出处；不确定的字段可继续修改。", "Review candidate facts and their sources; uncertain fields can be revised."),
            [("accept", _text(session, "确认无警告的候选字段", "Confirm candidates without warnings")),
             ("reject", _text(session, "拒绝本批候选，重新整理", "Reject this batch")),
             ("defer", _text(session, "稍后确认", "Review later"))], fact_ids=[f.fact_id for f in candidates], superseded_fact_ids=args.replaces)
        return {"_terminal": True, "answer": _text(session,
            f"已提取 {len(candidates)} 个候选字段，并保留原文引用。带警告的字段不会被批量确认；确认仅代表采纳提取值，不代表财务审核通过。",
            f"Extracted {len(candidates)} candidates with source quotes. Warning-marked fields cannot be batch-confirmed. Confirmation accepts extraction; it is not a financial audit.")}

    def _answer_question(self, session, turn):
        question = session.question
        if question is None or turn.question_id != question.question_id:
            raise ValueError("这个确认问题已失效，请刷新后选择当前问题。")
        if turn.option_id not in {o.id for o in question.options}:
            raise ValueError("未知选项，请使用当前问题提供的选项。")
        if question.kind == "task" and turn.option_id == "accept":
            previous_company = (session.draft.company, session.draft.ticker)
            session.draft = question.proposed_draft.model_copy(deep=True)
            if any(previous_company) and previous_company != (session.draft.company, session.draft.ticker):
                for fact in session.facts:
                    if fact.status == "confirmed":
                        fact.status = "proposed"
                        fact.warnings.append("研究公司已变更，请重新核对该字段的归属")
        if question.kind == "facts":
            for fact in session.facts:
                if fact.fact_id in question.fact_ids:
                    if turn.option_id == "accept" and not fact.warnings:
                        fact.status = "confirmed"
                    elif turn.option_id == "reject":
                        fact.status = "rejected"
            accepted_metrics = {(f.metric, f.period, f.role) for f in session.facts if f.fact_id in question.fact_ids and f.status == "confirmed"}
            if turn.option_id == "accept":
                for fact in session.facts:
                    if fact.fact_id in question.superseded_fact_ids and (fact.metric, fact.period, fact.role) in accepted_metrics:
                        fact.status = "rejected"
        label = next(o.label for o in question.options if o.id == turn.option_id)
        session.question = None
        session.status = "collecting"
        self.store.append_event(session.session_id, type="review.resolved", stage="research", status="completed", summary=label,
            payload={"question_id": question.question_id, "option_id": turn.option_id,
                     "fact_ids": question.fact_ids, "superseded_fact_ids": question.superseded_fact_ids})
        if turn.option_id == "revise":
            return _text(session, "请直接输入希望修改的公司、日期或研究方法。当前设置尚未更改。", "Describe the company, date or method changes. Current settings have not changed.")
        return _text(session, "已记录你的选择。可以继续上传资料、补充要求，或输入 /prepare 检查资料缺口。", "Your choice is saved. Add material or requirements, or use /prepare to check gaps.")

    def _prepare(self, session):
        gaps = []
        if not (session.draft.company or session.draft.ticker):
            gaps.append("研究公司尚未确认")
        if session.draft.valuation_date is None:
            gaps.append("估值基准日尚未确认")
        if not session.draft.methods:
            gaps.append("估值方法尚未确认")
        if not any(f.status == "confirmed" for f in session.facts):
            gaps.append("尚无已确认财务字段")
        if any(f.status == "proposed" for f in session.facts):
            gaps.append("仍有待确认的候选字段")
        generated = {"研究公司尚未确认", "估值基准日尚未确认", "估值方法尚未确认", "尚无已确认财务字段", "仍有待确认的候选字段"}
        gaps.extend(gap for gap in session.gaps if gap not in generated)
        session.gaps = list(dict.fromkeys(gaps))
        session.status = "collecting" if gaps else "awaiting_financial_model"
        return {"_terminal": True, "answer": _text(session,
            "资料准备检查：\n" + ("\n".join("• " + g for g in session.gaps) or "已确认研究范围及字段。") +
            "\n正式金融模型尚未接入；当前不生成估值。完整字段要求和财务勾稽将在金融契约接入后执行。",
            "Preparation check: " + ("; ".join(session.gaps) or "Scope and facts confirmed.") +
            "\nThe financial model is not connected. This is a preparation check, not full financial validation or a valuation.")}

    def _llm_turn(self, session, llm):
        def gaps(args):
            session.gaps = list(dict.fromkeys(args.missing))
            return {"missing": session.gaps, "reason": args.reason}

        def read(args):
            blocks = self.store.research_blocks(session.session_id, args.file_id)
            if args.query:
                terms = args.query.lower().split()
                blocks = [b for b in blocks if any(word in b["text"].lower() for word in terms)]
            return {"total": len(blocks), "offset": args.offset, "blocks": blocks[args.offset:args.offset + args.limit]}

        def finish(args):
            blocks = self._blocks(session)
            if any(key not in blocks for key in args.evidence_ids):
                raise ValueError("回答包含不存在的来源引用。")
            if args.question and session.question is None:
                labels = args.options or [_text(session, "我来补充说明", "I will provide details")]
                self._question(session, "clarification", args.question,
                               [(f"choice_{i}", label) for i, label in enumerate(labels)])
            answer = args.answer
            if args.evidence_ids:
                answer += "\n\n" + _text(session, "来源：", "Sources: ") + "\n".join(
                    key + " · " + canonical(blocks[key]["location"]) for key in args.evidence_ids)
            session.summary = args.answer[:2000]
            return {"_terminal": True, "answer": answer}

        def search(args):
            session.gaps = list(dict.fromkeys([*session.gaps, args.reason]))[:50]
            self._question(session, "search_unavailable", _text(session,
                "联网搜索尚未接入，怎样补充这部分资料？", "Search is not connected. How would you like to supply this material?"),
                [("upload", _text(session, "我来上传资料", "I will upload sources")),
                 ("defer", _text(session, "保留缺口，继续研究", "Keep the gap and continue"))])
            return {"_terminal": True, "answer": _text(session,
                "已记录检索需求：" + args.query + "。本次没有发出网络请求，也没有补入未经核验的数据。",
                "Search need saved: " + args.query + ". No network request was made; no missing values were filled.")}

        registry = ToolRegistry([
            ToolSpec("inspect_context", "读取已确认研究范围、资料清单、候选字段和待处理问题。", NoArguments,
                     lambda _: {**session.model_dump(mode="json"), "user_notes": [b for b in self._blocks(session).values() if b["block_id"].startswith("message:")][-8:]}),
            ToolSpec("read_document", "读取当前会话已上传文件的原文块；支持关键词和分页，返回可引用 block_id。", ReadDocument, read),
            ToolSpec("propose_task", "根据用户明确意图提出完整研究设置，等待用户确认；不是直接修改。", ProposeTask,
                     lambda args: self._propose_task(session, args.draft)),
            ToolSpec("propose_facts", "从原文或用户消息提出候选；用户消息标为手工来源。更正旧值用 replaces 指定字段 ID，确认后才替换。", ProposeFacts,
                     lambda args: self._facts(session, args)),
            ToolSpec("search_sources", "记录公开资料/同业检索需求；搜索适配器未连接，返回补资料选项。", SearchSources, search),
            ToolSpec("update_data_gaps", "核对新资料后更新仍未解决的缺口，说明为何移除或增加；不能代替正式财务校验。", UpdateGaps, gaps),
            ToolSpec("check_preparation", "检查研究资料准备状态；正式金融计算尚未接入。", NoArguments,
                     lambda _: self._prepare(session)),
            ToolSpec("finish_response", "综合已有工具结果回答本轮，必要时提供澄清问题和选项。", FinishResponse, finish),
        ])
        return run_tool_loop(llm, research_context(session, self.store.list_messages(session.session_id)), registry,
            lambda name, args, fn: self._tool(session, name, json.loads(args), fn), max_rounds=12, max_tokens=4500)

    def _offline(self, session, text):
        if text.strip() == "/prepare":
            return self._prepare(session)
        if text.startswith("/company "):
            draft = session.draft.model_copy(update={"company": text[9:].strip()})
            return self._propose_task(session, draft)
        if text.startswith("/date "):
            draft = session.draft.model_copy(update={"valuation_date": date.fromisoformat(text[6:].strip())})
            return self._propose_task(session, draft)
        if text.startswith("/methods "):
            draft = ResearchDraft.model_validate({**session.draft.model_dump(), "methods": text[9:].strip().split(",")})
            return self._propose_task(session, draft)
        ticker = re.search(r"(?<!\d)([036]\d{5})(?:\.(SH|SZ|BJ))?(?!\d)", text, re.I)
        if ticker:
            draft = session.draft.model_copy(update={"ticker": ticker[0].upper(), "objective": text[:2000]})
            return self._propose_task(session, draft)
        return {"answer": _text(session,
            "需求已保存。当前为资料整理模式，可以上传文件、查看原文和确认研究范围。连接模型后可理解自然语言、抽取字段并分析政策。\n"
            "可输入：/company 公司名称、/date 2026-09-14、/methods dcf,pe、/prepare；连接模型后输入“提取已上传文件中的财务字段”。",
            "Your request is saved. Local preparation supports files, source previews and scope confirmation. Connect a model for natural-language understanding, extraction and policy analysis.\n"
            "Commands: /company Name, /date 2026-09-14, /methods dcf,pe, /prepare.")}

    def turn(self, session_id, turn: ResearchTurn):
        owner = _id("lease_")
        if not self.store.acquire(session_id, owner):
            raise ValueError("当前会话正在处理请求，请稍后重试。")
        stop = threading.Event()
        def heartbeat():
            while not stop.wait(5):
                self.store.heartbeat(session_id, owner)
        worker = threading.Thread(target=heartbeat, daemon=True)
        worker.start()
        try:
            session = self.store.get_research(session_id)
            if turn.language is not None:
                session.language = turn.language
            # Reject stale selections before logging or changing any state.
            if turn.option_id and (session.question is None or session.question.question_id != turn.question_id or
                                   turn.option_id not in {o.id for o in session.question.options}):
                raise ValueError("确认问题或选项已失效，请刷新后重试。")
            if turn.option_id and turn.content.strip() and session.question.kind in {"task", "facts"}:
                raise ValueError("修改要求请作为文字单独发送，不能同时确认旧候选。")
            question_kind = session.question.kind if session.question else None
            label = next((o.label for o in session.question.options if o.id == turn.option_id), "") if session.question else ""
            content = turn.content.strip() or label or "上传资料"
            self.store.add_message(session_id, "user", content, "research")
            intent = interpret_intent(content, {
                "company": session.draft.company,
                "ticker": session.draft.ticker,
                "revision": session.revision,
            })
            self.store.append_event(
                session_id,
                type="intent.classified",
                stage="planning",
                status="completed",
                summary=intent.intent,
                payload=intent.model_dump(mode="json"),
            )
            self.store.append_event(session_id, type="turn.started", stage="research", status="running", summary=content[:300])
            try:
                acknowledgement = self._answer_question(session, turn) if turn.option_id else None
                for file_id in dict.fromkeys(turn.file_ids):
                    if file_id in {d.file_id for d in session.documents}:
                        continue
                    meta = self.store.get_file(file_id)
                    def parse(meta=meta):
                        blocks, warnings = parse_document(meta)
                        self.store.save_research_blocks(session_id, meta["file_id"], blocks)
                        doc = DocumentSummary(file_id=meta["file_id"], name=meta["original_name"], role=meta["role"],
                                              block_count=len(blocks), warnings=warnings)
                        session.documents.append(doc)
                        return doc.model_dump(mode="json")
                    self._tool(session, "parse_document", {"file_id": file_id, "name": meta["original_name"]}, parse)
                llm = self._clients.get(session_id)
                if acknowledgement and not turn.content and not turn.file_ids and (question_kind != "clarification" or llm is None):
                    result = {"answer": acknowledgement}
                elif turn.content.strip() == "/prepare":
                    result = self._tool(session, "check_preparation", {}, lambda: self._prepare(session))
                elif llm is not None:
                    result = self._llm_turn(session, llm)
                elif session.requires_model:
                    raise LlmError("MODEL_CONNECTION_REQUIRED: 会话已保存，请重新连接模型后继续。")
                else:
                    result = self._offline(session, turn.content)
                self._say(session, result["answer"])
                self.store.append_event(session_id, type="turn.completed", stage="research", status="completed", summary="本轮研究已保存")
            except (ValueError, LlmError, OSError) as exc:
                message = str(exc) if not isinstance(exc, OSError) else "文件读取失败，请检查文件后重试。"
                self._say(session, message)
                self.store.append_event(session_id, type="turn.failed", stage="research", status="failed", summary=message)
            except Exception:
                self._say(session, "资料处理失败，已保留当前会话和成功读取的材料。请检查文件或模型后重试。")
                self.store.append_event(session_id, type="turn.failed", stage="research", status="failed", summary="资料或模型响应处理失败")
            self.store.save_research(session)
            return self.snapshot(session_id)
        finally:
            stop.set()
            worker.join(timeout=1)
            self.store.release(session_id, owner)
