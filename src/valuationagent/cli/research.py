"""Conversation-first entry; option IDs and free text use the same backend contract."""
import os
import time
from concurrent.futures import ThreadPoolExecutor
import questionary
from rich.text import Text
from rich.live import Live
from rich.console import Group
from rich.table import Table
from valuationagent.application.research import ResearchService
from valuationagent.search.providers import (
    TavilySearchProvider,
    UnavailableSearchProvider,
    create_search_provider,
)
from valuationagent.finance.tools import FinanceResearchToolProvider
from valuationagent.core.data import LocalDataProvider
from valuationagent.market.tushare import TushareApiClient, TushareDataProvider
from valuationagent.application.research_export import build_research_export
from valuationagent.llm.client import LlmError, OpenAICompatibleClient
from valuationagent.schemas.models import ModelConnectionInput
from valuationagent.schemas.research import ResearchTurn
from valuationagent.cli.ui import console, panel, welcome


MODEL_CHOICES = {
    "deepseek": [
        ("DeepSeek Flash", "deepseek-flash"),
        ("DeepSeek V4 Pro", "deepseek-v4-pro"),
    ],
    "openai": [
        ("GPT-4o mini", "gpt-4o-mini"),
    ],
}
COMPANY_CHOICES = [
    "600519 贵州茅台",
    "000858 五粮液",
    "300750 宁德时代",
    "000333 美的集团",
    "600276 恒瑞医药",
    "603893 瑞芯微",
]


def _error_message(exc):
    """Validation diagnostics must not echo the rejected input or API key."""
    if hasattr(exc, "errors"):
        return "; ".join(item["msg"] for item in exc.errors())
    return str(exc)


def _make_turn(en=False, **values):
    try:
        return ResearchTurn(**values)
    except ValueError as exc:
        console.print(panel(Text(_error_message(exc), style="warn"), "Please check" if en else "请检查输入"))
        return None


def _model_choices(provider, en=False):
    """Return a compact, provider-aware model menu with custom input last."""
    choices = [
        questionary.Choice(f"{label:<20} {model_id}", value=model_id)
        for label, model_id in MODEL_CHOICES.get(provider, [])
    ]
    choices.append(
        questionary.Choice(
            "Custom model…" if en else "自定义模型…",
            value="__custom__",
        )
    )
    return choices


def _agent_choices(question, en=False):
    """Render model-generated choices and keep one consistent free-text exit."""
    choices = [questionary.Choice(option.label, value=option.id) for option in question.options]
    if question.kind in {"search_unavailable", "search_failed"}:
        choices.insert(
            0,
            questionary.Choice(
                "Connect Tavily Search" if en else "连接 Tavily 搜索",
                value="__search__",
            ),
        )
    choices.append(questionary.Choice("Chat", value="__chat__"))
    return choices


def configure_search(language="zh-CN"):
    """Create an in-memory Tavily provider; the key is never persisted."""
    from valuationagent.cli.main import secret_input

    en = language == "en-US"
    api_key = secret_input(
        "Tavily API Key (session only; never saved)"
        if en
        else "Tavily API Key（仅本次会话使用，不会保存）"
    ).strip()
    if not api_key:
        console.print(Text(
            "No key entered; search remains disconnected."
            if en else "未输入 Key，联网搜索仍保持未连接。",
            style="muted",
        ))
        return None
    return TavilySearchProvider(api_key)


def configure_search_on_start(language="zh-CN"):
    """Offer search setup before research so it never appears as a late surprise."""
    from valuationagent.cli.main import select

    en = language == "en-US"
    choice = select(
        "Configure web search now?" if en else "开始前配置联网搜索吗？",
        [
            questionary.Choice(
                "Connect Tavily Search" if en else "连接 Tavily 联网搜索",
                value="connect",
            ),
            questionary.Choice(
                "Set up later (/search)" if en else "稍后配置（/search）",
                value="later",
            ),
        ],
        language=language,
    )
    if choice != "connect":
        return None
    return configure_search(language)


def configure_market_data(language="zh-CN"):
    """Create a session-only Tushare provider without persisting its token."""
    from valuationagent.cli.main import secret_input

    en = language == "en-US"
    token = secret_input(
        "Tushare Token (session only; never saved)"
        if en else "Tushare Token（仅本次运行使用，不会保存）"
    ).strip()
    if not token:
        console.print(Text(
            "No token entered; A-share structured data remains disconnected."
            if en else "未输入 Token，A 股结构化取数仍保持未连接。",
            style="muted",
        ))
        return None
    return TushareDataProvider(TushareApiClient(token))


def configure_data_services_on_start(language, *, need_search, need_market):
    """Configure missing online services through one compact startup menu."""
    from valuationagent.cli.main import select

    en = language == "en-US"
    if need_search and need_market:
        choices = [
            questionary.Choice(
                "Tushare + Tavily (recommended)" if en else "Tushare + Tavily（推荐）",
                value="both",
            ),
            questionary.Choice("Tushare only" if en else "只连接 Tushare A 股取数", value="market"),
            questionary.Choice("Tavily only" if en else "只连接 Tavily 联网搜索", value="search"),
            questionary.Choice("Set up later" if en else "稍后配置", value="later"),
        ]
    elif need_market:
        choices = [
            questionary.Choice("Connect Tushare" if en else "连接 Tushare A 股取数", value="market"),
            questionary.Choice("Set up later (/market)" if en else "稍后配置（/market）", value="later"),
        ]
    else:
        choices = [
            questionary.Choice("Connect Tavily Search" if en else "连接 Tavily 联网搜索", value="search"),
            questionary.Choice("Set up later (/search)" if en else "稍后配置（/search）", value="later"),
        ]
    choice = select(
        "Configure online data services" if en else "配置在线数据服务",
        choices,
        language=language,
    )
    search = configure_search(language) if choice in {"both", "search"} else None
    market = configure_market_data(language) if choice in {"both", "market"} else None
    return search, market


def configure_model(language="zh-CN", include_company=True):
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
    model_name = select(
        "Model" if en else "选择模型",
        _model_choices(provider, en),
        language=language,
    )
    if model_name == "__custom__":
        model_name = text_input("Model name" if en else "模型名称").strip()
    if not model_name:
        raise ValueError("模型名称不能为空。")

    company = ""
    if include_company:
        company = autocomplete(
            "Company or A-share ticker" if en else "公司或 A 股代码",
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
    body.append("Start a valuation of 600519 and produce ranges, sensitivity analysis and a report.\n" if en else "“请对 600519 进行估值，给出估值区间、敏感性分析和报告。”\n", style="accent")
    body.append("Provide annual reports or let the Agent find sources, build the model and propose assumptions for review.\n" if en else "上传年报或选择联网获取；Agent 连续补齐模型输入，提出预测假设，整套方案确认后计算。\n")
    body.append("/upload path  ·  /connect  ·  /search  ·  /market  ·  /prepare  ·  /help\n", style="muted")
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
    store, runner = runtime()
    service = ResearchService(
        store,
        search_provider=create_search_provider(),
        tool_providers=(FinanceResearchToolProvider(),),
    )
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
    try:
        if os.getenv("VALUATION_LLM_MODEL") and os.getenv("VALUATION_LLM_API_KEY"):
            service.attach(session_id, OpenAICompatibleClient.from_environment())
            console.print(Text("Live Agent · " + os.getenv("VALUATION_LLM_MODEL", ""), style="accent"))
        elif is_new_session:
            client, configured_company = configure_model(language)
            if client is not None:
                service.attach(session_id, client)
            else:
                console.print(Text("Local preparation · /connect to use your model" if en else "资料整理模式 · /connect 可重新配置模型", style="muted"))
        elif console.is_terminal:
            client, _ = configure_model(language, include_company=False)
            if client is not None:
                service.attach(session_id, client)
            else:
                console.print(Text("Local preparation · /connect to use your model" if en else "资料整理模式 · /connect 可重新配置模型", style="muted"))
        else:
            console.print(Text("Local preparation · /connect to use your model" if en else "资料整理模式 · /connect 可重新配置模型", style="muted"))
    except (ValueError, LlmError) as exc:
        console.print(panel(Text(_error_message(exc), style="warn"), "Model setup" if en else "模型配置"))
        console.print(Text("Session preserved. Use /connect to retry." if en else "会话已保留，可以输入 /connect 重新配置。", style="muted"))
    # Data credentials are intentionally not persisted. Offer one compact setup
    # for both new and resumed terminal sessions.
    need_search = isinstance(service.search_provider, UnavailableSearchProvider)
    need_market = isinstance(runner.data, LocalDataProvider)
    if console.is_terminal and (need_search or need_market):
        try:
            search_provider, market_provider = configure_data_services_on_start(
                language, need_search=need_search, need_market=need_market
            )
            if search_provider is not None:
                service.attach_search(session_id, search_provider)
            if market_provider is not None:
                service.attach_market(session_id, market_provider)
        except ValueError as exc:
            console.print(panel(Text(_error_message(exc), style="warn"), "Data services" if en else "在线数据服务"))
    active_search = service._search_clients.get(session_id, service.search_provider)
    if isinstance(active_search, UnavailableSearchProvider):
        console.print(Text(
            "Web search · set up later with /search"
            if en else "联网搜索 · 稍后可用 /search 配置",
            style="muted",
        ))
    else:
        console.print(Text(
            f"Web search credential ready · {active_search.provider_id} · verified on first search"
            if en else f"联网搜索凭证已就绪 · {active_search.provider_id} · 首次检索时验证",
            style="good",
        ))
    market_status = service.data_service_status(session_id, default_market=runner.data)["market"]
    if not market_status["available"]:
        console.print(Text(
            "A-share data · set up later with /market"
            if en else "A 股结构化取数 · 稍后可用 /market 配置",
            style="muted",
        ))
    else:
        console.print(Text(
            f"A-share data credential ready · {market_status['provider']} · verified on first retrieval"
            if en else f"A 股取数凭证已就绪 · {market_status['provider']} · 首次取数时验证",
            style="good",
        ))
    seen_question = None
    try:
        while True:
            session = store.get_research(session_id)
            if session.question and seen_question != session.question.question_id:
                question = session.question
                if question.valuation_review:
                    plan = question.valuation_review
                    assumptions = plan.get("assumptions", {})
                    def percent(value):
                        return "—" if value is None else f"{float(value) * 100:.2f}%"
                    console.print(panel(Text(
                        f"{' / '.join(plan['methods']).upper()} · {plan['valuation_date']} · 基期 {plan['baseline_period']}\n"
                        f"WACC {percent(assumptions.get('wacc'))} / g {percent(assumptions.get('terminal_growth'))}\n"
                        + plan["forecast_rationale"] + "\n" + "；".join(plan["risks"])
                        + "\n预测是假设，不是历史事实。确认后直接计算；财务校验仍可能要求修正。"),
                        "Valuation plan" if en else "待确认估值方案"))
                    for key, title in (("revenue_growth_scenarios", "Revenue growth / 收入增长率"),
                                       ("ebit_margin_scenarios", "EBIT margin / 利润率")):
                        paths = assumptions.get(key)
                        if not paths:
                            continue
                        table = Table(title=title)
                        for column in ("Year / 年", "Pessimistic / 悲观", "Base / 基准", "Optimistic / 乐观"):
                            table.add_column(column)
                        for i in range(10):
                            table.add_row(str(i + 1), *(percent(paths[name][i]) for name in ("pessimistic", "base", "optimistic")))
                        console.print(table)
                if question.proposed_draft:
                    draft = question.proposed_draft
                    fields = [("Company" if en else "公司", draft.company or draft.ticker or ("Pending" if en else "待补充")),
                              ("Industry" if en else "行业", draft.industry),
                              ("Date" if en else "估值日", str(draft.valuation_date or ("Pending" if en else "待补充"))),
                              ("Methods" if en else "方法", " / ".join(draft.methods).upper() or ("Pending" if en else "待补充")),
                              ("Goal" if en else "目标", draft.objective)]
                    console.print(panel(Text("\n".join(f"{key}  {value}" for key, value in fields if value)), "Scope" if en else "待确认研究范围"))
                for fact in session.facts:
                    if fact.fact_id in question.fact_ids:
                        console.print(panel(Text(f"{fact.metric}: {fact.raw_value} {fact.unit} · {fact.period} · {fact.scope}\n{fact.quote}\n{fact.block_id}\n" + "; ".join(fact.warnings)), "Candidate" if en else "候选字段"))
                answer = select(question.title, _agent_choices(question, en), language)
                if answer == "__search__":
                    provider = configure_search(language)
                    if provider is None:
                        seen_question = None
                        continue
                    service.attach_search(session_id, provider)
                    console.print(Text(
                        "Tavily credential loaded · retrying the saved search"
                        if en else "Tavily 凭证已加载 · 正在重试刚才的检索",
                        style="good",
                    ))
                    payload = _make_turn(
                        en,
                        content=(
                            "Tavily search is connected. Retry the previous search request now."
                            if en else "Tavily 搜索已经连接，请立即重试刚才的检索请求。"
                        ),
                        question_id=question.question_id,
                    )
                    seen_question = question.question_id
                elif answer != "__chat__":
                    payload = _make_turn(en, question_id=question.question_id, option_id=answer)
                else:
                    content = text_input("Chat")
                    if not content.strip():
                        seen_question = None
                        continue
                    payload = _make_turn(en, content=content, question_id=question.question_id)
                seen_question = question.question_id
            else:
                default_prompt = ""
                if configured_company:
                    default_prompt = (
                        f"Start a valuation of {configured_company}: verify official data, propose suitable methods and assumptions for my review, and produce valuation ranges, sensitivity analysis and a report."
                        if en
                        else f"请对 {configured_company} 进行估值：核验官方数据，提出适用方法和预测假设让我确认，完成估值区间、敏感性分析和报告。"
                    )
                    configured_company = ""
                content = text_input("Chat", default=default_prompt).strip()
                if not content:
                    continue
                if content in {"/quit", "/exit"}:
                    break
                if content == "/help":
                    console.print(getting_started(language))
                    console.print(Text("/upload 路径 · /files · /memory · /tools · /confirm · /connect · /search · /market · /prepare · /valuation\n/company 名称 · /industry 行业 · /date YYYY-MM-DD · /methods dcf,pe,ps,ev_ebitda\n/export pdf|html|json · /wizard · /demo · /quit", style="muted"))
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
                            client, _ = configure_model(language, include_company=False)
                            if client is None:
                                continue
                        service.attach(session_id, client)
                        console.print(Text("Model connected" if en else "模型已连接，可继续自然语言研究。", style="good"))
                    except (LlmError, ValueError) as exc:
                        console.print(panel(Text(_error_message(exc)), "Model"))
                    continue
                if content == "/search":
                    try:
                        provider = configure_search(language)
                        if provider is None:
                            continue
                        service.attach_search(session_id, provider)
                        console.print(Text(
                            "Tavily credential loaded; the first search will verify it."
                            if en else "Tavily 凭证已加载，首次检索时自动验证。",
                            style="good",
                        ))
                    except ValueError as exc:
                        console.print(panel(Text(_error_message(exc)), "Search" if en else "联网搜索"))
                    continue
                if content == "/market":
                    try:
                        market_provider = configure_market_data(language)
                        if market_provider is None:
                            continue
                        service.attach_market(session_id, market_provider)
                        console.print(Text(
                            "Tushare credential loaded; the first retrieval will verify it."
                            if en else "Tushare 凭证已加载，首次正式取数时自动验证。",
                            style="good",
                        ))
                    except ValueError as exc:
                        console.print(panel(Text(_error_message(exc)), "A-share data" if en else "A 股取数"))
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
                        if isinstance(body, bytes):
                            target.write_bytes(body)
                        else:
                            target.write_text(body, encoding="utf-8")
                        console.print(Text(str(target.resolve()), style="good"))
                    except (ValueError, OSError) as exc:
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
                    payload = _make_turn(en, content=("Read and organize this document, extract relevant fields, and preserve sources and gaps." if en else "请读取并整理这份资料，提取有关字段，保留来源和缺口。"), file_ids=[file_id])
                else:
                    payload = _make_turn(en, content=content)
            if payload is None:
                seen_question = None
                continue
            try:
                result = turn_with_display(service, session_id, payload, en)
            except (ValueError, LlmError, OSError) as exc:
                console.print(panel(Text(_error_message(exc), style="warn"), "Please retry" if en else "请重试"))
                # Another interface may have answered the old question. Reload
                # the session on the next iteration rather than losing the CLI.
                seen_question = None
                continue
            console.print(panel(Text(result["messages"][-1]["content"]), "ValuationAgent"))
            if result.get("action", {}).get("type") == "submit_valuation":
                try:
                    record = service.submit_valuation(session_id, runner)
                    with console.status("Running valuation…" if en else "正在执行正式估值流水线…"):
                        record = runner.execute(record.run_id)
                    if record.result is None:
                        message = (record.error or {}).get("message") or (record.review or {}).get("message") or str(record.status)
                        raise ValueError(message)
                    console.print(panel(Text(record.result.executive_summary), "Valuation result" if en else "估值结果"))
                    console.print(Text(
                        (f"Export: valuationagent export {record.run_id} -o report.xlsx / report.pdf" if en else
                         f"导出：valuationagent export {record.run_id} -o report.xlsx（或 report.pdf）"),
                        style="muted",
                    ))
                except (ValueError, OSError) as exc:
                    console.print(panel(Text(_error_message(exc), style="warn"), "Valuation" if en else "提交估值"))
    except (KeyboardInterrupt, EOFError):
        pass
    finally:
        console.print(Text(f"valuationagent research --resume {session_id}", style="muted"))
