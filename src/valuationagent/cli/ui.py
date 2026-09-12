from __future__ import annotations
import time
from concurrent.futures import ThreadPoolExecutor
from rich import box
from rich.align import Align
from rich.console import Console, Group
from rich.panel import Panel
from rich.table import Table
from rich.text import Text
from rich.live import Live
from rich.progress_bar import ProgressBar
from rich.theme import Theme
from valuationagent.workflow.graph import STAGES
from valuationagent.core.i18n import translator

THEME = Theme(
    {
        "accent": "#5EEAD4",
        "title": "bold #E2E8F0",
        "muted": "#94A3B8",
        "good": "#6EE7B7",
        "warn": "#FBBF24",
        "bad": "#FB7185",
        "border": "#33465F",
    }
)
console = Console(theme=THEME, highlight=False)
STATUS = {
    "created": ("待执行", "muted"),
    "running": ("执行中", "accent"),
    "waiting_review": ("待复核", "warn"),
    "completed": ("已完成", "good"),
    "completed_with_warnings": ("完成 · 有提示", "warn"),
    "failed": ("执行失败", "bad"),
    "cached": ("已复用", "accent"),
    "cancelled": ("已取消", "muted"),
}
TOOL_LABELS = {
    "resolve_financial_input": "读取资料与来源",
    "inspect_financials": "Agent 检查财务",
    "inspect_comparables": "Agent 检查同业",
    "continue_valuation": "Agent 提交执行",
    "validate_financials": "审核财务规则",
    "resolve_assumptions": "形成经营假设",
    "forecast_financials": "预测自由现金流",
    "calculate_dcf": "计算 DCF 估值",
    "calculate_relative_valuation": "计算相对估值",
    "run_sensitivity": "重算敏感性网格",
    "reconcile_valuations": "比较估值区间",
}
BACKGROUND = "#E2E8F0 on #101B2D"


def panel(body, title="", **kwargs):
    return Panel(
        body,
        title=Text(title, style="muted"),
        border_style="border",
        style=BACKGROUND,
        padding=(1, 2),
        box=box.ROUNDED,
        safe_box=False,
        **kwargs,
    )


def banner(language="zh-CN"):
    _ = translator(language)
    title = Text()
    title.append("V / A  ", style="bold #5EEAD4")
    title.append("VALUATION AGENT", style="title")
    title.append("   " + _("估值研究工作台") + "\n", style="muted")
    title.append(_("资料与依据  /  经营假设  /  估值与复核"), style="muted")
    return panel(title)


# A small original terminal wordmark; no font downloads or extra dependencies.
_GLYPHS = {
    "V": ("█   █", "█   █", "█   █", " █ █ ", "  █  "),
    "A": (" ███ ", "█   █", "█████", "█   █", "█   █"),
    "L": ("█    ", "█    ", "█    ", "█    ", "█████"),
    "U": ("█   █", "█   █", "█   █", "█   █", " ███ "),
    "T": ("█████", "  █  ", "  █  ", "  █  ", "  █  "),
    "I": ("█████", "  █  ", "  █  ", "  █  ", "█████"),
    "O": (" ███ ", "█   █", "█   █", "█   █", " ███ "),
    "N": ("█   █", "██  █", "█ █ █", "█  ██", "█   █"),
    "G": (" ███ ", "█    ", "█ ███", "█   █", " ███ "),
    "E": ("█████", "█    ", "████ ", "█    ", "█████"),
}


def _wordmark(words):
    lines = []
    for row in range(5):
        line = Text(no_wrap=True)
        for index, (word, color) in enumerate(words):
            if index:
                line.append("   ")
            line.append(" ".join(_GLYPHS[c][row] for c in word), style=color)
        lines.append(line)
    return Group(*lines)


def welcome(width=116, height=40):
    """Entrance screen, sized independently of the compact execution dashboard."""
    width = min(width, 116)
    teal, blue = "bold #5EEAD4", "bold #60A5FA"
    compact = height < (30 if width >= 94 else 36)
    if width >= 94 and not compact:
        logo = _wordmark([("VALUATION", teal), ("AGENT", blue)])
    elif width >= 62 and not compact:
        logo = Group(
            Align.center(_wordmark([("VALUATION", teal)])),
            Text(""),
            Align.center(_wordmark([("AGENT", blue)])),
        )
    else:
        logo = Text("ValuationAgent", style=teal, justify="center")
    intro = Text(
        "Agent-powered financial modeling & valuation research",
        style="title",
        justify="center",
    )
    tagline = Text(
        "Traceable data. Explicit assumptions. Reproducible valuations.",
        style="muted",
        justify="center",
    )
    steps = [
        ("I", "Data & Evidence"),
        ("II", "Financial Review"),
        ("III", "Assumptions & Forecast"),
        ("IV", "DCF & Multiples"),
        ("V", "Sensitivity & Validation"),
        ("VI", "Report & Dialogue"),
    ]
    flow = Text()
    per_line = 3 if width >= 100 else 2 if width >= 76 else 1
    for index, (numeral, label) in enumerate(steps):
        if index:
            flow.append("\n" if index % per_line == 0 else "  →  ", style="muted")
        flow.append(numeral + ". ", style="bold #60A5FA")
        flow.append(label, style="#CBD5E1")
    body = Group(
        Text(""),
        Align.center(logo),
        Text(""),
        intro,
        tagline,
        Text(""),
        Text("WORKFLOW", style="bold #5EEAD4"),
        flow,
        Text(""),
    )
    if compact:
        body = Group(
            Align.center(logo),
            Text(""),
            intro,
            Text(""),
            Text("WORKFLOW", style="bold #5EEAD4"),
            flow,
        )
    return Align.center(
        Panel(
            body,
            width=width,
            title=Text(" Welcome to ValuationAgent ", style=teal),
            subtitle=Text(" FINANCIAL MODELING  /  VALUATION RESEARCH ", style="muted"),
            border_style="#299A91",
            style=BACKGROUND,
            padding=(1, 3),
            box=box.ROUNDED,
            safe_box=False,
        )
    )


def number(value):
    return "—" if value is None else f"{value:,.2f}"


def result_view(record):
    _ = translator(record.request.language)
    if not record.result:
        info = (record.review or record.error or {}).get(
            "message", _("任务尚未形成结果。")
        )
        return panel(Text(info, style="warn"), _("需要处理"))
    result = record.result
    table = Table(
        box=box.SIMPLE_HEAD,
        expand=True,
        header_style="muted",
        show_edge=False,
        padding=(0, 1),
    )
    table.add_column(_("估值方法"))
    table.add_column(_("基准 / 股"), justify="right", style="accent")
    table.add_column(_("区间 / 股"), justify="right")
    if result.dcf:
        d = result.dcf
        table.add_row(
            _("DCF · 现金流折现"),
            number(d.per_share_value),
            f"{number(d.range_low)} — {number(d.range_high)}",
        )
    for r in result.relative:
        table.add_row(
            _({"pe": "P/E · 市盈率", "ev_ebitda": "EV/EBITDA"}.get(r.method, r.method)),
            number(r.per_share_value),
            f"{number(r.range_low)} — {number(r.range_high)}"
            if r.status == "success"
            else r.reason,
        )
    caption = Text(
        f"{result.currency}  ·  {result.valuation_date}  ·  v{record.revision}  ·  {result.mode.upper()}\n",
        style="muted",
    )
    caption.append(result.reconciliation.conclusion, style="muted")
    if result.warnings:
        caption.append("\n" + "；".join(result.warnings), style="warn")
    caption.append(_("\n参考模型，待金融团队核准。"), style="muted")
    return panel(Group(table, caption), _("估值结果"))


def dashboard(record, events, elapsed, width=110, height=40):
    _ = translator(record.request.language)
    statuses = {stage: "created" for stage, _ in STAGES}
    cached = set()
    for event in events:
        if event.type.startswith("stage.") and event.stage in statuses:
            statuses[event.stage] = event.status or "created"
        if event.type == "tool.cached":
            cached.add(event.stage)
    done = sum(v == "completed" for v in statuses.values())
    completed_calls = sum(e.type == "tool.completed" for e in events)
    cached_calls = sum(e.type == "tool.cached" for e in events)
    state, color = STATUS.get(record.status, (record.status, "muted"))
    company = (
        record.request.company.name or record.request.company.ticker or _("未命名公司")
    )
    head = Text()
    head.append("V / A   ", style="bold #5EEAD4")
    head.append(company, style="title")
    head.append(f"    {_(state)}", style=color)
    head.append(
        f"\n{record.request.valuation_date}  ·  {record.request.mode.upper()}  ·  v{record.revision}",
        style="muted",
    )
    progress = Table.grid(expand=True)
    progress.add_column(ratio=1)
    progress.add_column(width=17, justify="right")
    progress.add_row(
        ProgressBar(
            total=len(STAGES),
            completed=done,
            width=None,
            complete_style="#299A91",
            finished_style="#5EEAD4",
        ),
        Text(f"{done:02d} / {len(STAGES)}   {elapsed:4.1f}s", style="muted"),
    )
    stage_table = Table.grid(padding=(0, 1), expand=True)
    stage_table.add_column(width=2)
    stage_table.add_column(ratio=1)
    stage_table.add_column(justify="right")
    compact = height < 32 or width < 88
    show = STAGES
    if compact:
        index = next(
            (
                i
                for i, (stage, _) in enumerate(STAGES)
                if statuses[stage] != "completed"
            ),
            len(STAGES) - 1,
        )
        show = STAGES[max(0, index - 1) : index + 2]
    for index, (stage, label) in enumerate(STAGES):
        if (stage, label) not in show:
            continue
        status = statuses[stage]
        title, style = STATUS.get(status, (status, "muted"))
        if stage in cached and status == "completed":
            title, style = "复用", "accent"
        marker = "●" if status == "running" else "✓" if status == "completed" else "!"
        if status == "created":
            marker = "·"
        stage_table.add_row(
            Text(marker, style=style),
            Text(_(label), style="title" if status == "running" else "muted"),
            Text(_(title), style=style),
        )
    feed = []
    settled = {
        e.tool_call_id for e in events if e.type in ("tool.completed", "tool.failed")
    }
    relevant = [
        e
        for e in events
        if e.type
        in (
            "conversation.message",
            "tool.started",
            "tool.completed",
            "tool.cached",
            "review.required",
        )
        and not (e.type == "tool.started" and e.tool_call_id in settled)
    ]
    for event in relevant[-(3 if compact else 5) :]:
        label = (
            "Agent"
            if event.type == "conversation.message"
            else "复核"
            if event.type == "review.required"
            else "Tool"
        )
        line = Text(_(label) + "  ", style="accent" if label != "复核" else "warn")
        content = event.summary
        if event.type.startswith("tool."):
            content = (
                _(TOOL_LABELS.get(event.tool, event.tool or ""))
                + " · "
                + _(
                    {
                        "tool.started": "执行中",
                        "tool.completed": "完成",
                        "tool.cached": "复用",
                    }[event.type]
                )
            )
        elif event.stage == "reporting":
            content = _("估值已完成，详见下方区间、依据与提示。")
        line.append(content[:100] + ("…" if len(content) > 100 else ""), style="muted")
        feed.append(line)
    left = panel(stage_table, _("工作流"))
    right = panel(
        Group(*(feed or [Text(_("等待执行事件…"), style="muted")])), _("Agent 与工具")
    )
    if compact:
        middle = Group(left, right)
    else:
        middle = Table.grid(expand=True, padding=(0, 1))
        middle.add_column(ratio=2, min_width=28)
        middle.add_column(ratio=3)
        middle.add_row(left, right)
    hint = (
        "Ctrl+C 请求暂停（当前工具结束后）"
        if record.status == "running"
        else "/tools 查看明细 · /help 对话帮助"
    )
    footer = Text(
        _(
            "工具完成 {done}   ·   复用 {cached}   ·   事件 {events}   ·   {hint}"
        ).format(
            done=completed_calls, cached=cached_calls, events=len(events), hint=_(hint)
        ),
        style="muted",
    )
    return Group(panel(head), progress, middle, footer)


def execute_with_display(runner, run_id, *, plain=False):
    started = time.perf_counter()
    if plain or not console.is_terminal:
        record = runner.execute(run_id)
        console.print(result_view(record))
        return record
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.execute, run_id)
        with Live(console=console, refresh_per_second=8, transient=True) as live:
            while not future.done():
                try:
                    record = runner.store.get_run(run_id)
                    live.update(
                        dashboard(
                            record,
                            runner.store.list_events(run_id),
                            time.perf_counter() - started,
                            console.width,
                            console.height,
                        )
                    )
                    time.sleep(0.08)
                except KeyboardInterrupt:
                    runner.request_pause(run_id)
                    live.update(
                        panel(
                            Text(
                                translator(record.request.language)(
                                    "已请求暂停，等待当前工具结束…"
                                ),
                                style="warn",
                            )
                        )
                    )
            record = future.result()
    console.print(
        dashboard(
            record,
            runner.store.list_events(run_id),
            time.perf_counter() - started,
            console.width,
            console.height,
        )
    )
    console.print(result_view(record))
    return record


def show_events(store, run_id):
    _ = translator(store.get_run(run_id).request.language)
    table = Table(box=box.SIMPLE_HEAD, header_style="muted", expand=True)
    for title in ("序号", "步骤 / 工具", "状态", "耗时"):
        table.add_column(_(title))
    for e in store.list_events(run_id):
        if e.type.startswith("tool.") or e.type == "review.required":
            table.add_row(
                str(e.sequence),
                Text(e.tool or e.stage or ""),
                Text(e.status or ""),
                f"{e.duration_ms} ms" if e.duration_ms is not None else "—",
            )
    console.print(panel(table, _("执行记录")))


def converse_with_display(runner, run_id, content, *, plain=False):
    if plain or not console.is_terminal:
        return runner.converse(run_id, content)
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(runner.converse, run_id, content)
        with Live(console=console, refresh_per_second=8, transient=True) as live:
            while not future.done():
                try:
                    records = runner.store.revisions(run_id)
                    record = records[-1]
                    live.update(
                        dashboard(
                            record,
                            runner.store.list_events(record.run_id),
                            time.perf_counter() - started,
                            console.width,
                            console.height,
                        )
                    )
                    time.sleep(0.08)
                except KeyboardInterrupt:
                    runner.request_pause(record.run_id)
                    live.update(
                        panel(
                            Text(
                                translator(record.request.language)(
                                    "已请求暂停，等待当前工具结束…"
                                ),
                                style="warn",
                            )
                        )
                    )
            return future.result()
