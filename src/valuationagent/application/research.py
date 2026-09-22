"""Shared, finance-independent research conversations for CLI and Web."""
import hashlib
import json
import re
import threading
import time
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import Literal
from pydantic import Field, ValidationError
from valuationagent.core.documents import parse_document
from valuationagent.core.tools import NoArguments, ToolRegistry, ToolSpec, canonical
from valuationagent.llm.agent import run_tool_loop
from valuationagent.llm.client import LlmError
from valuationagent.llm.context import RESEARCH_PROMPT_VERSION, research_context
from valuationagent.llm.intent import interpret_intent
from valuationagent.search.providers import UnavailableSearchProvider
from valuationagent.schemas.agent import SearchQuery
from valuationagent.schemas.models import ApiModel
from valuationagent.schemas.research import (
    DocumentSummary, FactCandidate, ResearchChoice, ResearchDraft,
    ResearchIssue, ResearchMemoryItem, ResearchQuestion, ResearchSession,
    ResearchTurn,
)


class ReadDocument(ApiModel):
    file_id: str
    query: str = Field(default="", max_length=200)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=6, ge=1, le=8)


class ProposeTask(ApiModel):
    draft: ResearchDraft


class CandidateInput(ApiModel):
    metric: str = Field(
        min_length=1,
        max_length=120,
        description="来源中的字段名；只有映射证据明确时才使用标准字段名，歧义映射须先询问用户。",
    )
    raw_value: str = Field(
        min_length=1,
        max_length=100,
        description="原文中实际出现的数值，不得插值、推算或移到其他年份。",
    )
    unit: str = Field(
        default="unknown",
        description="原文明确给出的单位；来源未说明或单位冲突时使用 unknown。",
    )
    period: str = Field(
        default="unknown",
        description="原文明确对应的报告期；不得把披露日期或相邻列年份当作报告期。",
    )
    scope: str = Field(
        default="unknown",
        description="原文明确给出的合并、母公司或其他口径；无法判断时使用 unknown。",
    )
    role: str = "historical"
    block_id: str = Field(description="包含该候选值及其字段、期间或单位依据的来源块 ID。")
    quote: str = Field(
        min_length=1,
        max_length=2400,
        description="来源中的连续原文，须覆盖数值，并尽量同时覆盖字段名、年份、单位和口径。",
    )


class ProposeFacts(ApiModel):
    candidates: list[CandidateInput] = Field(min_length=1, max_length=20)
    missing: list[str] = Field(default_factory=list, max_length=30)
    replaces: list[str] = Field(default_factory=list, max_length=20)


class MemoryUpdate(ApiModel):
    key: str = Field(
        min_length=1,
        max_length=100,
        pattern=r"^[a-zA-Z0-9_.:-]+$",
        description="稳定、可复用的记忆键；同一事项变更时沿用原 key。",
    )
    kind: Literal["goal", "preference", "constraint", "decision", "definition"]
    content: str = Field(
        min_length=1,
        max_length=600,
        description="用户明确表达的长期上下文；不得写入财务事实、推断、临时结果或秘密。",
    )


class UpdateMemory(ApiModel):
    updates: list[MemoryUpdate] = Field(default_factory=list, max_length=8)
    remove_keys: list[str] = Field(default_factory=list, max_length=8)


class FinishResponse(ApiModel):
    answer: str = Field(
        min_length=1,
        max_length=7000,
        description="面向用户的真实结论；资料异常时说明发现位置、不确定点、影响和已安全完成的部分。",
    )
    evidence_ids: list[str] = Field(
        default_factory=list,
        max_length=12,
        description="支持回答的来源块 ID；不能引用未读取或不存在的来源。",
    )
    question: str = Field(
        default="",
        max_length=600,
        description="仅填写当前最小的阻塞问题；不得暗示系统已确定仍有歧义的字段或年份。",
    )
    options: list[str] = Field(
        default_factory=list,
        max_length=3,
        description="可选的 2—3 个互斥方案，说明采用的口径或影响；没有合理候选时留空，让用户自由输入。",
    )
    memory_updates: list[MemoryUpdate] = Field(
        default_factory=list,
        max_length=8,
        description="只保存用户明确表达、且未来回合仍有用的目标、偏好、约束、决定或术语定义。",
    )
    memory_remove_keys: list[str] = Field(
        default_factory=list,
        max_length=8,
        description="仅在用户明确撤回既有长期要求时填写对应 memory key。",
    )


class SearchSources(ApiModel):
    query: str = Field(min_length=1, max_length=500)
    reason: str = Field(min_length=1, max_length=500)
    purpose: Literal["company_profile", "financials", "comparables", "policy", "other"] = "other"
    as_of_date: date | None = None
    allowed_domains: list[str] = Field(default_factory=list, max_length=12)


class UpdateGaps(ApiModel):
    missing: list[str] = Field(default_factory=list, max_length=30)
    reason: str = Field(
        min_length=1,
        max_length=1000,
        description="说明实际缺失或冲突、来源位置及其影响；不得用默认值掩盖资料问题。",
    )


def _id(prefix):
    return prefix + uuid.uuid4().hex


def _text(session, zh, en):
    return en if session.language == "en-US" else zh


class ResearchService:
    def __init__(self, store, *, search_provider=None, tool_providers=()):
        self.store = store
        self._clients = {}
        self.search_provider = search_provider or UnavailableSearchProvider()
        self.tool_providers = tuple(tool_providers)

    @staticmethod
    def _model_identity(llm):
        config = getattr(llm, "config", None)
        return (
            str(getattr(config, "provider", "") or llm.__class__.__name__),
            str(getattr(config, "model", "") or "attached-model"),
        )

    def create(self, language="zh-CN", llm=None):
        provider, model = self._model_identity(llm) if llm is not None else ("", "")
        session = ResearchSession(
            session_id=_id("research_"),
            language=language,
            requires_model=llm is not None,
            prompt_version=RESEARCH_PROMPT_VERSION,
            model_provider=provider,
            model_name=model,
        )
        self.store.create_research(session)
        if llm is not None:
            self._clients[session.session_id] = llm
            self.store.append_event(
                session.session_id, type="model.attached", stage="configuration",
                status="completed", summary=f"{provider} / {model}",
                payload={"provider": provider, "model": model, "prompt_version": RESEARCH_PROMPT_VERSION},
            )
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
        session.model_provider, session.model_name = self._model_identity(llm)
        session.prompt_version = RESEARCH_PROMPT_VERSION
        self.store.append_event(
            session.session_id, type="model.attached", stage="configuration",
            status="completed", summary=f"{session.model_provider} / {session.model_name}",
            payload={"provider": session.model_provider, "model": session.model_name,
                     "prompt_version": RESEARCH_PROMPT_VERSION},
        )
        if session.last_issue and session.last_issue.code in {
            "LLM_HTTP_401", "LLM_HTTP_403", "MODEL_SESSION_REVOKED", "MODEL_CONNECTION_REQUIRED"
        }:
            self._resolve_issue(session)
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
                                payload={"arguments": self._redact_value(args)})
        try:
            result = fn()
        except Exception as exc:
            if isinstance(exc, ValidationError):
                fields = [".".join(str(part) for part in item["loc"])
                          for item in exc.errors(include_input=False)]
                message = "工具参数不符合约束" + ("：" + "、".join(fields[:12]) if fields else "。")
            elif isinstance(exc, (ValueError, LlmError)):
                message = str(exc)[:1200]
            else:
                message = "文件或工具处理失败，请检查资料后重试。"
            self.store.append_event(session.session_id, type="tool.failed", stage="research", tool=name,
                tool_call_id=call_id, status="failed", summary=message,
                duration_ms=int((time.monotonic() - started) * 1000),
                payload={"error_type": exc.__class__.__name__})
            raise
        self.store.append_event(session.session_id, type="tool.completed", stage="research", tool=name,
            tool_call_id=call_id, status="completed", summary=name,
            duration_ms=int((time.monotonic() - started) * 1000),
            payload={"output": self._redact_value(result)})
        self.store.save_research(session)
        return self._redact_value(result)

    def _question(self, session, kind, title, choices, **kwargs):
        session.question = ResearchQuestion(question_id=_id("question_"), kind=kind, title=title,
            options=[ResearchChoice(id=key, label=label) for key, label in choices], **kwargs)
        session.status = "waiting_confirmation"
        self.store.append_event(session.session_id, type="review.required", stage="research", status="waiting_confirmation",
                                summary=title, payload={"question_id": session.question.question_id})

    @staticmethod
    def _contains_secret(text):
        return bool(re.search(r"(?i)\b(?:sk|key)-[a-z0-9_-]{12,}\b|\bbearer\s+[a-z0-9._-]{12,}", text))

    @staticmethod
    def _redact_text(text):
        return re.sub(
            r"(?i)\b(?:sk|key)-[a-z0-9_-]{12,}\b|\bbearer\s+[a-z0-9._-]{12,}",
            "[REDACTED_CREDENTIAL]",
            text,
        )

    @classmethod
    def _redact_value(cls, value):
        if isinstance(value, str):
            return cls._redact_text(value)
        if isinstance(value, dict):
            return {key: cls._redact_value(item) for key, item in value.items()}
        if isinstance(value, list):
            return [cls._redact_value(item) for item in value]
        if isinstance(value, tuple):
            return tuple(cls._redact_value(item) for item in value)
        return value

    def _apply_memory(self, session, updates, remove_keys):
        current = {item.key: item for item in session.memory}
        removed, changed, rejected = [], [], []
        for key in dict.fromkeys(remove_keys):
            if key in current:
                current.pop(key)
                removed.append(key)
        user_messages = [m for m in self.store.list_messages(session.session_id) if m.role == "user"]
        source_message_id = user_messages[-1].message_id if user_messages else ""
        for update in updates:
            if self._contains_secret(update.content):
                rejected.append(update.key)
                continue
            # Reinsert replacements at the end so the bounded list keeps recent decisions.
            current.pop(update.key, None)
            current[update.key] = ResearchMemoryItem(
                **update.model_dump(), source_message_id=source_message_id
            )
            changed.append(update.key)
        session.memory = list(current.values())[-80:]
        if changed or removed or rejected:
            self.store.append_event(
                session.session_id,
                type="memory.updated",
                stage="context",
                status="completed" if not rejected else "warning",
                summary=f"长期记忆更新 {len(changed)}，移除 {len(removed)}，拒绝 {len(rejected)}",
                payload={"updated_keys": changed, "removed_keys": removed, "rejected_keys": rejected},
            )
        return {"updated": changed, "removed": removed, "rejected": rejected}

    def _recover(self, session, exc, stage="unknown", context=None):
        raw = str(exc).strip()
        match = re.match(r"([A-Z][A-Z0-9_]+):", raw)
        code = match.group(1) if match else (
            "DOCUMENT_PROCESSING_FAILED" if stage == "document" else
            "INPUT_INVALID" if isinstance(exc, ValueError) else
            "AGENT_UNEXPECTED_ERROR"
        )
        if isinstance(exc, (ValueError, LlmError)):
            message = raw[:1200] or "当前步骤无法可靠完成。"
        elif isinstance(exc, OSError):
            message = "文件读取失败，请检查格式、权限或文件是否完整。"
        else:
            message = "处理过程中出现未预期问题，已停止当前步骤。"
        retryable = code not in {"LLM_HTTP_401", "LLM_HTTP_403", "MODEL_SESSION_REVOKED", "MODEL_CONNECTION_REQUIRED"}
        issue = ResearchIssue(
            issue_id=_id("issue_"),
            code=code,
            stage=stage,
            message=message,
            retryable=retryable,
            context=context or {},
        )
        session.last_issue = issue
        if code in {"LLM_HTTP_401", "LLM_HTTP_403", "MODEL_SESSION_REVOKED", "MODEL_CONNECTION_REQUIRED"}:
            choices = [
                ("reconnect", _text(session, "重新配置模型后继续", "Reconnect a model and continue")),
                ("offline", _text(session, "切换为离线资料整理", "Switch to local preparation")),
                ("defer", _text(session, "保留进度，稍后处理", "Keep progress and decide later")),
            ]
        else:
            choices = [
                ("retry", _text(session, "按现有资料重试当前步骤", "Retry with the current material")),
                ("revise", _text(session, "我来补充或修改要求", "I will clarify or revise the request")),
                ("defer", _text(session, "保留进度，跳过这一步", "Keep progress and skip this step")),
            ]
        short_message = message[:360]
        title = _text(
            session,
            f"当前步骤未完成：{short_message} 已完成的资料和确认结果均已保留。你希望怎样继续？",
            f"This step did not complete: {short_message} Existing material and confirmed results were preserved. How should we continue?",
        )
        self._question(session, "recovery", title, choices)
        self.store.append_event(
            session.session_id,
            type="agent.recovery_required",
            stage=stage,
            status="waiting_confirmation",
            summary=message,
            payload={"issue_id": issue.issue_id, "code": code, "retryable": retryable},
        )
        return _text(
            session,
            f"我没有继续猜测或生成结果。问题代码：{code}。已保留当前进度，请选择下一步，也可以直接输入补充说明。",
            f"I stopped instead of guessing or producing a result. Issue code: {code}. Progress is saved; choose a next step or type clarification.",
        )

    def _resolve_issue(self, session, status="resolved"):
        if session.last_issue and session.last_issue.status in {"open", "retrying"}:
            session.last_issue.status = status
            self.store.append_event(
                session.session_id,
                type="agent.recovery_resolved",
                stage=session.last_issue.stage,
                status="completed",
                summary=session.last_issue.code,
                payload={"issue_id": session.last_issue.issue_id, "resolution": status},
            )

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
        if question.kind == "recovery":
            if turn.option_id == "retry" and session.last_issue:
                session.last_issue.status = "retrying"
            elif turn.option_id == "offline":
                session.requires_model = False
                self._clients.pop(session.session_id, None)
                self._resolve_issue(session)
            elif turn.option_id in {"defer", "revise"}:
                self._resolve_issue(session, "deferred")
        label = next(o.label for o in question.options if o.id == turn.option_id)
        session.question = None
        session.status = "collecting"
        self.store.append_event(session.session_id, type="review.resolved", stage="research", status="completed", summary=label,
            payload={"question_id": question.question_id, "option_id": turn.option_id,
                     "fact_ids": question.fact_ids, "superseded_fact_ids": question.superseded_fact_ids})
        if turn.option_id == "revise" and question.kind != "recovery":
            return _text(session, "请直接输入希望修改的公司、日期或研究方法。当前设置尚未更改。", "Describe the company, date or method changes. Current settings have not changed.")
        if question.kind == "recovery":
            if turn.option_id == "retry":
                return _text(session, "正在按已保存的上下文重试；不会重复采纳已确认字段。", "Retrying from saved context without re-accepting confirmed facts.")
            if turn.option_id == "reconnect":
                return _text(session, "请重新连接模型后继续。CLI 使用 /connect；网页使用“连接模型”。当前进度不会丢失。", "Reconnect the model, then continue. Current progress is preserved.")
            if turn.option_id == "offline":
                return _text(session, "已切换为离线资料整理模式。仍可上传、预览、确认范围和导出记录。", "Switched to local preparation. Uploads, previews, scope confirmation and exports remain available.")
            if turn.option_id == "revise":
                return _text(session, "请直接输入补充信息或修改要求，我会从已保存的进度继续。", "Type the additional information or revised request, and I will continue from saved progress.")
            return _text(session, "已保留当前进度。你可以继续提出其他问题或补充资料。", "Progress is preserved. You can continue with another question or add material.")
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

    def _llm_turn(self, session, llm, intent=None):
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
            answer = self._redact_text(args.answer)
            if args.question:
                guidance = _text(
                    session,
                    "请选择下方选项，或直接输入你的判断、补充信息或修改要求。",
                    "Choose an option below, or type your decision, additional context, or requested change.",
                )
                if guidance not in answer:
                    answer += "\n\n" + guidance
            if args.evidence_ids:
                answer += "\n\n" + _text(session, "来源：", "Sources: ") + "\n".join(
                    key + " · " + canonical(blocks[key]["location"]) for key in args.evidence_ids)
            memory_result = self._apply_memory(session, args.memory_updates, args.memory_remove_keys)
            if memory_result["rejected"]:
                answer += "\n\n" + _text(
                    session,
                    "出于安全考虑，疑似包含密钥的内容未写入长期记忆。",
                    "For safety, content that appeared to contain a credential was not stored in long-term memory.",
                )
            session.summary = self._redact_text(args.answer)[:2000]
            return {"_terminal": True, "answer": answer}

        def remember(args):
            result = self._apply_memory(session, args.updates, args.remove_keys)
            return {"memory_update": result, "active_memory_count": len(session.memory)}

        def search(args):
            query = SearchQuery(
                query=args.query,
                ticker=session.draft.ticker or None,
                company_name=session.draft.company or None,
                purpose=args.purpose,
                as_of_date=args.as_of_date or session.draft.valuation_date,
                information_cutoff=session.draft.valuation_date,
                allowed_domains=args.allowed_domains,
            )
            result = self.search_provider.search(query)
            if result.status == "not_configured":
                session.gaps = list(dict.fromkeys([*session.gaps, args.reason]))[:50]
                self._question(session, "search_unavailable", _text(session,
                    "联网搜索尚未接入，怎样补充这部分资料？", "Search is not connected. How would you like to supply this material?"),
                    [("upload", _text(session, "我来上传资料", "I will upload sources")),
                     ("defer", _text(session, "保留缺口，继续研究", "Keep the gap and continue"))])
                return {"_terminal": True, "answer": _text(session,
                    "已记录检索需求：" + args.query + "。本次没有发出网络请求，也没有补入未经核验的数据。",
                    "Search need saved: " + args.query + ". No network request was made; no missing values were filled.")}
            return result.model_dump(mode="json")

        base_specs = [
            ToolSpec("inspect_context", "读取已确认研究范围、资料清单、候选字段和待处理问题。", NoArguments,
                     lambda _: {**session.model_dump(mode="json"), "user_notes": [b for b in self._blocks(session).values() if b["block_id"].startswith("message:")][-8:]}),
            ToolSpec("read_document", "读取当前会话已上传文件的原文块；支持关键词和分页，返回可引用 block_id。遇到表格时应同时读取标题、表头、单位、相邻行和注释，不能凭单个单元格判断字段或年份。", ReadDocument, read),
            ToolSpec("propose_task", "根据用户明确意图提出完整研究设置，等待用户确认；不是直接修改。", ProposeTask,
                     lambda args: self._propose_task(session, args.draft)),
            ToolSpec("propose_facts", "从原文或用户消息提出无歧义的候选，并保留原始值、期间、单位和口径。字段或年份有多种合理对应时先澄清，不得静默映射。用户消息标为手工来源；更正旧值用 replaces 指定字段 ID，确认后才替换。", ProposeFacts,
                     lambda args: self._facts(session, args)),
            ToolSpec("search_sources", "通过已配置的搜索 provider 检索公开资料或同业；结果只是候选来源，必须进一步读取、核验并保留来源后才能形成事实。未配置时明确返回补资料选项。", SearchSources, search),
            ToolSpec("update_data_gaps", "核对新资料后更新仍未解决的缺失、字段/年份/单位/口径冲突，写明位置和影响；不能用默认值掩盖问题，也不能代替正式财务校验。", UpdateGaps, gaps),
            ToolSpec("update_memory", "保存用户明确表达且跨回合仍有效的目标、偏好、约束、决定或术语定义；不保存财务数值、推断、临时结果或秘密。需要继续调用其他终止工具时先使用本工具。", UpdateMemory, remember),
            ToolSpec("check_preparation", "检查研究资料准备状态；正式金融计算尚未接入。", NoArguments,
                     lambda _: self._prepare(session)),
            ToolSpec("finish_response", "综合已有工具结果回答本轮。遇到资料异常时如实说明发现、位置、不确定点和影响；必要时只问最小阻塞问题，提供 2—3 个互斥方案或让用户自由输入。", FinishResponse, finish),
        ]
        extension_specs, extensions = [], []
        for provider in self.tool_providers:
            specs = list(provider.tool_specs(session))
            extension_specs.extend(specs)
            extensions.append({
                "provider_id": str(getattr(provider, "provider_id", provider.__class__.__name__)),
                "version": str(getattr(provider, "version", "")),
                "tools": [spec.name for spec in specs],
            })
        registry = ToolRegistry([*base_specs, *extension_specs])
        messages = research_context(
            session,
            self.store.list_messages(session.session_id),
            intent.model_dump(mode="json") if hasattr(intent, "model_dump") else intent,
        )
        context_hash = hashlib.sha256(canonical(messages).encode("utf-8")).hexdigest()
        self.store.append_event(
            session.session_id,
            type="agent.started",
            stage="planning",
            status="running",
            summary="LLM 正在理解意图并选择注册工具",
            payload={
                "prompt_version": RESEARCH_PROMPT_VERSION,
                "context_sha256": context_hash,
                "context_messages": len(messages),
                "model_provider": session.model_provider,
                "model_name": session.model_name,
                "registered_tools": list(registry.specs),
                "tool_extensions": extensions,
            },
        )
        def invoke(name, arguments, fn):
            try:
                visible_arguments = json.loads(arguments)
            except (TypeError, ValueError):
                visible_arguments = {
                    "invalid_json": True,
                    "raw_sha256": hashlib.sha256(str(arguments).encode("utf-8")).hexdigest(),
                }
            return self._tool(session, name, visible_arguments, fn)

        try:
            result = run_tool_loop(
                llm,
                messages,
                registry,
                invoke,
                max_rounds=12,
                max_tokens=4500,
            )
        except Exception as exc:
            self.store.append_event(
                session.session_id,
                type="agent.failed",
                stage="planning",
                status="failed",
                summary=str(exc)[:500] if isinstance(exc, (ValueError, LlmError)) else "Agent 执行失败",
                payload={"prompt_version": RESEARCH_PROMPT_VERSION, "context_sha256": context_hash},
            )
            raise
        trace = result.pop("_agent_trace", {})
        self.store.append_event(
            session.session_id,
            type="agent.completed",
            stage="planning",
            status="completed",
            summary="Agent 已完成本轮决策",
            payload={"prompt_version": RESEARCH_PROMPT_VERSION, "context_sha256": context_hash, **trace},
        )
        return result

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
            if turn.question_id and not turn.option_id and (
                session.question is None or session.question.question_id != turn.question_id
            ):
                raise ValueError("补充说明对应的确认问题已失效，请刷新后重试。")
            if turn.option_id and turn.content.strip() and session.question.kind in {"task", "facts"}:
                raise ValueError("修改要求请作为文字单独发送，不能同时确认旧候选。")
            pending_question = session.question.model_copy(deep=True) if session.question else None
            question_kind = pending_question.kind if pending_question else None
            label = next((o.label for o in pending_question.options if o.id == turn.option_id), "") if pending_question else ""
            recovery_context = dict(session.last_issue.context) if question_kind == "recovery" and session.last_issue else {}
            raw_content = turn.content.strip() or label or "上传资料"
            content = self._redact_text(raw_content)
            user_message = self.store.add_message(session_id, "user", content, "research")
            if content != raw_content:
                self.store.append_event(
                    session_id, type="security.credential_redacted", stage="input",
                    status="completed", summary="疑似凭证已从对话内容中移除",
                )
            answered_question = None
            if (
                turn.content.strip()
                and pending_question
                and not turn.option_id
                and (
                    turn.question_id == pending_question.question_id
                    or pending_question.kind in {"clarification", "recovery"}
                )
            ):
                # Free text is a first-class answer to a pending question. The
                # previous proposal remains in history, but no stale card can
                # accidentally be accepted after the Agent processes the text.
                answered_question = pending_question.model_dump(mode="json")
                session.question = None
                session.status = "collecting"
                if pending_question.kind == "recovery" and session.last_issue:
                    session.last_issue.status = "retrying"
                self.store.append_event(
                    session_id,
                    type="review.resolved",
                    stage="research",
                    status="completed",
                    summary="用户以文字补充或修改",
                    payload={"question_id": pending_question.question_id, "option_id": "free_text"},
                )
            intent = interpret_intent(content, {
                "company": session.draft.company,
                "ticker": session.draft.ticker,
                "revision": session.revision,
            })
            planning_hint = {
                "classification": intent.model_dump(mode="json"),
                "answered_question": answered_question,
            }
            self.store.append_event(
                session_id,
                type="intent.classified",
                stage="planning",
                status="completed",
                summary=intent.intent,
                payload=intent.model_dump(mode="json"),
            )
            self.store.append_event(session_id, type="turn.started", stage="research", status="running", summary=content[:300])
            current_stage = "input"
            try:
                acknowledgement = self._answer_question(session, turn) if turn.option_id else None
                retry_requested = question_kind == "recovery" and turn.option_id == "retry"
                retry_files = recovery_context.get("file_ids", []) if retry_requested else []
                retry_content = ""
                if retry_requested and recovery_context.get("message_id"):
                    retry_content = next((
                        message.content for message in self.store.list_messages(session_id)
                        if message.message_id == recovery_context["message_id"]
                    ), "")
                current_stage = "document"
                for file_id in dict.fromkeys([*retry_files, *turn.file_ids]):
                    if file_id in {d.file_id for d in session.documents}:
                        continue
                    meta = self.store.get_file(file_id)

                    def parse(meta=meta):
                        blocks, warnings = parse_document(meta)
                        self.store.save_research_blocks(session_id, meta["file_id"], blocks)
                        doc = DocumentSummary(file_id=meta["file_id"], name=meta["original_name"], role=meta["role"],
                                              block_count=len(blocks), sha256=meta["sha256"],
                                              size_bytes=meta["size_bytes"], warnings=warnings)
                        session.documents.append(doc)
                        return doc.model_dump(mode="json")

                    self._tool(session, "parse_document", {"file_id": file_id, "name": meta["original_name"]}, parse)
                llm = self._clients.get(session_id)
                current_stage = "agent"
                if acknowledgement and not turn.content and not turn.file_ids and not retry_requested and (
                    question_kind != "clarification" or llm is None
                ):
                    result = {"answer": acknowledgement}
                elif content == "/prepare":
                    result = self._tool(session, "check_preparation", {}, lambda: self._prepare(session))
                elif llm is not None:
                    current_stage = "model"
                    result = self._llm_turn(session, llm, planning_hint)
                elif retry_requested:
                    result = self._offline(session, retry_content) if retry_content else {"answer": acknowledgement}
                elif session.requires_model:
                    current_stage = "model"
                    raise LlmError("MODEL_CONNECTION_REQUIRED: 会话已保存，请重新连接模型后继续。")
                else:
                    result = self._offline(session, content)
                if retry_requested or (answered_question and question_kind == "recovery"):
                    self._resolve_issue(session)
                self._say(session, result["answer"])
                self.store.append_event(session_id, type="turn.completed", stage="research", status="completed", summary="本轮研究已保存")
            except LlmError as exc:
                message = self._recover(
                    session,
                    exc,
                    "model" if current_stage in {"model", "agent"} else current_stage,
                    {"file_ids": list(dict.fromkeys([*turn.file_ids, *recovery_context.get("file_ids", [])])),
                     "intent": intent.intent, "message_id": user_message.message_id},
                )
                self._say(session, message)
                self.store.append_event(session_id, type="turn.interrupted", stage=current_stage, status="waiting_confirmation", summary=session.last_issue.code)
            except (ValueError, OSError) as exc:
                stage = (
                    "document" if current_stage == "document" else
                    "tool" if current_stage in {"agent", "model"} else
                    "input"
                )
                message = self._recover(session, exc, stage, {
                    "file_ids": list(dict.fromkeys([*turn.file_ids, *recovery_context.get("file_ids", [])])),
                    "intent": intent.intent,
                    "message_id": user_message.message_id,
                })
                self._say(session, message)
                self.store.append_event(session_id, type="turn.interrupted", stage=stage, status="waiting_confirmation", summary=session.last_issue.code)
            except Exception as exc:
                message = self._recover(session, exc, "unknown", {
                    "file_ids": list(turn.file_ids), "intent": intent.intent,
                    "message_id": user_message.message_id,
                })
                self._say(session, message)
                self.store.append_event(session_id, type="turn.interrupted", stage="unknown", status="waiting_confirmation", summary=session.last_issue.code)
            self.store.save_research(session)
            return self.snapshot(session_id)
        finally:
            stop.set()
            worker.join(timeout=1)
            self.store.release(session_id, owner)
