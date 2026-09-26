from __future__ import annotations
import json
from typing import Callable
from pydantic import ValidationError
from valuationagent.core.tools import ToolRegistry, canonical
from valuationagent.llm.client import LlmError


def run_tool_loop(
    llm, messages: list[dict], registry: ToolRegistry, call: Callable, *, max_rounds=6, max_tokens=900, check_cancel=None
):
    """Finite, auditable tool loop with bounded self-correction.

    Free text is never interpreted as a command or valuation. Tool failures are
    returned to the model as safe structured feedback; repeated no-progress
    failures stop and let the application ask the user how to recover.
    """
    messages = list(messages)
    trace = {"rounds": 0, "tools": [], "tool_errors": 0, "protocol_errors": 0}
    failed_signatures: dict[str, int] = {}
    last_failure = ""
    for round_index in range(1, max_rounds + 1):
        if check_cancel:
            check_cancel()
        trace["rounds"] = round_index
        reply = llm.chat(messages, tools=registry.schemas(), tool_choice="required", max_tokens=max_tokens)
        if check_cancel:
            check_cancel()
        if not isinstance(reply, dict):
            raise LlmError("TOOL_RESPONSE_INVALID: 模型未返回有效的工具调用消息。")
        calls = reply.get("tool_calls") or []
        if not isinstance(calls, list):
            raise LlmError("TOOL_RESPONSE_INVALID: 工具调用列表格式不正确。")
        if len(calls) != 1:
            trace["protocol_errors"] += 1
            messages.append(
                {
                    "role": "user",
                    "content": "协议错误：本轮必须且只能调用一个已注册工具。请根据工具 schema 重新提交；纯文本不能改变任务或产生正式结果。",
                }
            )
            continue
        item = calls[0]
        if not isinstance(item, dict) or not isinstance(item.get("function"), dict):
            raise LlmError("TOOL_RESPONSE_INVALID: 工具调用结构不完整。")
        fn = item["function"]
        if (
            not isinstance(item.get("id"), str) or not item["id"].strip()
            or not isinstance(fn.get("name"), str) or not fn["name"].strip()
            or not isinstance(fn.get("arguments"), str)
        ):
            raise LlmError("TOOL_RESPONSE_INVALID: 工具调用缺少 id、名称或 JSON 参数。")
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
        tool_name = fn.get("name", "")
        trace["tools"].append(tool_name)
        try:
            result = call(
                tool_name,
                fn["arguments"],
                lambda: registry.invoke(tool_name, fn["arguments"]),
            )
        except ValidationError as exc:
            trace["tool_errors"] += 1
            fields = [".".join(str(part) for part in error["loc"]) for error in exc.errors(include_input=False)]
            details = [
                f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
                for error in exc.errors(include_input=False)
            ]
            last_failure = "；".join(details[:8])
            result = {
                "ok": False,
                "error": {
                    "code": "TOOL_ARGUMENTS_INVALID",
                    "message": "工具参数不符合 schema，请按枚举和值域修正后重试。" + (
                        " 具体错误：" + last_failure if last_failure else ""
                    ),
                    "fields": fields[:12],
                    "recoverable": True,
                },
            }
        except ValueError as exc:
            trace["tool_errors"] += 1
            message = str(exc).strip()[:1000] or "工具前置条件不满足。"
            last_failure = message
            result = {
                "ok": False,
                "error": {
                    "code": "TOOL_PRECONDITION_FAILED",
                    "message": message,
                    "recoverable": True,
                },
            }
        messages.append(
            {"role": "tool", "tool_call_id": item["id"], "content": canonical(result)}
        )
        if isinstance(result, dict) and result.get("_terminal"):
            terminal = dict(result)
            terminal["_agent_trace"] = trace
            return terminal
        if isinstance(result, dict) and result.get("ok") is False:
            try:
                signature = tool_name + ":" + canonical(json.loads(fn["arguments"]))
            except (TypeError, ValueError):
                signature = tool_name + ":invalid-json"
            failed_signatures[signature] = failed_signatures.get(signature, 0) + 1
            if failed_signatures[signature] >= 2:
                raise LlmError(
                    "AGENT_NO_PROGRESS: 同一工具调用连续失败，已停止自动重试并保留当前进度。"
                    + (" 最近一次错误：" + last_failure if last_failure else "")
                )
    raise LlmError("AGENT_STEP_LIMIT: 未在限定步骤内提交有效决策，请复核后重试。")
