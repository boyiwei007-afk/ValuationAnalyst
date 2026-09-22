"""One export path for both interfaces; research reports contain no invented valuation."""
import html
import json


def build_research_export(service, session_id, format="json"):
    snapshot = service.snapshot(session_id)
    snapshot["report_kind"] = "research_preparation"
    snapshot["financial_model_status"] = "not_connected"
    if format == "json":
        return json.dumps(snapshot, ensure_ascii=False, indent=2), "application/json"
    if format != "html":
        raise ValueError("当前支持 JSON 复核包和 HTML 研究报告。")
    session = snapshot["session"]
    esc = lambda value: html.escape(str(value))
    rows = "".join("<tr>" + "".join(f"<td>{esc(f[key])}</td>" for key in
        ("metric", "raw_value", "unit", "period", "scope", "status", "quote", "block_id")) + "</tr>" for f in session["facts"])
    documents = "".join(
        "<tr>" + "".join(f"<td>{esc(value)}</td>" for value in (
            item["name"], item["role"], item["block_count"], item.get("size_bytes", 0),
            item.get("sha256", ""), "; ".join(item.get("warnings", [])),
        )) + "</tr>" for item in session["documents"]
    )
    memory = "".join(
        "<tr>" + "".join(f"<td>{esc(item.get(key, ''))}</td>" for key in
        ("key", "kind", "content", "source_message_id", "updated_at")) + "</tr>"
        for item in session.get("memory", [])
    )
    trail = "".join(
        "<tr>" + "".join(f"<td>{esc(value)}</td>" for value in (
            event["sequence"], event["type"], event.get("tool") or "",
            event["status"], event.get("duration_ms") or "", event["summary"],
        )) + "</tr>" for event in snapshot["events"]
    )
    messages = "".join(f"<article><b>{esc(m['role'])}</b><p>{esc(m['content'])}</p></article>" for m in snapshot["messages"])
    issue = esc(json.dumps(session.get("last_issue"), ensure_ascii=False, indent=2)) if session.get("last_issue") else "无 / None"
    content = f"""<!doctype html><html lang="{esc(session['language'])}"><meta charset="utf-8">
<title>ValuationAgent · Research report</title><style>
body{{font:15px/1.7 'Segoe UI','Microsoft YaHei',sans-serif;color:#173047;max-width:1120px;margin:40px auto;padding:24px}}
h1{{color:#087f75}} table{{border-collapse:collapse;width:100%;font-size:12px}}td,th{{border:1px solid #d7e1e8;padding:8px;text-align:left;overflow-wrap:anywhere}}p{{white-space:pre-wrap}}article{{border-top:1px solid #e2e8f0;padding:12px 0}}.note{{background:#eaf6f3;padding:16px}}@media print{{body{{margin:0;padding:8px}}tr{{break-inside:avoid}}}}
</style><h1>ValuationAgent · 研究资料报告 / Research preparation</h1>
<p class="note">正式金融模型未接入，本报告不包含正式估值。Financial model not connected; no valuation is issued.</p>
<p>{esc(session['session_id'])} · v{session['revision']} · {esc(session['updated_at'])}</p>
<p>Agent {esc(session.get('agent_protocol_version', ''))} · Prompt {esc(session.get('prompt_version', ''))} · Model {esc(session.get('model_provider', ''))}/{esc(session.get('model_name', ''))}</p>
<h2>研究范围 / Scope</h2><pre>{esc(json.dumps(session['draft'], ensure_ascii=False, indent=2))}</pre>
<h2>资料清单与哈希 / Source manifest</h2><table><thead><tr><th>文件</th><th>用途</th><th>原文块</th><th>字节</th><th>SHA-256</th><th>警告</th></tr></thead><tbody>{documents}</tbody></table>
<h2>字段与原文 / Facts & sources</h2><table><thead><tr><th>字段</th><th>原值</th><th>单位</th><th>期间</th><th>口径</th><th>状态</th><th>原文</th><th>来源 ID</th></tr></thead><tbody>{rows}</tbody></table>
<h2>缺口 / Gaps</h2><p>{esc(chr(10).join(session['gaps']))}</p>
<h2>长期上下文 / Durable context</h2><table><thead><tr><th>键</th><th>类型</th><th>内容</th><th>来源消息</th><th>更新时间</th></tr></thead><tbody>{memory}</tbody></table>
<h2>最近异常 / Latest issue</h2><pre>{issue}</pre>
<h2>执行审计 / Execution trail</h2><table><thead><tr><th>#</th><th>事件</th><th>工具</th><th>状态</th><th>耗时 ms</th><th>摘要</th></tr></thead><tbody>{trail}</tbody></table>
<h2>研究记录 / Conversation</h2>{messages}</html>"""
    return content, "text/html"
