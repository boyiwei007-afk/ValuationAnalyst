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
from pydantic import Field, ValidationError, field_validator, model_validator
from valuationagent.core.documents import parse_document
from valuationagent.application.research_valuation import ResearchValuationAssembler
from valuationagent.core.data import LocalDataProvider
from valuationagent.core.tools import NoArguments, ToolRegistry, ToolSpec, canonical
from valuationagent.llm.agent import run_tool_loop
from valuationagent.llm.client import LlmError
from valuationagent.llm.context import RESEARCH_PROMPT_VERSION, research_context, research_snapshot
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


class InspectContext(ApiModel):
    section: Literal["overview", "facts", "memory", "documents", "user_notes"] = "overview"
    query: str = Field(default="", max_length=200, description="按关键词或 ID 查找历史字段、记忆或用户原话。")
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=8, ge=1, le=20)


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
    unit: Literal["元", "万元", "亿元", "股", "万股", "亿股", "%", "ratio", "unknown"] = Field(
        default="unknown",
        description="只能使用枚举中的标准值；来源未说明或单位冲突时使用 unknown。",
    )
    period: str = Field(
        default="unknown",
        description="原文明确对应的报告期；不得把披露日期或相邻列年份当作报告期。",
    )
    scope: Literal["consolidated", "parent", "unknown"] = Field(
        default="unknown",
        description="只能填写 consolidated（合并）、parent（母公司）或 unknown（无法判断），不得填写中文说明或自行扩展。",
    )
    role: Literal["historical", "assumption", "policy"] = "historical"
    block_id: str = Field(description="包含该候选值及其字段、期间或单位依据的来源块 ID。")
    quote: str = Field(
        min_length=1,
        max_length=2400,
        description="来源中的连续原文，须覆盖数值，并尽量同时覆盖字段名、年份、单位和口径。",
    )

    @field_validator("unit", mode="before")
    @classmethod
    def normalize_unit(cls, value):
        text = str(value or "unknown").strip().lower()
        aliases = {
            "人民币元": "元", "rmb": "元", "cny": "元",
            "人民币万元": "万元", "人民币亿元": "亿元",
            "百分比": "%", "percent": "%", "比例": "ratio",
            "未知": "unknown", "不明": "unknown", "": "unknown",
        }
        allowed = {"元", "万元", "亿元", "股", "万股", "亿股", "%", "ratio", "unknown"}
        return aliases.get(text, text if text in allowed else "unknown")

    @field_validator("scope", mode="before")
    @classmethod
    def normalize_scope(cls, value):
        text = str(value or "unknown").strip().lower()
        if text in {"consolidated", "合并", "合并口径", "合并报表"} or text.startswith("合并（"):
            return "consolidated"
        if text in {"parent", "母公司", "母公司口径", "母公司报表"} or text.startswith("母公司（"):
            return "parent"
        return "unknown"

    @field_validator("role", mode="before")
    @classmethod
    def normalize_role(cls, value):
        aliases = {
            "历史": "historical", "历史数据": "historical",
            "假设": "assumption", "预测假设": "assumption",
            "政策": "policy", "政策数据": "policy",
        }
        text = str(value or "historical").strip().lower()
        return aliases.get(text, text)


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

    @classmethod
    def _choice_error(cls):
        return "需要用户决定时必须提供 2—3 个明确、互斥且可执行的选项。"

    @model_validator(mode="after")
    def validate_question_choices(self):
        if self.question and not 2 <= len(self.options) <= 3:
            raise ValueError(self._choice_error())
        if not self.question and self.options:
            raise ValueError("没有问题时不能单独提供选项。")
        return self


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
        self._search_clients = {}
        self._market_clients = {}
        self.search_provider = search_provider or UnavailableSearchProvider()
        self.tool_providers = tuple(tool_providers)
        self.valuation_assembler = ResearchValuationAssembler()

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
            "欢迎来到 ValuationAgent。可以先说公司和研究目标，或上传年报、财务表和政策资料。我会整理来源和缺口；需要确认时给出可执行选项，最后一项 Chat 可补充你的判断。",
            "Welcome to ValuationAgent. Describe a company and your goal, or upload reports, spreadsheets and policy material. I will organize sources and gaps; when a decision is needed, choose an actionable option or use Chat for your own response."))
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
            "LLM_HTTP_401", "LLM_HTTP_403", "MODEL_SESSION_REVOKED", "MODEL_CONNECTION_REQUIRED",
            "LLM_TIMEOUT", "LLM_CONNECTION_FAILED", "LLM_NETWORK_FAILED", "LLM_RESPONSE_INVALID_JSON",
        }:
            self._resolve_issue(session)
        self.store.save_research(session)

    def attach_search(self, session_id, provider):
        """Attach a session-only search provider without persisting its secret."""
        if self.store.active(session_id):
            raise ValueError("当前会话正在处理请求，请稍后连接搜索服务。")
        session = self.store.get_research(session_id)
        provider_id = str(getattr(provider, "provider_id", "") or "unknown")
        if provider_id == "unavailable" or not callable(getattr(provider, "search", None)):
            raise ValueError("联网搜索服务配置无效。")
        self._search_clients[session_id] = provider
        self.store.append_event(
            session_id,
            type="search.attached",
            stage="configuration",
            status="completed",
            summary=f"联网搜索凭证已加载 · {provider_id}",
            payload={
                "provider": provider_id,
                "provider_version": str(getattr(provider, "version", "")),
            },
        )

    def attach_market(self, session_id, provider):
        """Attach a session-only A-share data provider without storing its token."""
        if self.store.active(session_id):
            raise ValueError("当前会话正在处理请求，请稍后连接A股取数服务。")
        self.store.get_research(session_id)
        if not callable(getattr(provider, "resolve", None)):
            raise ValueError("A股取数服务配置无效。")
        self._market_clients[session_id] = provider
        self.store.append_event(
            session_id,
            type="market.attached",
            stage="configuration",
            status="completed",
            summary=f"A股取数凭证已加载 · {getattr(provider, 'version', 'unknown')}",
            payload={"provider_version": str(getattr(provider, "version", ""))},
        )

    def data_service_status(self, session_id, *, default_market=None):
        """Return public availability metadata; never return credentials."""
        self.store.get_research(session_id)
        search = self._search_clients.get(session_id, self.search_provider)
        market = self._market_clients.get(session_id, default_market)
        search_id = str(getattr(search, "provider_id", "unavailable") or "unavailable")
        market_version = str(getattr(market, "version", "") or "")
        return {
            "search": {
                "available": search_id != "unavailable",
                "provider": search_id,
            },
            "market": {
                "available": bool(market and callable(getattr(market, "resolve", None)))
                and not isinstance(market, LocalDataProvider),
                "provider": market_version or "unavailable",
            },
        }

    def snapshot(self, session_id):
        session = self.store.get_research(session_id)
        return {"session": session.model_dump(mode="json"),
                "messages": [m.model_dump(mode="json") for m in self.store.list_messages(session_id)],
                "events": [e.model_dump(mode="json") for e in self.store.list_events(session_id)]}

    def submit_valuation(self, session_id, runner):
        """Create one auditable valuation run from the confirmed research state."""
        if self.store.active(session_id):
            raise ValueError("当前研究会话正在处理请求，请稍后提交估值。")
        session = self.store.get_research(session_id)
        if session.valuation_run_id:
            try:
                record = runner.store.get_run(session.valuation_run_id)
                if session_id in self._market_clients:
                    runner.attach_data(record.run_id, self._market_clients[session_id])
                return record
            except KeyError:
                session.valuation_run_id = None
        request = self.valuation_assembler.build(session)
        record = runner.create_run(request, self._clients.get(session_id))
        if session_id in self._market_clients:
            runner.attach_data(record.run_id, self._market_clients[session_id])
        session.valuation_run_id = record.run_id
        session.status = "submitted"
        self.store.save_research(session)
        self.store.append_event(
            session_id,
            type="valuation.submitted",
            stage="handoff",
            status="completed",
            summary=f"研究会话已提交估值任务 {record.run_id}",
            payload={
                "run_id": record.run_id,
                "data_source": request.data_source,
                "confirmed_fact_ids": [
                    fact.fact_id for fact in session.facts if fact.status == "confirmed"
                ],
            },
        )
        runner.store.append_event(
            record.run_id,
            type="research.handoff",
            stage="data_intake",
            status="completed",
            summary=f"来自研究会话 {session_id}",
            payload={"research_session_id": session_id, "research_revision": session.revision},
        )
        return record

    def _say(self, session, content):
        content = self._redact_text(content)
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
                message = self._redact_text(str(exc))[:1200]
            else:
                message = "文件或工具处理失败，请检查资料后重试。"
            self.store.append_event(session.session_id, type="tool.failed", stage="research", tool=name,
                tool_call_id=call_id, status="failed", summary=message,
                duration_ms=int((time.monotonic() - started) * 1000),
                payload={"error_type": exc.__class__.__name__})
            # The same safe error also goes back to the model for correction.
            if isinstance(exc, LlmError):
                raise LlmError(message) from None
            if isinstance(exc, ValueError) and not isinstance(exc, ValidationError):
                raise ValueError(message) from None
            raise
        self.store.append_event(session.session_id, type="tool.completed", stage="research", tool=name,
            tool_call_id=call_id, status="completed", summary=name,
            duration_ms=int((time.monotonic() - started) * 1000),
            payload={"output": self._redact_value(result)})
        self.store.save_research(session)
        return self._redact_value(result)

    def _question(self, session, kind, title, choices, **kwargs):
        title = self._redact_text(title)
        session.question = ResearchQuestion(question_id=_id("question_"), kind=kind, title=title,
            options=[ResearchChoice(id=key, label=self._redact_text(label)) for key, label in choices], **kwargs)
        session.status = "waiting_confirmation"
        self.store.append_event(session.session_id, type="review.required", stage="research", status="waiting_confirmation",
                                summary=title, payload={"question_id": session.question.question_id})

    def _ask_data_source(self, session, title=None):
        """Require an explicit acquisition route before external data work."""
        self._question(
            session,
            "data_source",
            title or _text(
                session,
                "这次研究需要外部数据，你希望怎样提供？",
                "How would you like to provide the external data for this study?",
            ),
            [
                (
                    "online",
                    _text(
                        session,
                        "联网获取（Tushare 财务数据与 Tavily 公开资料）",
                        "Retrieve online (Tushare financials and Tavily public sources)",
                    ),
                ),
                (
                    "upload",
                    _text(
                        session,
                        "我来上传年报或财务数据文件",
                        "I will upload annual reports or financial data files",
                    ),
                ),
            ],
        )

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
        raw = self._redact_text(str(exc)).strip()
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
            context=self._redact_value(context or {}),
        )
        session.last_issue = issue
        if code in {"LLM_HTTP_401", "LLM_HTTP_403", "MODEL_SESSION_REVOKED", "MODEL_CONNECTION_REQUIRED"}:
            choices = [
                ("reconnect", _text(session, "重新配置模型后继续", "Reconnect a model and continue")),
                ("offline", _text(session, "切换为离线资料整理", "Switch to local preparation")),
                ("defer", _text(session, "保留进度，稍后处理", "Keep progress and decide later")),
            ]
        elif code in {"LLM_TIMEOUT", "LLM_CONNECTION_FAILED", "LLM_NETWORK_FAILED", "LLM_RESPONSE_INVALID_JSON"}:
            choices = [
                ("retry", _text(session, "重试模型当前步骤", "Retry the current model step")),
                ("reconnect", _text(session, "重新配置模型连接", "Reconnect the model")),
                ("defer", _text(session, "保留进度，稍后继续", "Keep progress and continue later")),
            ]
        elif code == "AGENT_NO_PROGRESS":
            choices = [
                ("retry", _text(session, "拆成更小步骤后重试", "Retry in smaller steps")),
                ("revise", _text(session, "我来补充资料或说明口径", "I will add material or clarify the scope")),
                ("defer", _text(session, "保留有效结果，跳过失败字段", "Keep valid results and skip the failed field")),
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
        if code == "AGENT_NO_PROGRESS":
            detail = message.partition(":")[2].strip() or "模型重复了同一项无效工具调用。"
            return _text(
                session,
                f"模型重复提交了未通过校验的工具调用，我已停止循环。最近失败原因：{detail} 当前有效资料和确认结果均已保留。",
                f"The model repeated a tool call that failed validation, so I stopped the loop. Latest failure: {detail} Valid material and confirmations are preserved.",
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
        self._question(session, "task", _text(session, "请确认本次研究范围", "Confirm the research scope"),
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
        rejected = []
        for item in args.candidates:
            block = blocks.get(item.block_id)
            compact = lambda text: re.sub(r"\s+", "", text)
            if not block or compact(item.quote) not in compact(block["text"]):
                rejected.append(f"{item.metric}：引用不是当前资料中的连续原文")
                continue
            raw_value = item.raw_value.strip().replace("−", "-")
            # Validate the value before removing separators. Otherwise adjacent
            # report columns such as ``100 90`` collapse to ``10090`` and look
            # like one legitimate amount. Spaces/commas are accepted only when
            # they form conventional three-digit thousands groups.
            scalar_pattern = r"[+-]?(?:\d+(?:\.\d+)?|\d{1,3}(?:[,，\s]+\d{3})+(?:\.\d+)?)"
            if not re.fullmatch(scalar_pattern, raw_value):
                rejected.append(f"{item.metric}：原始值包含多个数字或不是单一有效数值")
                continue
            clean_value = re.sub(r"[,，\s]", "", raw_value)
            try:
                amount = Decimal(clean_value)
                if not amount.is_finite():
                    raise InvalidOperation()
            except InvalidOperation:
                rejected.append(f"{item.metric}：原始值包含多个数字或不是单一有效数值")
                continue
            # Preserve whitespace between adjacent report columns. Removing it
            # joined values such as ``1,286... 1,550...`` into one giant number
            # and falsely rejected the first, correctly quoted value.
            quoted = re.sub(r"[,，]", "", item.quote).replace("−", "-")
            # A positive candidate must not match the numeric suffix of a
            # negative source value (e.g. 12000 inside -12000).
            if not re.search(r"(?<![\d.+-])" + re.escape(clean_value) + r"(?![\d.])", quoted):
                rejected.append(f"{item.metric}：候选数值未出现在引用原文中")
                continue
            fact = FactCandidate(**item.model_dump(), fact_id=_id("fact_"))
            if item.block_id.startswith("message:"):
                fact.source_type = "user_note"
            factors = {"元": "1", "万元": "10000", "亿元": "100000000", "股": "1", "万股": "10000", "亿股": "100000000", "%": "0.01", "ratio": "1"}
            if fact.unit in factors:
                fact.normalized_value = str(amount * Decimal(factors[fact.unit]))
            if fact.unit == "unknown":
                fact.warnings.append("单位待确认")
            if fact.period == "unknown":
                fact.warnings.append("期间待确认")
            if fact.scope == "unknown" and fact.role == "historical":
                fact.warnings.append("合并/母公司口径待确认")
            candidates.append(fact)
        if not candidates:
            detail = "；".join(rejected[:5]) or "没有可核验的候选字段"
            raise ValueError("CANDIDATE_EVIDENCE_INVALID: " + detail)
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
        rejected_gaps = ["候选未采纳：" + item for item in rejected]
        session.gaps = list(dict.fromkeys([*session.gaps, *args.missing, *rejected_gaps]))[:50]
        eligible = sum(not fact.warnings for fact in candidates)
        warned = len(candidates) - eligible
        if eligible:
            choices = [
                ("accept", _text(
                    session,
                    f"确认 {eligible} 个无警告字段" + (f"，保留 {warned} 个继续核对" if warned else ""),
                    f"Confirm {eligible} clean candidate(s)" + (f"; keep {warned} for review" if warned else ""),
                )),
                ("reject", _text(session, "拒绝本批候选，重新整理", "Reject this batch")),
                ("defer", _text(session, "暂不确认，继续补充来源", "Keep all pending and add sources")),
            ]
        else:
            choices = [
                ("defer", _text(session, "保留候选，继续补充来源", "Keep candidates and add sources")),
                ("reject", _text(session, "拒绝本批候选，重新提取", "Reject this batch and extract again")),
            ]
        self._question(
            session,
            "facts",
            _text(
                session,
                f"本批 {len(candidates)} 个候选中，{eligible} 个可确认，{warned} 个仍需核对。",
                f"This batch contains {eligible} confirmable and {warned} pending candidate(s).",
            ),
            choices,
            fact_ids=[f.fact_id for f in candidates],
            superseded_fact_ids=args.replaces,
        )
        return {"_terminal": True, "answer": _text(session,
            f"已提取 {len(candidates)} 个候选字段，并保留原文引用。" +
            (f"另有 {len(rejected)} 个字段因证据不完整而隔离，未影响本批其他字段。" if rejected else "") +
            "带警告的字段不会被批量确认；确认仅代表采纳提取值，不代表财务审核通过。",
            f"Extracted {len(candidates)} candidates with source quotes. " +
            (f"{len(rejected)} unsupported candidates were isolated without discarding the valid batch. " if rejected else "") +
            "Warning-marked fields cannot be batch-confirmed. Confirmation accepts extraction; it is not a financial audit.")}

    def _answer_question(self, session, turn):
        question = session.question
        if question is None or turn.question_id != question.question_id:
            raise ValueError("这个确认问题已失效，请刷新后选择当前问题。")
        if turn.option_id not in {o.id for o in question.options}:
            raise ValueError("未知选项，请使用当前问题提供的选项。")
        ask_data_source_after = False
        if question.kind == "task" and turn.option_id == "accept":
            previous_company = (session.draft.company, session.draft.ticker)
            session.draft = question.proposed_draft.model_copy(deep=True)
            if any(previous_company) and previous_company != (session.draft.company, session.draft.ticker):
                for fact in session.facts:
                    if fact.status == "confirmed":
                        fact.status = "proposed"
                        fact.warnings.append("研究公司已变更，请重新核对该字段的归属")
            ask_data_source_after = (
                not session.data_source_preference
                and not session.documents
                and not any(fact.status == "confirmed" for fact in session.facts)
            )
        if question.kind == "data_source":
            session.data_source_preference = turn.option_id
        if question.kind in {"search_unavailable", "search_failed"} and turn.option_id == "upload":
            session.data_source_preference = "upload"
        fact_resolution = None
        if question.kind == "facts":
            batch = [fact for fact in session.facts if fact.fact_id in question.fact_ids]
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
            fact_resolution = {
                "confirmed": sum(fact.status == "confirmed" for fact in batch),
                "pending": sum(fact.status == "proposed" for fact in batch),
                "rejected": sum(fact.status == "rejected" for fact in batch),
            }
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
        if question.kind == "facts" and fact_resolution is not None:
            if turn.option_id == "accept":
                return _text(
                    session,
                    f"本批实际确认 {fact_resolution['confirmed']} 个字段；"
                    f"{fact_resolution['pending']} 个带警告字段仍未进入估值。可继续补充来源、让 Agent 更正，或拒绝这些候选。",
                    f"Confirmed {fact_resolution['confirmed']} field(s). "
                    f"{fact_resolution['pending']} warning-marked field(s) remain excluded from valuation. Add sources, ask the Agent to revise them, or reject them.",
                )
            if turn.option_id == "reject":
                return _text(
                    session,
                    f"已拒绝本批 {fact_resolution['rejected']} 个候选，它们不会进入估值。",
                    f"Rejected {fact_resolution['rejected']} candidate(s); they will not enter valuation.",
                )
            return _text(
                session,
                f"已保留 {fact_resolution['pending']} 个待核对候选，均不会在确认前进入估值。",
                f"Kept {fact_resolution['pending']} candidate(s) pending; none enter valuation before confirmation.",
            )
        if ask_data_source_after:
            self._ask_data_source(session)
            return _text(
                session,
                "研究范围已确认。开始整理外部数据前，请先选择资料来源。",
                "The research scope is confirmed. Choose a data source before gathering external data.",
            )
        if question.kind == "data_source":
            if turn.option_id == "online":
                return _text(
                    session,
                    "已选择联网获取。Agent 将按需调用 Tushare 与 Tavily，并保留来源；服务未配置或鉴权失败时会立即说明。",
                    "Online retrieval selected. The Agent may use Tushare and Tavily with source records, and will report missing configuration or authentication failures.",
                )
            return _text(
                session,
                "已选择自行上传。请上传年报、财务 Excel 或其他原始资料；在你明确切换前，Agent 不会用联网结果替代这些资料。",
                "Upload selected. Add annual reports, financial spreadsheets or other source material; the Agent will not replace them with online results unless you explicitly switch.",
            )
        return _text(session, "已记录你的选择。可以继续上传资料、补充要求，或输入 /prepare 检查资料缺口。", "Your choice is saved. Add material or requirements, or use /prepare to check gaps.")

    def _prepare(self, session):
        blocking = []
        if not (session.draft.company or session.draft.ticker):
            blocking.append("研究公司尚未确认")
        if session.draft.valuation_date is None:
            blocking.append("估值基准日尚未确认")
        if not session.draft.methods:
            blocking.append("估值方法尚未确认")
        if not session.data_source_preference:
            blocking.append("尚未确认资料来源（联网获取或自行上传）")
        has_ticker = bool(session.draft.ticker)
        if session.data_source_preference == "upload" and not any(
            f.status == "confirmed" for f in session.facts
        ):
            blocking.append("已选择自行上传，但尚无可用于估值的已确认财务字段")
        elif session.data_source_preference == "online" and not has_ticker and not any(
            f.status == "confirmed" for f in session.facts
        ):
            blocking.append("尚无已确认财务字段")
        online_ticker = session.data_source_preference == "online" and has_ticker
        if any(f.status == "proposed" for f in session.facts) and not online_ticker:
            blocking.append("仍有待确认的候选字段")
        generated = {
            "研究公司尚未确认", "估值基准日尚未确认", "估值方法尚未确认",
            "尚未确认资料来源（联网获取或自行上传）",
            "已选择自行上传，但尚无可用于估值的已确认财务字段",
            "尚无已确认财务字段", "仍有待确认的候选字段",
        }
        if not blocking:
            try:
                self.valuation_assembler.build(session)
            except ValueError as exc:
                blocking.append(str(exc))
        session.status = "collecting" if blocking else "ready_for_valuation"
        research_gaps = [gap for gap in session.gaps if gap not in generated]
        session.gaps = list(dict.fromkeys([*blocking, *research_gaps]))
        if not blocking and online_ticker:
            pending = sum(fact.status == "proposed" for fact in session.facts)
            return {"_terminal": True, "answer": _text(
                session,
                "资料准备检查：已具备正式在线取数条件。\n"
                "• 正式估值将按 A 股代码通过 Tushare 获取点时结构化财务数据和可比公司。\n"
                + (f"• 当前 {pending} 个未确认的搜索候选只保留为研究记录，不会进入正式计算。\n" if pending else "")
                + ("• 仍有研究资料缺口，但不会覆盖或替代 Tushare 数据。\n" if research_gaps else "")
                + "下一步输入 /valuation；若 Tushare 尚未配置，系统会要求连接后再提交。",
                "Preparation check: ready for formal online data retrieval. The valuation will use point-in-time Tushare data and peers. "
                "Unconfirmed search candidates remain research notes and are excluded from calculations. Next, use /valuation.")}
        return {"_terminal": True, "answer": _text(session,
            "资料准备检查：\n" + ("\n".join("• " + g for g in blocking) or "已确认研究范围及字段。") +
            ("\n可以提交正式估值；提交后会执行财务审核、十年预测、DCF、相对估值和敏感性分析。" if not blocking else "\n请先解决上述阻塞项目，再提交正式估值。"),
            "Preparation check: " + ("; ".join(blocking) or "Scope and facts confirmed.") +
            ("\nReady to submit the formal valuation workflow." if not blocking else "\nResolve these blocking items before formal valuation."))}

    def _llm_turn(self, session, llm, intent=None):
        session.prompt_version = RESEARCH_PROMPT_VERSION
        document_reads: dict[str, dict[str, int]] = {}

        def inspect(args):
            messages = self.store.list_messages(session.session_id)
            if args.section == "overview":
                snapshot = research_snapshot(session, messages)
                return {
                    **snapshot.task_state,
                    "session_id": session.session_id,
                    "revision": session.revision,
                    "summary": session.summary,
                    "user_notes": [
                        {"block_id": "message:" + message.message_id,
                         "text": message.content,
                         "location": {"message_id": message.message_id, "source_type": "user_note"}}
                        for message in messages if message.role == "user"
                    ][-2:],
                    "retrieval": "使用 section、query、offset、limit 检索完整历史；按 next_offset 继续分页。",
                }
            if args.section == "user_notes":
                rows = [
                    {"block_id": "message:" + message.message_id,
                     "text": message.content,
                     "location": {"message_id": message.message_id, "source_type": "user_note"}}
                    for message in messages if message.role == "user"
                ]
            else:
                rows = [item.model_dump(mode="json") for item in getattr(session, args.section)]
            if args.query:
                needle = args.query.casefold()
                rows = [row for row in rows if needle in canonical(row).casefold()]
            page, used = [], 0
            for row in rows[args.offset:args.offset + args.limit]:
                size = len(canonical(row))
                if page and used + size > 24000:
                    break
                page.append(row)
                used += size
            end = args.offset + len(page)
            return {"section": args.section, "total": len(rows), "offset": args.offset,
                    "items": page, "next_offset": end if end < len(rows) else None}

        def gaps(args):
            session.gaps = list(dict.fromkeys(args.missing))
            return {"missing": session.gaps, "reason": args.reason}

        def read(args):
            blocks = self.store.research_blocks(session.session_id, args.file_id)
            counters = document_reads.setdefault(
                args.file_id, {"unfiltered": 0, "filtered": 0}
            )
            counter = "filtered" if args.query else "unfiltered"
            counters[counter] += 1
            limit = 2 if args.query else 1
            if counters[counter] > limit:
                return {
                    "total": len(blocks),
                    "offset": args.offset,
                    "blocks": [],
                    "next_offset": None,
                    "retrieval_budget_exhausted": True,
                    "guidance": (
                        "本轮对该文件的同类读取预算已用完。不要继续改变 offset 逐页扫描；"
                        "请利用已读证据完成阶段性回答，并明确尚未覆盖的范围。若仍缺关键证据，"
                        "下一轮按更具体关键词继续。"
                    ),
                }
            if args.query:
                terms = args.query.lower().split()
                blocks = [b for b in blocks if any(word in b["text"].lower() for word in terms)]
            page = blocks[args.offset:args.offset + args.limit]
            end = args.offset + len(page)
            result = {
                "total": len(blocks),
                "offset": args.offset,
                "blocks": page,
                "next_offset": end if end < len(blocks) else None,
                "retrieval_budget_remaining": limit - counters[counter],
            }
            if not args.query and len(blocks) > args.limit:
                result["guidance"] = (
                    "这是长文档预览。不要沿 next_offset 顺序遍历全文；下一次请设置 query，"
                    "用与用户问题直接相关的关键词检索，随后综合回答并披露覆盖边界。"
                )
            return result

        def finish(args):
            blocks = self._blocks(session)
            if any(key not in blocks for key in args.evidence_ids):
                raise ValueError("回答包含不存在的来源引用。")
            if args.question and session.question is None:
                labels = args.options
                self._question(session, "clarification", args.question,
                               [(f"choice_{i}", label) for i, label in enumerate(labels)])
            answer = self._redact_text(args.answer)
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
            if not session.data_source_preference:
                self._ask_data_source(session)
                return {
                    "_terminal": True,
                    "answer": _text(
                        session,
                        "尚未确定资料来源，因此没有发出网络请求。请先选择联网获取或自行上传。",
                        "No data source has been selected, so no network request was made. Choose online retrieval or upload first.",
                    ),
                }
            if session.data_source_preference == "upload":
                self._ask_data_source(session, _text(
                    session,
                    "当前选择为自行上传。是否改用联网获取？",
                    "The current source is upload. Would you like to switch to online retrieval?",
                ))
                return {
                    "_terminal": True,
                    "answer": _text(
                        session,
                        "当前资料来源设置为自行上传，因此没有发出网络请求。",
                        "The source is currently set to upload, so no network request was made.",
                    ),
                }
            query = SearchQuery(
                query=args.query,
                ticker=session.draft.ticker or None,
                company_name=session.draft.company or None,
                purpose=args.purpose,
                as_of_date=args.as_of_date or session.draft.valuation_date,
                information_cutoff=session.draft.valuation_date,
                allowed_domains=args.allowed_domains,
            )
            provider = self._search_clients.get(session.session_id, self.search_provider)
            result = provider.search(query)
            if result.status == "not_configured":
                session.gaps = list(dict.fromkeys([*session.gaps, args.reason]))[:50]
                self._question(session, "search_unavailable", _text(session,
                    "当前会话未配置联网搜索服务，怎样继续？", "Web search is not configured for this session. How would you like to continue?"),
                    [("upload", _text(session, "我来上传资料", "I will upload sources")),
                     ("defer", _text(session, "保留缺口，继续研究", "Keep the gap and continue"))])
                return {"_terminal": True, "answer": _text(session,
                    "已记录检索需求：" + args.query + "。当前进程未配置 Tavily API Key，因此本次没有发出网络请求，也没有补入未经核验的数据。",
                    "Search need saved: " + args.query + ". This process has no Tavily API key, so no network request was made and no missing values were filled.")}
            if result.status == "failed":
                session.gaps = list(dict.fromkeys([*session.gaps, args.reason]))[:50]
                detail = result.error_message or _text(
                    session,
                    "联网搜索失败，本次没有取得任何资料。",
                    "Web search failed and returned no material.",
                )
                self._question(
                    session,
                    "search_failed",
                    _text(
                        session,
                        f"联网搜索未完成：{detail} 怎样继续？",
                        f"Web search did not complete: {detail} How would you like to continue?",
                    ),
                    [
                        ("retry", _text(session, "重试本次联网检索", "Retry this web search")),
                        ("upload", _text(session, "改为上传资料", "Upload sources instead")),
                        ("defer", _text(session, "保留资料缺口", "Keep the data gap")),
                    ],
                )
                return {
                    "_terminal": True,
                    "answer": _text(
                        session,
                        f"{detail} 已保留检索需求和当前进度；没有把失败结果交给模型继续猜测。",
                        f"{detail} The search need and current progress were preserved; the failed result was not passed to the model as evidence.",
                    ),
                }
            output = result.model_dump(mode="json")
            for index, hit in enumerate(result.hits, 1):
                file_id = "web_" + hit.source_id
                block_id = f"{file_id}:1"
                text = "\n".join(part for part in [
                    hit.title,
                    f"URL: {hit.url}",
                    f"Published: {hit.published_at}" if hit.published_at else "",
                    hit.snippet,
                ] if part)
                digest = hashlib.sha256(text.encode("utf-8")).hexdigest()
                if file_id not in {document.file_id for document in session.documents}:
                    self.store.save_research_blocks(session.session_id, file_id, [{
                        "block_id": block_id,
                        "text": text,
                        "location": {
                            "source_type": "web_search",
                            "provider": result.provider,
                            "url": hit.url,
                            "domain": hit.domain,
                            "published_at": hit.published_at.isoformat() if hit.published_at else None,
                            "search_query": args.query,
                        },
                    }])
                    session.documents.append(DocumentSummary(
                        file_id=file_id,
                        name=hit.title,
                        role="evidence",
                        block_count=1,
                        sha256=digest,
                        size_bytes=len(text.encode("utf-8")),
                        warnings=["联网搜索摘要；形成关键事实前应打开原始URL核对全文。"],
                    ))
                output["hits"][index - 1]["block_id"] = block_id
            return output

        def preparation(_):
            # Inside an LLM turn this is evidence for the final response, not a
            # substitute for answering the user's actual question. Direct
            # `/prepare` still returns the terminal checklist in turn().
            result = self._prepare(session)
            return {
                "status": session.status,
                "gaps": list(session.gaps),
                "checklist": result.get("answer", ""),
                "instruction": (
                    "Use this checklist only as supporting evidence. Continue to "
                    "finish_response and answer the user's stated request."
                ),
            }

        base_specs = [
            ToolSpec("inspect_context", "读取有界任务摘要，或按 section、query、offset、limit 分页检索事实、长期记忆、文件清单及早期用户原话；返回 next_offset 时可继续读取。", InspectContext, inspect),
            ToolSpec("read_document", "读取当前会话已上传文件的原文块；支持关键词和分页，返回可引用 block_id。遇到表格时应同时读取标题、表头、单位、相邻行和注释，不能凭单个单元格判断字段或年份。", ReadDocument, read),
            ToolSpec("propose_task", "根据用户明确意图提出完整研究设置，等待用户确认；不是直接修改。", ProposeTask,
                     lambda args: self._propose_task(session, args.draft)),
            ToolSpec("propose_facts", "从原文或用户消息提出无歧义的候选，并保留原始值、期间、单位和口径。scope 只用 consolidated、parent、unknown；不确定就用 unknown。字段或年份有多种合理对应时先澄清，不得静默映射。一次可只提交证据最充分的少量字段，缺失项放入 missing；用户消息标为手工来源；更正旧值用 replaces 指定字段 ID，确认后才替换。", ProposeFacts,
                     lambda args: self._facts(session, args)),
            ToolSpec("search_sources", "通过已配置的搜索 provider 检索公开资料或同业；结果只是候选来源，必须进一步读取、核验并保留来源后才能形成事实。未配置时明确返回补资料选项。", SearchSources, search),
            ToolSpec("update_data_gaps", "核对新资料后更新仍未解决的缺失、字段/年份/单位/口径冲突，写明位置和影响；不能用默认值掩盖问题，也不能代替正式财务校验。", UpdateGaps, gaps),
            ToolSpec("update_memory", "保存用户明确表达且跨回合仍有效的目标、偏好、约束、决定或术语定义；不保存财务数值、推断、临时结果或秘密。需要继续调用其他终止工具时先使用本工具。", UpdateMemory, remember),
            ToolSpec("check_preparation", "仅检查当前研究是否具备提交正式估值的条件；它不会代替文件评审、方案分析或最终答复。调用后仍须使用 finish_response 回答用户。A股代码可走在线取数，确认字段可组装为结构化输入。", NoArguments,
                     preparation),
            ToolSpec("finish_response", "综合已有工具结果回答本轮。遇到资料异常时如实说明发现、位置、不确定点和影响；需要用户决定、确认或恢复时只问最小阻塞问题，并结合上下文提供 2—3 个互斥、可执行的选项。界面会自动追加 Chat 自由输入入口。", FinishResponse, finish),
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
                max_rounds=20,
                max_tokens=4500,
            )
        except Exception as exc:
            self.store.append_event(
                session.session_id,
                type="agent.failed",
                stage="planning",
                status="failed",
                summary=self._redact_text(str(exc))[:500] if isinstance(exc, (ValueError, LlmError)) else "Agent 执行失败",
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
        if text.startswith("/industry "):
            draft = session.draft.model_copy(update={"industry": text[10:].strip()})
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
            "可输入：/company 公司名称、/industry 行业、/date 2026-09-14、/methods dcf,pe、/prepare；连接模型后输入“提取已上传文件中的财务字段”。",
            "Your request is saved. Local preparation supports files, source previews and scope confirmation. Connect a model for natural-language understanding, extraction and policy analysis.\n"
            "Commands: /company Name, /industry Industry, /date 2026-09-14, /methods dcf,pe, /prepare.")}

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
            answered_question = pending_question.model_dump(mode="json") if turn.option_id else None
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
                "selected_option_id": turn.option_id,
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
                if turn.file_ids and not session.data_source_preference:
                    # Supplying a file is itself an explicit source choice.
                    session.data_source_preference = "upload"
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
                continue_with_model = (
                    question_kind == "clarification"
                    or (question_kind == "data_source" and turn.option_id == "online")
                )
                if acknowledgement and not turn.content and not turn.file_ids and not retry_requested and (
                    not continue_with_model or llm is None
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
                if retry_requested or (answered_question and question_kind == "recovery" and not turn.option_id):
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
