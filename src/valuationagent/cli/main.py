from __future__ import annotations
import json
import os
import sys
from datetime import date
from pathlib import Path
import questionary
import typer
from rich.text import Text
from valuationagent.core.i18n import translator
from valuationagent.application.runner import ValuationRunner
from valuationagent.finance.reference import ReferenceFinancialModel
from valuationagent.llm.client import LlmError, OpenAICompatibleClient
from valuationagent.schemas.models import Language, RevisionInput, ValuationRequest
from valuationagent.storage.sqlite import SQLiteRunStore
from valuationagent.cli.ui import (
    console,
    banner,
    welcome,
    panel,
    result_view,
    execute_with_display,
    show_events,
)

app = typer.Typer(
    invoke_without_command=True,
    no_args_is_help=False,
    pretty_exceptions_enable=False,
    help="ValuationAgent · 对话式估值研究工作台",
    rich_markup_mode="rich",
)
STYLE = questionary.Style(
    [
        ("qmark", "fg:#5eead4 bold"),
        ("question", "bold"),
        ("answer", "fg:#5eead4"),
        ("pointer", "fg:#5eead4 bold"),
        ("highlighted", "fg:#5eead4 bold"),
        ("selected", "fg:#5eead4"),
        ("instruction", "fg:#94a3b8"),
    ]
)


def runtime():
    store = SQLiteRunStore(Path(os.getenv("VALUATION_DATA_DIR", "var")))
    return store, ValuationRunner(store, ReferenceFinancialModel())


def model(live):
    return OpenAICompatibleClient.from_environment() if live else None


def ask(prompt):
    value = prompt.unsafe_ask()
    if value is None:
        raise typer.Exit(130)
    return value


def select(title, choices, language="zh-CN"):
    return ask(
        questionary.select(
            title,
            choices=choices,
            style=STYLE,
            pointer="›",
            instruction=translator(language)("↑↓ 选择 · 回车确认"),
        )
    )


def text_input(title, default=""):
    return ask(questionary.text(title, default=default, style=STYLE))


def autocomplete(title, choices, default=""):
    """Select a suggested value while keeping free-form input available."""
    return ask(
        questionary.autocomplete(
            title,
            choices=choices,
            default=default,
            style=STYLE,
            instruction="↑↓ 选择建议 · 直接输入也可以",
        )
    )


def secret_input(title):
    """Read a secret without echoing it to the terminal or storing it."""
    return ask(questionary.password(title, style=STYLE, instruction="输入后不会回显"))


def read_request(path):
    return ValuationRequest.model_validate_json(
        Path(path).read_text(encoding="utf-8-sig")
    )


def friendly_error(exc, language="zh-CN"):
    if hasattr(exc, "errors"):
        message = "；".join(
            ".".join(str(p) for p in e["loc"]) + ": " + e["msg"] for e in exc.errors()
        )
    else:
        message = str(exc)
    console.print(panel(Text(message, style="warn"), translator(language)("请检查")))
    raise typer.Exit(2)


def finish(record):
    _ = translator(record.request.language)
    console.print(
        Text(
            _("任务 {run_id}  ·  v{revision}").format(
                run_id=record.run_id, revision=record.revision
            ),
            style="muted",
        )
    )


@app.callback()
def main(ctx: typer.Context):
    if ctx.invoked_subcommand is None:
        if sys.stdin.isatty():
            interactive(language=None)
        else:
            console.print(ctx.get_help())


@app.command()
def demo(
    company: str = typer.Option("估值演示公司"),
    valuation_date: str = typer.Option(None),
    chat: bool = typer.Option(False, "--chat", help="完成后进入对话"),
    plain: bool = typer.Option(False, "--plain", help="静态输出，适合日志"),
    language: Language = typer.Option(
        Language.ZH_CN, "--language", help="对话与结果语言 / Session language"
    ),
):
    """使用明确标记的合成数据体验完整流程。"""
    try:
        store, runner = runtime()
        req = ValuationRequest(
            company={
                "name": translator(language)(company)
                if company == "估值演示公司"
                else company
            },
            valuation_date=valuation_date or date.today(),
            mode="demo",
            language=language,
        )
        record = runner.create_run(req)
        console.print(banner(req.language))
        record = execute_with_display(runner, record.run_id, plain=plain)
        finish(record)
        if chat:
            conversation(runner, record.run_id, plain=plain)
        elif record.status in ("waiting_review", "failed"):
            raise typer.Exit(1)
    except (ValueError, OSError, LlmError) as exc:
        friendly_error(exc)


@app.command("run")
def run_request(
    request_file: Path = typer.Argument(..., exists=True, dir_okay=False),
    live: bool = typer.Option(False, "--live"),
    chat: bool = typer.Option(False, "--chat"),
    plain: bool = typer.Option(False, "--plain"),
    language: Language | None = typer.Option(
        None, "--language", help="覆盖请求中的语言 / Override request language"
    ),
):
    """运行结构化请求文件，可接入模型并持续对话。"""
    try:
        store, runner = runtime()
        req = read_request(request_file)
        if live:
            req = req.model_copy(update={"mode": "live"})
        if language is not None:
            req = req.model_copy(update={"language": language.value})
        record = runner.create_run(req, model(req.mode == "live"))
        console.print(banner(req.language))
        record = execute_with_display(runner, record.run_id, plain=plain)
        finish(record)
        if chat:
            conversation(runner, record.run_id, plain=plain)
        elif record.status in ("waiting_review", "failed"):
            raise typer.Exit(1)
    except (ValueError, OSError, LlmError) as exc:
        friendly_error(exc)


def upload(store, path, role):
    target = Path(path.strip('"'))
    if target.stat().st_size > 50 * 1024 * 1024:
        raise ValueError("文件不能超过50MB。")
    return store.save_upload(target.name, role, None, target.read_bytes())["file_id"]


@app.command("wizard")
def wizard(
    language: Language | None = typer.Option(
        None, "--language", help="预选语言；默认进入语言选择 / Preselect language"
    ),
):
    """分步选择公司、数据、假设和方法，随后进入对话工作台。"""
    if not sys.stdin.isatty():
        console.print(
            "交互向导需要终端。脚本中请使用 valuationagent demo 或 valuationagent run 文件。"
        )
        raise typer.Exit(2)
    try:
        console.print(welcome(console.width, console.height))
        console.print()
        if language is None:
            console.print(
                panel(
                    Text(
                        "选择向导、对话与结果的语言。\nChoose a language for setup, conversation and results.",
                        style="muted",
                    ),
                    "00 / Language · 语言",
                )
            )
            language = select(
                "Language / 语言",
                [
                    questionary.Choice("简体中文  /  Chinese", value="zh-CN"),
                    questionary.Choice("English   /  英语", value="en-US"),
                ],
                language="en-US",
            )
        _ = translator(language)

        def choose(title, choices):
            return select(
                _(title),
                [questionary.Choice(_(label), value=value) for label, value in choices],
                language,
            )

        def prompt(title, default=""):
            return text_input(_(title), default)

        console.print(
            Text(
                _("↑ ↓ 选择  ·  Enter 确认  ·  Space 多选  ·  Ctrl+C 退出"),
                style="muted",
            )
        )
        store, runner = runtime()
        mode = choose(
            "01 / 运行模式",
            [
                ("合成数据体验     无需 API Key", "demo"),
                ("结构化估值       确定性模型，无需 API Key", "snapshot"),
                ("实时 Agent       使用环境变量中的模型", "live"),
            ],
        )
        base = {}
        if mode != "demo":
            source = choose(
                "02 / 历史财务来源",
                [
                    ("结构化请求 JSON", "structured"),
                    ("A 股代码 · 适配器待接入，可复核补数", "ticker"),
                    ("上传文档 · JSON 可解析，PDF/Excel 待接入", "upload"),
                ],
            )
            if source == "structured":
                base = read_request(
                    prompt("请求文件", "examples/structured_request.json")
                ).model_dump(mode="json")
            elif source == "ticker":
                base["company"] = {"ticker": prompt("A 股代码，例如 600519.SH")}
            else:
                base["file_ids"] = [
                    upload(store, prompt("财务文件路径"), "historical_financials")
                ]
            base["data_source"] = source
        base["mode"] = mode
        base["language"] = language
        company = base.get("company", {})
        company["name"] = prompt(
            "03 / 企业或项目名称", company.get("name") or _("估值演示公司")
        )
        base["company"] = company
        base["valuation_date"] = prompt("04 / 估值基准日 YYYY-MM-DD", str(date.today()))
        assumption_source = choose(
            "05 / 经营假设",
            [
                ("默认参考政策 · 用于框架测试", "automatic"),
                ("手工设定 WACC 与永续增长率", "manual"),
                ("上传假设文件 · JSON 可解析", "upload"),
            ],
        )
        base["assumption_source"] = assumption_source
        base["assumptions"] = {}
        base["assumption_file_ids"] = []
        if assumption_source == "manual":
            from decimal import Decimal

            base["assumptions"] = {
                "wacc": Decimal(prompt("WACC（%）", "9.5")) / 100,
                "terminal_growth": Decimal(prompt("永续增长率（%）", "3")) / 100,
            }
        elif assumption_source == "upload":
            base["assumption_file_ids"] = [
                upload(store, prompt("假设文件路径"), "assumptions")
            ]
        base["methods"] = ask(
            questionary.checkbox(
                _("06 / 估值方法"),
                choices=[
                    questionary.Choice(
                        _("DCF · 现金流折现"), value="dcf", checked=True
                    ),
                    questionary.Choice(_("P/E · 市盈率"), value="pe", checked=True),
                    questionary.Choice(
                        _("EV/EBITDA · 企业价值倍数"), value="ev_ebitda", checked=True
                    ),
                ],
                style=STYLE,
                instruction=_("↑↓ 移动 · 空格勾选 · 回车确认"),
            )
        )
        base["forecast_years"] = int(
            select(_("07 / 预测期"), ["5", "3", "7", "10"], language)
        )
        req = ValuationRequest.model_validate(base)
        console.print(
            panel(
                Text(
                    f"{company['name']}  ·  {req.valuation_date}  ·  {mode.upper()}\n"
                    + _("方法 {methods}  ·  预测 {years} 年").format(
                        methods=" / ".join(req.methods), years=req.forecast_years
                    )
                    + "\n"
                    + _("语言 {language}").format(language=req.language)
                    + "\n"
                    + _("运行后可继续提问、修改假设或处理复核。")
                ),
                _("准备执行"),
            )
        )
        record = runner.create_run(req, model(mode == "live"))
        record = execute_with_display(runner, record.run_id)
        finish(record)
        conversation(runner, record.run_id)
    except (KeyboardInterrupt, EOFError):
        console.print(
            Text(
                translator(language)("\n已退出。已创建的任务和记录会保留。"),
                style="muted",
            )
        )
    except (ValueError, OSError, LlmError) as exc:
        friendly_error(exc, language)


@app.command()
def interactive(language: Language | None = typer.Option(None, "--language")):
    """欢迎页后直接描述需求、上传资料，用选项或文字确认。"""
    if not sys.stdin.isatty():
        console.print("交互研究需要终端。脚本中请使用 valuationagent run 文件。")
        raise typer.Exit(2)
    from valuationagent.cli.research import launch_research
    launch_research(language=language)


@app.command("research")
def research_command(
    resume: str | None = typer.Option(None, "--resume"),
    language: Language | None = typer.Option(None, "--language"),
):
    """开始研究对话，或恢复已保存的研究会话。"""
    from valuationagent.cli.research import launch_research
    try:
        launch_research(language=language, session_id=resume)
    except (ValueError, KeyError, LlmError) as exc:
        friendly_error(exc)


HELP = """直接提问：本次用了哪些假设？  /  把 WACC 改为 8%
/result           查看当前结果
/assumptions      查看假设与来源
/tools            查看工具与耗时
/history          查看版本
/set wacc=8%      修改假设并创建新版本
/review 路径.json 提交更正文件（reason + changes），随后继续
/resume           从成功步骤的检查点继续
/help             查看帮助
/quit             退出，保留任务"""

HELP_EN = """Ask: What assumptions were used?  /  Set WACC to 8%
/result           Show current results
/assumptions      Show assumptions and sources
/tools            Show tools and timing
/history          Show revisions
/set wacc=8%      Revise assumptions and create a new version
/review path.json Submit corrections (reason + changes) and continue
/resume           Resume from completed tool checkpoints
/help             Show commands
/quit             Exit and preserve the run"""


def conversation(runner, run_id, plain=False):
    language = runner.store.get_run(run_id).request.language
    _ = translator(language)
    if not sys.stdin.isatty():
        console.print(Text(_("持续对话需要交互终端。"), style="warn"))
        return
    console.print(
        panel(
            Text(
                _(
                    "直接提问，或输入：把 WACC 改为 8%\n/help 查看命令  ·  /tools 工具明细  ·  /quit 保存并退出"
                ),
                style="muted",
            ),
            _("对话已就绪"),
        )
    )
    while True:
        try:
            language = runner.store.get_run(run_id).request.language
            _ = translator(language)
            content = console.input("[accent]" + _("你 › ") + "[/]").strip()
            if not content:
                continue
            if content in ("/quit", "/exit", "退出"):
                break
            if content == "/help":
                console.print(
                    panel(Text(HELP_EN if language == "en-US" else HELP), _("帮助"))
                )
                continue
            if content == "/result":
                console.print(result_view(runner.store.get_run(run_id)))
                continue
            if content == "/tools":
                show_events(runner.store, run_id)
                continue
            if content == "/history":
                for r in runner.store.revisions(run_id):
                    console.print(
                        Text(f"v{r.revision}  {r.status}  {r.run_id}", style="muted")
                    )
                continue
            if content == "/resume":
                record = execute_with_display(runner, run_id, plain=plain)
                finish(record)
                continue
            if content.startswith("/review "):
                revision = RevisionInput.model_validate_json(
                    Path(content[8:].strip().strip('"')).read_text(encoding="utf-8-sig")
                )
                child = runner.revise(run_id, revision, execute=False)
                run_id = child.run_id
                record = execute_with_display(runner, run_id, plain=plain)
                finish(record)
                continue
            if content == "/assumptions":
                content = _("本次用了哪些假设？")
            if content.startswith("/set "):
                content = content[5:]
            from valuationagent.cli.ui import converse_with_display

            message = converse_with_display(runner, run_id, content, plain=plain)
            console.print(panel(Text(message.content), "ValuationAgent"))
            if message.related_run_id:
                run_id = message.related_run_id
                console.print(result_view(runner.store.get_run(run_id)))
                finish(runner.store.get_run(run_id))
        except (KeyboardInterrupt, EOFError):
            break
        except (ValueError, OSError, LlmError) as exc:
            console.print(panel(Text(str(exc), style="warn"), _("需要处理")))
    console.print(
        Text(
            _("会话已保存。继续：valuationagent chat {run_id}").format(run_id=run_id),
            style="muted",
        )
    )


@app.command("chat")
def chat_command(
    run_id: str,
    live: bool = typer.Option(False, "--live"),
    plain: bool = typer.Option(False, "--plain"),
):
    """继续已有任务的对话。"""
    try:
        store, runner = runtime()
        record = store.get_run(run_id)
        if live or record.request.mode == "live":
            runner.attach_model(run_id, model(True))
        console.print(banner(record.request.language))
        console.print(result_view(record))
        conversation(runner, run_id, plain)
    except KeyError:
        friendly_error(ValueError("任务不存在，请用 valuationagent history 查看。"))
    except (ValueError, OSError, LlmError) as exc:
        friendly_error(exc)


@app.command("resume")
def resume_command(
    run_id: str,
    live: bool = typer.Option(False, "--live"),
    plain: bool = typer.Option(False, "--plain"),
):
    """恢复暂停或失败任务；已完成步骤按输入哈希复用。"""
    try:
        store, runner = runtime()
        record = store.get_run(run_id)
        if live or record.request.mode == "live":
            runner.attach_model(run_id, model(True))
        record = execute_with_display(runner, run_id, plain=plain)
        finish(record)
        if record.status in ("waiting_review", "failed"):
            raise typer.Exit(1)
    except KeyError:
        friendly_error(ValueError("任务不存在。"))
    except (ValueError, OSError, LlmError) as exc:
        friendly_error(exc)


@app.command("review")
def review_command(
    run_id: str,
    patch_file: Path = typer.Argument(..., exists=True),
    plain: bool = typer.Option(False, "--plain"),
):
    """用 reason + changes JSON 更正输入，保留旧版本并继续。"""
    try:
        store, runner = runtime()
        record = store.get_run(run_id)
        if record.request.mode == "live":
            runner.attach_model(run_id, model(True))
        revision = RevisionInput.model_validate_json(
            patch_file.read_text(encoding="utf-8-sig")
        )
        child = runner.revise(run_id, revision, execute=False)
        record = execute_with_display(runner, child.run_id, plain=plain)
        finish(record)
        if record.status in ("waiting_review", "failed"):
            raise typer.Exit(1)
    except KeyError:
        friendly_error(ValueError("任务不存在。"))
    except (ValueError, OSError, LlmError) as exc:
        friendly_error(exc)


@app.command()
def history():
    """查看本地任务和版本。"""
    from rich.table import Table

    store, _ = runtime()
    table = Table("企业", "版本", "状态", "任务 ID", box=None)
    for r in store.list_runs():
        table.add_row(
            Text(r.request.company.name or r.request.company.ticker or "未命名"),
            str(r.revision),
            r.status,
            r.run_id,
        )
    console.print(panel(table, "任务历史"))


@app.command()
def inspect(run_id: str, tools: bool = typer.Option(False, "--tools")):
    """查看已有结果，或展开工具日志。"""
    store, _ = runtime()
    try:
        if tools:
            show_events(store, run_id)
        else:
            console.print(result_view(store.get_run(run_id)))
    except KeyError:
        friendly_error(ValueError("任务不存在。"))


@app.command()
def export(run_id: str, destination: Path = typer.Option(..., "--output", "-o")):
    """导出含版本、数据、假设、结果及工具证据的 JSON 复算包。"""
    store, _ = runtime()
    try:
        record = store.get_run(run_id)
        if record.result is None:
            raise ValueError("该任务尚未形成结果，请先复核或恢复。")
        if destination.exists():
            raise ValueError("目标文件已存在，请指定新文件名。")
        payload = {
            "run": record.model_dump(mode="json"),
            "artifacts": store.artifacts(run_id),
            "events": [e.model_dump(mode="json") for e in store.list_events(run_id)],
        }
        destination.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        console.print(Text(f"已导出：{destination.resolve()}", style="good"))
    except KeyError:
        friendly_error(ValueError("任务不存在。"))
    except (ValueError, OSError) as exc:
        friendly_error(exc)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    """启动 Web API 服务。"""
    import uvicorn

    uvicorn.run("valuationagent.api.main:app", host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
