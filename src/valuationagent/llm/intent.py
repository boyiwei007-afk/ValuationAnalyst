"""Deterministic intent baseline used before and alongside an LLM interpreter."""

from __future__ import annotations

import re
from typing import Any

from valuationagent.schemas.agent import IntentResult


_TICKER = re.compile(r"(?<!\d)([036]\d{5})(?:\.(SH|SZ|BJ))?(?!\d)", re.I)

_VALUATION_ACTION_ZH = re.compile(
    r"(?:请|现在|直接|立即|马上|可以|就|按[^，。；\n]{0,40})?"
    r"(?:开始|启动|运行|执行|提交|进行)"
    r"(?:本次|该公司|上述|正式|完整|一次|自动化|自动)*\s*"
    r"(?:(?:DCF|P\s*/?\s*E|P\s*/?\s*S|EV\s*[/_-]?\s*EBITDA|现金流折现|相对)"
    r"\s*(?:和|与|、|,|，|\+)?\s*){0,4}(?:的)?估值(?:流程|计算|任务|建模)?",
    re.I,
)
_VALUATION_ACTION_EN = re.compile(
    r"\b(?:please\s+)?(?:start|run|execute|submit|proceed\s+with)\s+"
    r"(?:the\s+)?(?:formal\s+|full\s+)?(?:(?:DCF|P/E|PE|P/S|PS|EV/EBITDA)\s+)?valuation(?:\s+(?:workflow|model|calculation))?\b",
    re.I,
)
_VALUATION_NEGATION = re.compile(
    r"(?:不要|别|暂不|先不|无需|不能|无法|尚未|为什么|怎么|如何|是否|能否|可以吗)"
    r"[^，。；\n]{0,24}(?:开始|启动|运行|执行|提交|进行)?[^，。；\n]{0,12}估值"
)

_METHOD_TOKEN = re.compile(
    r"EV\s*[/_-]?\s*EBITDA|DCF|P\s*/?\s*E|P\s*/?\s*S",
    re.I,
)


def _explicit_methods(message: str) -> list[str]:
    """Extract only a method list that the user explicitly labelled.

    A bare mention of P/E in a research question must not silently change the
    task scope, so the parser first requires ``方法``/``methods`` or the
    ``/methods`` command and then normalizes the supported valuation names.
    """
    match = re.search(
        r"(?:/methods\s+|(?:估值)?方法\s*(?:改为|设为|选择|用|为|是)?\s*[:：]?\s*|"
        r"\bmethods?\s*(?:are|is|=|:)?\s*)([^\n。；;]{1,120})",
        message,
        re.I,
    )
    if not match:
        # Also accept the common trailing-label form, such as
        # ``只使用 DCF 和 PE 方法``. Requiring the trailing 方法/methods label
        # keeps a bare research mention of P/E from changing task scope.
        match = re.search(
            r"((?:EV\s*[/_-]?\s*EBITDA|DCF|P\s*/?\s*E|P\s*/?\s*S)"
            r"(?:\s*(?:、|,|，|和|与|及|and|&)\s*"
            r"(?:EV\s*[/_-]?\s*EBITDA|DCF|P\s*/?\s*E|P\s*/?\s*S)){0,3})"
            r"\s*(?:估值)?(?:方法|methods?)",
            message,
            re.I,
        )
    if not match:
        return []
    value = re.split(
        r"(?:估值(?:基准)?日期?|所属行业|申万行业|公司)\s*[:：]",
        match.group(1),
        maxsplit=1,
        flags=re.I,
    )[0]
    aliases = {
        "DCF": "dcf",
        "PE": "pe",
        "PS": "ps",
        "EVEBITDA": "ev_ebitda",
    }
    methods: list[str] = []
    for token in _METHOD_TOKEN.findall(value):
        method = aliases[re.sub(r"[\s/_-]", "", token).upper()]
        if method not in methods:
            methods.append(method)
    return methods


def requests_valuation(message: str) -> bool:
    """Recognize an explicit request to hand off to the deterministic pipeline.

    This deliberately accepts imperative language, not a bare mention of a
    valuation or a quoted error message. Ambiguous turns remain with the LLM.
    """
    text = message.strip()
    if not text:
        return False
    if re.fullmatch(r"/valuation", text, re.I):
        return True
    if _VALUATION_NEGATION.search(text):
        return False
    return bool(_VALUATION_ACTION_ZH.search(text) or _VALUATION_ACTION_EN.search(text))


def rejects_tushare(message: str) -> bool:
    """Detect an explicit instruction to stop using Tushare for this study."""
    normalized = re.sub(r"\s+", "", message).casefold()
    return any(phrase in normalized for phrase in (
        "不使用tushare", "不用tushare", "不要用tushare", "停止使用tushare",
        "取消tushare", "donotusetushare", "stopusingtushare",
    ))


def _slots(message: str, context: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    ticker = _TICKER.search(message)
    if ticker:
        result["ticker"] = ticker.group(0).upper()
        # Capture the equally common ``贵州茅台（600519.SH）`` form before
        # falling back to the short name after the code.
        before = message[:ticker.start()]
        prefix = re.search(
            r"([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9·]{1,59})\s*[（(]\s*$",
            before,
        )
        if prefix:
            value = re.sub(
                r"^(?:(?:请)?帮我(?:对|为|分析|研究|估值)?|"
                r"请(?:对|为|给)?|对|为|给|分析|研究|估值)+",
                "",
                prefix.group(1),
            ).strip()
            if len(value) >= 2:
                result["company"] = value
        # A-share requests commonly put the short company name immediately
        # after the code (for example ``600276 恒瑞医药``). Capture only that
        # compact token; longer descriptions remain the research objective.
        tail = message[ticker.end():]
        company = re.match(
            r"[\s·:：,，-]*([\u4e00-\u9fffA-Za-z][\u4e00-\u9fffA-Za-z0-9]{1,29})",
            tail,
        )
        if company and "company" not in result:
            value = re.split(
                r"(?:的历史|的估值|的年报|公司|进行|开展|先|希望|用于|数据)",
                company.group(1),
                maxsplit=1,
            )[0].strip("，,。；;：:")
            if len(value) >= 2 and value not in {
                "历史", "年报", "财务", "估值", "数据", "研究", "公司",
            }:
                result["company"] = value
    industry = re.search(
        r"(?:申万(?:一级|二级)?行业|所属行业|行业)\s*[:：]\s*([^，,。；;）)\]\n]{2,60})",
        message,
        re.I,
    )
    if industry:
        result["industry"] = industry.group(1).strip()
    for key, pattern in {
        "export_format": r"(?:/export\s+|导出(?:为|成)?\s*)(json|html|xlsx|excel|pdf)",
        "valuation_date": (
            r"(?:/date\s+|估值(?:基准)?日期?\s*(?:改为|设为|用|为|是)?\s*[:：]?\s*)"
            r"(\d{4}-\d{2}-\d{2})"
        ),
    }.items():
        match = re.search(pattern, message, re.I)
        if match:
            result[key] = match.group(1).lower()
    methods = _explicit_methods(message)
    if methods:
        result["methods"] = methods
    if context:
        for key in ("company", "ticker", "revision"):
            if key not in result and context.get(key) not in (None, ""):
                result[key] = context[key]
    return result


def interpret_intent(message: str, context: dict[str, Any] | None = None) -> IntentResult:
    """Classify a turn with transparent rules and conservative confidence.

    The result is a planning hint. It never authorizes a financial calculation
    or confirms a candidate fact by itself.
    """
    text = message.strip()
    lower = text.lower()
    slots = _slots(text, context)
    if not text:
        return IntentResult(intent="ask_explanation", confidence=0, slots=slots)
    if rejects_tushare(text):
        slots["data_source"] = "not_tushare"
        return IntentResult(intent="provide_material", confidence=.99, slots=slots)
    if requests_valuation(text):
        return IntentResult(intent="run_valuation", confidence=.99, slots=slots)
    if lower.startswith("/export") or any(word in lower for word in ("导出报告", "导出复算", "export")):
        return IntentResult(intent="request_export", confidence=.98, slots=slots)
    if lower.startswith("/review") or any(word in lower for word in ("确认候选", "复核", "review")):
        return IntentResult(intent="resume_review", confidence=.82, slots=slots, requires_confirmation=True)
    if any(word in lower for word in ("政策", "监管", "法规", "policy", "regulation")):
        return IntentResult(intent="policy_analysis", confidence=.9, slots=slots)
    if any(word in lower for word in ("上一版", "版本", "对比", "比较", "compare", "previous revision")):
        return IntentResult(intent="compare_versions", confidence=.84, slots=slots)
    if any(word in lower for word in ("为什么", "解释", "假设", "敏感性", "风险", "explain", "assumption", "sensitivity")):
        return IntentResult(intent="ask_explanation", confidence=.82, slots=slots)
    if any(word in lower for word in ("改为", "修改", "调整", "revise", "change", "set ")) or lower.startswith("/set"):
        return IntentResult(intent="revise_assumption", confidence=.8, slots=slots, requires_confirmation=True)
    if slots.get("ticker") and any(word in lower for word in ("估值", "研究", "valuation", "research")):
        return IntentResult(intent="new_valuation", confidence=.88, slots=slots)
    if text.startswith("/upload") or text.startswith("/company") or text.startswith("/date") or text.startswith("/methods"):
        return IntentResult(intent="provide_material", confidence=.95, slots=slots)
    if any(word in lower for word in ("上传", "年报", "excel", "pdf", "文件", "资料", "原文", "upload")):
        return IntentResult(intent="provide_material", confidence=.86, slots=slots)
    if slots.get("ticker") or any(word in lower for word in ("估值", "研究", "公司", "valuation", "research", "company")):
        return IntentResult(intent="new_valuation", confidence=.7 if slots.get("ticker") else .58, slots=slots)
    return IntentResult(intent="ask_explanation", confidence=.35, slots=slots)
