from __future__ import annotations
from typing import Callable
from valuationagent.core.tools import ToolRegistry, canonical
from valuationagent.llm.client import LlmError


def run_tool_loop(
    llm, messages: list[dict], registry: ToolRegistry, call: Callable, *, max_rounds=6, max_tokens=900
):
    """Finite tool loop. Free text is never interpreted as a command or valuation."""
    messages = list(messages)
    for _ in range(max_rounds):
        reply = llm.chat(messages, tools=registry.schemas(), tool_choice="required", max_tokens=max_tokens)
        calls = reply.get("tool_calls") or []
        if len(calls) != 1:
            messages.append(
                {
                    "role": "user",
                    "content": "请只调用一个已注册工具；纯文本不能提交任务。",
                }
            )
            continue
        item = calls[0]
        fn = item.get("function", {})
        if not isinstance(item.get("id"), str) or not isinstance(
            fn.get("arguments"), str
        ):
            raise LlmError("TOOL_RESPONSE_INVALID: 工具调用缺少 id 或 JSON 参数。")
        if len(fn["arguments"]) > 32000:
            raise LlmError("TOOL_ARGUMENTS_TOO_LARGE: 工具参数过长。")
        clean = {
            "role": "assistant",
            "content": reply.get("content") if isinstance(reply.get("content"), str) else None,
            "tool_calls": [
                {
                    "id": item["id"],
                    "type": "function",
                    "function": {
                        "name": fn.get("name", ""),
                        "arguments": fn["arguments"],
                    },
                }
            ],
        }
        # Required by providers that use reasoning with tools. It stays only in
        # this in-memory protocol history, never in tool events or UI messages.
        if isinstance(reply.get("reasoning_content"), str):
            clean["reasoning_content"] = reply["reasoning_content"]
        messages.append(clean)
        try:
            result = call(
                fn.get("name", ""),
                fn["arguments"],
                lambda: registry.invoke(fn.get("name", ""), fn["arguments"]),
            )
        except ValueError:
            result = {
                "error": "工具参数或前置条件不满足，请检查工具 schema 和此前结果。"
            }
        messages.append(
            {"role": "tool", "tool_call_id": item["id"], "content": canonical(result)}
        )
        if isinstance(result, dict) and result.get("_terminal"):
            return result
    raise LlmError("AGENT_STEP_LIMIT: 未在限定步骤内提交有效决策，请复核后重试。")
