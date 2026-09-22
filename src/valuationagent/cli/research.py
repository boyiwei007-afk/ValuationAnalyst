"""Conversation-first entry; option IDs and free text use the same backend contract."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
import questionary
from rich.text import Text
from rich.live import Live
from rich.console import Group
from valuationagent.application.research import ResearchService
from valuationagent.application.research_export import build_research_export
from valuationagent.llm.client import LlmError, OpenAICompatibleClient
from valuationagent.schemas.models import ModelConnectionInput
from valuationagent.schemas.research import ResearchTurn
from valuationagent.cli.ui import console, panel, welcome


MODEL_CHOICES = [
    "deepseek-flash     ·  DeepSeek Flash",
    "deepseek-v4-pro    ·  DeepSeek V4 Pro",
    "gpt-4o-mini        ·  OpenAI GPT-4o mini",
    "__custom__  ·  自定义模型 / Custom model",
]
COMPANY_CHOICES = [
    "600519 贵州茅台",
    "000858 五粮液",
    "300750 宁德时代",
    "601318 中国平安",
    "000001 平安银行",
    "603893 瑞芯微",
]


def configure_model(language="zh-CN"):
    """Run the first-entry model setup without writing a secret to disk."""
    from valuationagent.cli.main import autocomplete, secret_input, select, text_input

    en = language == "en-US"
    choice = select(
        "How would you like to start?" if en else "开始前先配置研究模型吗？",
        [
            questionary.Choice("连接研究模型 / Connect a model", value="connect"),
            questionary.Choice("资料整理模式 / Local preparation", value="local"),
        ],
        language=language,
    )
    if choice != "connect":
        return None, ""

    provider = select(
        "Provider protocol" if en else "选择模型接口",
        [
            questionary.Choice("DeepSeek · OpenAI Compatible", value="deepseek"),
            questionary.Choice("OpenAI", value="openai"),
            questionary.Choice("其他 OpenAI Compatible 接口", value="custom"),
        ],
        language=language,
    )
    model_name = autocomplete(
        "Model name (type to filter; free input is allowed)"
        if en
        else "模型名称（可选择或直接输入，输入会自动补全）",
        MODEL_CHOICES,
    ).strip()
    if model_name.startswith("__custom__"):
        model_name = text_input("Custom model name" if en else "输入自定义模型名称").strip()
    elif "·" in model_name:
        model_name = model_name.split("·", 1)[0].strip()
    if not model_name:
        raise ValueError("模型名称不能为空。")

    company = autocomplete(
        "Company or A-share ticker (optional)" if en else "公司名或 A 股代码（可选，支持直接输入）",
        COMPANY_CHOICES,
    ).strip()
    defaults = {
        "deepseek": "https://api.deepseek.com",
        "openai": "https://api.openai.com/v1",
        "custom": "https://",
    }
    base_url = defaults[provider]
    if provider == "custom":
        base_url = text_input("Base URL" if en else "接口地址", default=base_url).strip()
    api_key = secret_input("API Key（只在本次会话使用，不会保存）" if not en else "API Key (session only; never saved)")
    if not api_key.strip():
        console.print(Text("未输入 API Key，继续资料整理模式。" if not en else "No API Key entered; continuing in local preparation mode.", style="muted"))
        return None, company
    config = ModelConnectionInput(
        provider="openai" if provider == "openai" else "openai_compatible",
        base_url=base_url,
        model=model_name,
        api_key=api_key.strip(),
        thinking="auto",
    )
    client = OpenAICompatibleClient(config)
    try:
        with console.status("Verifying model…" if en else "正在验证模型工具调用能力…"):
            client.test_connection()
    except LlmError as exc:
        console.print(panel(Text(str(exc), style="warn"), "Model setup" if en else "模型配置"))
        console.print(Text("可继续资料整理，之后输入 /connect 重试。" if not en else "You can continue locally and retry later with /connect.", style="muted"))
        return None, company
    console.print(Text(f"Model connected · {model_name}" if en else f"模型已连接 · {model_name}", style="good"))
    return client, company


def getting_started(language="zh-CN"):
    en = language == "en-US"
    body = Text()
    body.append("Start with a question or a file.\n" if en else "先说需求，或直接提供资料。\n", style="title")
    body.append("I want to research 600519 and identify missing financial data.\n" if en else "“我想研究 600519，先帮我看看需要哪些财务数据。”\n", style="accent")
    body.append("Upload annual reports, Excel tables or policy text.\n" if en else "上传年报、Excel 财务表或政策原文，先整理信息和来源。\n")
    body.append("/upload path  ·  /connect  ·  /prepare  ·  /help\n", style="muted")
    body.append("Choose an option when asked, or enter your own requirements." if en else "需要确认时可选答案，也可以输入自己的修改要求。", style="muted")
    return panel(body, "What can I do?" if en else "你可以这样开始")


def turn_with_display(service, session_id, payload, en=False):
    if not console.is_terminal:
        return service.turn(session_id, payload)
    cursor = max((e.sequence for e in service.store.list_events(session_id)), default=0)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(service.turn, session_id, payload)
        with Live(console=console, refresh_per_second=4, transient=True) as live:
            while not future.done():
                events = service.store.list_events(session_id, cursor)
                lines = [Text("Agent · Researching…" if en else "Agent · 正在理解需求与整理资料…", style="accent")]
                for event in events[-7:]:
                    label = f"{event.tool or event.type} · {event.status}"
                    if event.duration_ms is not None:
                        label += f" · {event.duration_ms} ms"
                    lines.append(Text(label, style="warn" if event.status == "failed" else "muted"))
                live.update(panel(Group(*lines), "Agent & tools" if en else "Agent 与工具"))
                time.sleep(.2)
        return future.result()


def launch_research(language=None, session_id=None):
    from valuationagent.cli.main import runtime, select, text_input, upload
    store, _ = runtime()
    service = ResearchService(store)
    is_new_session = session_id is None
    console.print(welcome(console.width, console.height))
    if session_id:
        session = store.get_research(session_id)
        language = session.language
    else:
        if language is None:
            language = select("Language / 语言", [questionary.Choice("简体中文", value="zh-CN"),
                                                    questionary.Choice("English", value="en-US")])
        session = service.create(language)
        session_id = session.session_id
    en = language == "en-US"
    console.print(getting_started(language))
    configured_company = ""
    if os.getenv("VALUATION_LLM_MODEL") and os.getenv("VALUATION_LLM_API_KEY"):
        service.attach(session_id, OpenAICompatibleClient.from_environment())
        console.print(Text("Live Agent · " + os.getenv("VALUATION_LLM_MODEL", ""), style="accent"))
    elif is_new_session:
        client, configured_company = configure_model(language)
        if client is not None:
            service.attach(session_id, client)
        else:
            console.print(Text("Local preparation · /connect to use your model" if en else "资料整理模式 · /connect 可重新配置模型", style="muted"))
    else:
        console.print(Text("Local preparation · /connect to use your model" if en else "资料整理模式 · 配置环境变量后 /connect 连接模型", style="muted"))
    seen_question = None
    try:
        while True:
            session = store.get_research(session_id)
            if session.question and seen_question != session.question.question_id:
                question = session.question
                if question.proposed_draft:
                    draft = question.proposed_draft
                    fields = [("Company" if en else "公司", draft.company or draft.ticker or ("Pending" if en else "待补充")),
                              ("Date" if en else "估值日", str(draft.valuation_date or ("Pending" if en else "待补充"))),
                              ("Methods" if en else "方法", " / ".join(draft.methods).upper() or ("Pending" if en else "待补充")),
                              ("Goal" if en else "目标", draft.objective)]
                    console.print(panel(Text("\n".join(f"{key}  {value}" for key, value in fields if value)), "Scope" if en else "待确认研究范围"))
                for fact in session.facts:
                    if fact.fact_id in question.fact_ids:
                        console.print(panel(Text(f"{fact.metric}: {fact.raw_value} {fact.unit} · {fact.period} · {fact.scope}\n{fact.quote}\n{fact.block_id}\n" + "; ".join(fact.warnings)), "Candidate" if en else "候选字段"))
                answer = select(question.title, [*[questionary.Choice(o.label, value=o.id) for o in question.options],
                    questionary.Choice("Enter your own requirements" if en else "输入其他要求 / 修改说明", value="__text"),
                    questionary.Choice("Keep chatting; decide later" if en else "暂不选择，继续对话", value="__skip")], language)
                seen_question = question.question_id
                if answer not in {"__text", "__skip"}:
                    payload = ResearchTurn(question_id=question.question_id, option_id=answer)
                elif answer == "__text":
                    content = text_input("Your requirements" if en else "补充或修改要求")
                    if not content.strip():
                        continue
                    payload = ResearchTurn(content=content, question_id=question.question_id)
                else:
                    continue
            else:
                default_prompt = ""
                if configured_company:
                    default_prompt = (
                        f"I want to research {configured_company}. First identify the historical data needed."
                        if en
                        else f"我想研究 {configured_company}，先帮我梳理需要哪些历史财务数据。"
                    )
                    configured_company = ""
                content = text_input("You" if en else "你", default=default_prompt).strip()
                if not content:
                    continue
                if content in {"/quit", "/exit"}:
                    break
                if content == "/help":
                    console.print(getting_started(language))
                    console.print(Text("/upload 路径 · /files · /memory · /tools · /confirm · /connect · /prepare\n/company 名称 · /date YYYY-MM-DD · /methods dcf,pe\n/export json|html · /wizard · /demo · /quit", style="muted"))
                    continue
                if content == "/confirm":
                    seen_question = None
                    continue
                if content == "/connect":
                    try:
                        if os.getenv("VALUATION_LLM_MODEL") and os.getenv("VALUATION_LLM_API_KEY"):
                            client = OpenAICompatibleClient.from_environment()
                            with console.status("Connecting…" if en else "正在检查模型连接…"):
                                client.test_connection()
                        else:
                            client, _ = configure_model(language)
                            if client is None:
                                continue
                        service.attach(session_id, client)
                        console.print(Text("Model connected" if en else "模型已连接，可继续自然语言研究。", style="good"))
                    except (LlmError, ValueError) as exc:
                        console.print(panel(Text(str(exc)), "Model"))
                    continue
                if content == "/wizard":
                    from valuationagent.cli.main import wizard
                    wizard(language=language)
                    continue
                if content == "/demo":
                    from valuationagent.cli.main import demo
                    demo(company="估值演示公司", valuation_date=None, chat=False, plain=True, language=language)
                    continue
                if content == "/files":
                    for doc in session.documents:
                        console.print(Text(f"{doc.name} · {doc.block_count} blocks · {doc.file_id}"))
                        for block in store.research_blocks(session_id, doc.file_id)[:3]:
                            console.print(panel(Text(block["text"]), block["block_id"]))
                    continue
                if content == "/tools":
                    for event in store.list_events(session_id):
                        if event.tool or event.type.startswith(("agent.", "intent.", "memory.", "security.")):
                            console.print(Text(f"{event.sequence} · {event.type} · {event.tool or '-'} · {event.duration_ms or 0} ms · {event.summary}"))
                    continue
                if content == "/memory":
                    if not session.memory:
                        console.print(Text("No durable context saved." if en else "尚未保存长期上下文。", style="muted"))
                    for item in session.memory:
                        console.print(panel(Text(f"{item.content}\n{item.source_message_id}", style="muted"), f"{item.kind} · {item.key}"))
                    continue
                if content.startswith("/export"):
                    format = content.partition(" ")[2].strip() or "json"
                    try:
                        body, _ = build_research_export(service, session_id, format)
                        directory = store.data_dir / "exports"
                        directory.mkdir(exist_ok=True)
                        target = directory / f"{session_id}-v{session.revision}.{format}"
                        target.write_text(body, encoding="utf-8")
                        console.print(Text(str(target.resolve()), style="good"))
                    except ValueError as exc:
                        console.print(panel(Text(str(exc)), "Export"))
                    continue
                if content.startswith("/upload "):
                    role = select("File role" if en else "文件用途", [
                        questionary.Choice("历史财务 / Financials", value="historical_financials"),
                        questionary.Choice("经营假设 / Assumptions", value="assumptions"),
                        questionary.Choice("可比公司 / Comparables", value="comparables"),
                        questionary.Choice("政策与其他证据 / Evidence", value="evidence")], language)
                    try:
                        file_id = upload(store, content[8:].strip(), role)
                    except (ValueError, OSError) as exc:
                        console.print(panel(Text(str(exc)), "File"))
                        continue
                    payload = ResearchTurn(content="请读取并整理这份资料，提取有关字段，保留来源和缺口。", file_ids=[file_id])
                else:
                    payload = ResearchTurn(content=content)
            result = turn_with_display(service, session_id, payload, en)
            console.print(panel(Text(result["messages"][-1]["content"]), "ValuationAgent"))
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        console.print(Text(f"valuationagent research --resume {session_id}", style="muted"))
