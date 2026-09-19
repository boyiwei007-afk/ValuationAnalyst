"""Deterministic intent baseline used before and alongside an LLM interpreter."""

from __future__ import annotations

import re
from typing import Any

from valuationagent.schemas.agent import IntentResult


_TICKER = re.compile(r"(?<!\d)([036]\d{5})(?:\.(SH|SZ|BJ))?(?!\d)", re.I)


def _slots(message: str, context: dict[str, Any] | None) -> dict[str, Any]:
    result: dict[str, Any] = {}
    ticker = _TICKER.search(message)
    if ticker:
        result["ticker"] = ticker.group(0).upper()
    for key, pattern in {
        "export_format": r"(?:/export\s+|导出(?:为|成)?\s*)(json|html|xlsx|excel|pdf)",
        "valuation_date": r"(?:/date\s+|估值(?:基准)?日(?:改为|用)?\s*)(\d{4}-\d{2}-\d{2})",
    }.items():
        match = re.search(pattern, message, re.I)
        if match:
            result[key] = match.group(1).lower()
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
