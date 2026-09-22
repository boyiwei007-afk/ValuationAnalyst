# ValuationAgent · 0.2

可追溯的估值 Agent、交互式命令行与网页研究工作台。CLI 和 Web 共用任务、金融插件、版本、对话与执行事件。

Agent 以 LLM 负责意图理解、规划、工具选择和结果解释，以确定性程序负责数据校验与金融计算；长期上下文、异常恢复、来源引用和工具轨迹均可检查。完整机制见[Agent 工作机制与完整流程](docs/ValuationAgent_Agent工作机制与流程.md)。

## 启动

在项目目录打开 PowerShell：

```powershell
conda activate economic_agent

# 欢迎提示 → 自由对话 / 上传资料 → 选项确认 → 持续研究
valuationagent interactive

# 原有结构化配置和参考模型演示仍可使用
valuationagent wizard

# 直接体验合成数据，并进入对话
valuationagent demo --chat

# 运行自己的结构化请求
valuationagent run examples/structured_request.json --chat
```

也可以直接输入 `valuationagent` 进入研究对话。已有环境使用 editable 安装时，代码修改立即生效；首次安装或需要文件解析依赖时运行 `python -m pip install -e ".[documents]"`。未安装入口时使用 `python -m valuationagent.cli.main interactive`。

首次进入 `interactive` 或新建研究时，CLI 会先打开模型配置向导：可选择 DeepSeek、OpenAI 或其他 OpenAI Compatible 接口；模型名和公司名支持建议补全，也可以直接输入；API Key 使用隐藏输入，只在本次进程的模型会话中使用。验证失败会回到资料整理模式，可稍后用 `/connect` 重试。网页端点击“模型连接”打开同样的配置窗口，研究对象会自动带入欢迎页。

## 新版研究入口

CLI 和网页默认打开资料研究对话，不要求用户一开始就提供完整财务快照。欢迎提示框提供研究目标、文件上传和政策分析示例。研究会话与原有估值任务分开保存，先完成资料准备，再等待正式金融模型接入。

1. 输入公司、研究目标，或上传现有资料。连接模型后，LLM 理解上下文，按需读取文件原文，而不是把整个年报一次塞入提示词。
2. 公司、估值日、方法等设置先形成待确认方案。CLI 方向键选择，Web 点击选项后提交；也可以直接输入修改要求。
3. 提取出的数值先进入候选区，保留原始值、单位、期间、合并/母公司口径、原文引用和页码/工作表位置。用户对话里补充的数据标为手工来源。
4. 明确选择后才采纳候选。单位或口径缺失、同字段冲突等警告不能批量确认。更正保留旧记录；采纳提取值不等同于财务审核通过。
5. 继续追问政策、补充文件、检查资料缺口，或导出研究资料及对话。JSON 与可打印 HTML 已实现，正式 PDF/Excel 估值报告仍待接入。

文本型 PDF、XLSX、CSV、JSON、TXT、MD 支持真实解析，不要求统一列名或模板；语义字段提取需要连接 LLM。扫描件 OCR 尚未接入，旧 `.xls` 请先另存为 `.xlsx`。Excel 中的公式不会执行。读取成功不代表所有表格均被准确识别，提取结果仍需核对来源。

```text
我想研究 600519，先帮我整理需要的历史财务数据。
/upload examples/research_sample.txt
提取这份资料中的营业收入，保留出处，缺失信息不要猜。
/confirm
/files
/memory
/tools
/prepare
/export json
/export html
/quit
```

样例文件是明确标注的合成资料。退出时显示恢复命令：`valuationagent research --resume <research_id>`。研究会话、原文块、候选与确认事件保存在本地 SQLite；密钥不保存。未连接模型时可上传与预览原文，并用 `/company 名称`、`/date YYYY-MM-DD`、`/methods dcf,pe` 整理范围，不能进行通用自然语言提取。

这一阶段没有连接正式金融计算、A 股在线取数、联网搜索或外部金融插件。LLM 可以记录检索需求，并提供“上传资料 / 保留缺口”选项，不能把未执行的搜索描述成已完成。原有参考模型继续通过 `wizard`、`demo`、`run` 使用。

应用层契约已先冻结：`schemas/agent.py` 定义意图、上下文快照、证据、搜索查询/结果、政策影响卡片和导出产物；`search/providers.py` 提供零网络的 `MockSearchProvider` 与显式的 `UnavailableSearchProvider`。后续接入 A 股数据或联网搜索时，只需实现同一 `SearchProvider` 协议，并保存来源与截止日期，不能绕过证据复核直接填入估值。

命令行采用分步向导、深蓝/青绿配色、实时进度、Agent/工具动态与估值结果分区。宽屏并排，窄屏堆叠；日志重定向自动使用静态输出，也可显式加 `--plain`。建议终端宽度 100 列以上；尊重 `NO_COLOR` 设置。

[启动欢迎页 SVG](docs/assets/cli-welcome-112.svg) · [欢迎页 HTML](docs/assets/cli-welcome-112.html)

[宽屏预览](docs/assets/cli-preview-112.html) · [窄屏预览](docs/assets/cli-preview-70.html)

欢迎页使用青绿 / 蓝色 ValuationAgent 字标、英文简介与六段工作流程。宽高足够时展开字标；较窄的高终端分成两行，小窗口显示紧凑标题，为语言选择保留空间。

## 语言与任务参数

向导第一步可选简体中文（`zh-CN`）或 English（`en-US`）。后续向导、工作流标签、结果摘要和常用对话随选择切换。已有任务继续对话时自动读取保存的语言；旧请求未填写语言时默认中文。

```powershell
# 跳过语言提问，直接使用英文研究对话
valuationagent interactive --language en-US

# 英文演示及持续对话
valuationagent demo --language en-US --chat

# 使用文件中的 language；也可以用 --language 覆盖
valuationagent run examples/structured_request.json --language en-US --chat
```

英文离线对话支持 `What assumptions were used?`、`Explain sensitivity`、`Set WACC to 8%`、`/set terminal_growth=2.5%` 等明确指令。通用 CLI 帮助、供应商错误及原始资料/插件说明可能保留原文；语言设置不改变币种、金额、公式或数据证据。

CLI 与 Web 共用 `ValuationRequest.language`，API 可在 `POST /api/runs` 的 `request` 中传入 `"language": "en-US"`。语言与公司、估值日、数据来源、假设来源、估值方法、预测期等选择一起持久化；修改假设产生的新版本继承这些字段。结果与 JSON 复算包也记录语言。

开发接入：`request.agent_parameters()` 返回 JSON 可序列化的任务选择；执行前 Agent、对话 Agent 的上下文及财务检查工具已接入它。金融插件仍接收完整的 `ValuationRequest`，可直接读取 `request.language / methods / forecast_years / assumptions` 等字段；API 密钥独立配置，不放在任务参数中。详见[接入契约](docs/金融插件接入契约.md)。

## 参考模型任务的对话与更正

```text
本次用了哪些假设？
把 WACC 改为 8%
/set wacc=8%
/result
/assumptions
/tools
/history
/review examples/revise_wacc.json
/resume
/help
/quit
```

修改假设创建新版本、保留旧结果。对参考模型，仅修改 WACC 会复用经营预测和历史倍数估值，重算 DCF、敏感性、区间验证和报告。

无模型模式支持结果解释和上述明确的假设修改语法；实时模式通过受约束工具调用处理意图。比率使用 `8%` 或 `0.08`。增长率和营业利润率的单值修改应用到全部预测年；不同年份使用 JSON 数组指定。

跨进程继续：

```powershell
valuationagent history
valuationagent chat <run_id>
valuationagent inspect <run_id> --tools
valuationagent review <run_id> examples/revise_wacc.json
valuationagent resume <run_id>
valuationagent export <run_id> --output valuation-run.json
```

更正文件包含 `reason` 与 `changes`。可以更正 assumptions、financials、peers、data_source 等输入字段。复核不能跳过硬性规则：未解决的错误会再次阻断。

每个成功工具保存输入、输出、版本和哈希检查点；恢复复用相同输入的步骤。已完成任务重复 execute 不会重新计算；更改输入必须创建新版本。工具进行中按 Ctrl+C 请求暂停，当前工具结束后暂停。异常进程退出后，执行租约最多30秒到期。

## 模型配置

实时 Agent 要求兼容 Chat Completions 的供应商支持 function calling：

```powershell
$env:VALUATION_LLM_PROVIDER = "openai_compatible"
$env:VALUATION_LLM_BASE_URL = "https://你的供应商地址/v1"
$env:VALUATION_LLM_MODEL = "你的模型名称"
$env:VALUATION_LLM_API_KEY = "你的密钥"

valuationagent run examples/structured_request.json --live --chat
```

新版研究入口在检测到模型名称与密钥后自动使用模型；已有 CLI 会话可用 `/connect` 重新检查连接。DeepSeek 官方接口示例：

```powershell
$env:VALUATION_LLM_BASE_URL = "https://api.deepseek.com"
$env:VALUATION_LLM_MODEL = "deepseek-flash"
$env:VALUATION_LLM_API_KEY = "你的密钥"
$env:VALUATION_LLM_THINKING = "auto"
valuationagent
```

模型名称应与账户实际可用名称一致。`THINKING=auto` 在 DeepSeek 官方端点关闭思考模式，使用工具调用循环；显式 `enabled` 会调整工具选择并回传协议要求的临时 `reasoning_content`。内部思考内容不写入研究记录。400 错误只显示脱敏的参数提示，429/502/503/504 做有限重试。

环境变量由当前进程读取，`.env.example` 只是示例，不会自动加载。Web 通过 `POST /api/model-sessions` 建立内存会话。连接测试会验证工具调用，而不只是问一句 OK。

会话密钥不主动持久化；供应商错误正文不写入事件、报告或响应。删除会话会撤销它创建的客户端。重启程序后，实时任务需要重新提供模型配置；CLI 的 `chat/resume` 会从环境变量读取。

当前实现已经通过模拟供应商与 HTTP 协议测试；已用临时会话联调过 DeepSeek `deepseek-flash` 和 `deepseek-v4-pro` 的工具调用。模型名称仍以你的账号 `/models` 返回为准。

## 数据模式

| 模式 | 行为 |
|---|---|
| demo | 显式合成资料，结果带 DEMO 标记；不能混入标为真实的财务 |
| snapshot（默认） | 使用提交的数据和确定性计算，不调用 LLM |
| live | 使用提交的数据，加上 LLM 的资料审核、工具选择和对话 |

不再默认切换到演示数据。历史财务与经营假设是两个独立来源：

- 结构化请求直接填写 financials、assumptions、peers。
- 历史文件使用角色 historical_financials，ID 放入 file_ids；JSON 可以是 FinancialSnapshot 或包含 financials/peers 的对象。
- 假设文件使用角色 assumptions，ID 放入 assumption_file_ids；JSON 可以是 AssumptionInputs 或包含 assumptions 的对象。
- 选 upload 却没有对应文件、文件角色冲突、上传与手工假设混用，都会明确阻断。
- A 股在线取数尚未接入。新版研究会话可解析 PDF/XLSX 并经 LLM 提取候选，但尚未自动转换为参考模型所需的严格 FinancialSnapshot；原有 `run` 路径仍要求结构化数据。

金融接口、单位、证据、期间政策及插件替换方式见[金融插件接入契约](docs/金融插件接入契约.md)。

## 网页工作台

新版前端位于本项目的 `web/`。React / Vite 前端与 CLI 共用当前 Python 后端、SQLite 任务、版本与工具事件；仓库不依赖项目目录之外的前端副本。

本次已经构建前端，在项目目录启动即可使用：

```powershell
conda activate economic_agent
valuationagent serve --host 127.0.0.1 --port 8000
```

浏览器打开 **http://127.0.0.1:8000/**；接口文档仍在 `/docs`。如果 8000 被旧服务占用，先结束旧终端里的服务，或指定另一个端口。后端启动时发现 `web/dist/index.html`，会一并提供网页；不会向浏览器暴露源代码、任务库和上传目录。

全新检出或修改前端后，先构建：

```powershell
npm.cmd --prefix web ci
npm.cmd --prefix web run build
valuationagent serve --host 127.0.0.1 --port 8000
```

网页默认显示新版研究入口：左侧自由对话与确认选项，右侧资料原文、候选字段和真实工具执行记录；支持上传、研究历史恢复和 JSON/HTML 导出。顶部“连接模型”填写接口地址、模型和密钥后启用 LLM。文件可先上传，连接后再要求提取。服务重启或模型会话失效时需要重新连接。

通过“打开原有分步向导 / 参考模型演示”可进入以下既有流程：

1. **新建估值研究**：选择演示 / 结构化 / Live Agent，填写企业、估值日与语言；继续选择财务来源、假设来源、估值方法和预测期。
2. **模型连接**：Live Agent 填写兼容接口地址、模型名称、API Key，实际验证工具调用后创建后端临时会话。密钥不写入 localStorage、任务或报告。刷新网页后需重新连接；旧任务可以重新附加会话而不重跑估值。
3. **研究对话**：提问假设、风险、敏感性，或输入“把 WACC 改为 8%”。重算会创建新版本并切换工作台；版本下拉框和历史研究可以打开旧结果。演示与结构化模式提供确定性结果问答，Live 模式使用已连接模型选择工具。
4. **执行轨迹**：展开实际工作流节点，查看调用状态、耗时及缓存复用；点击工具检查持久化的输入和输出。SSE 增量更新配合轮询恢复，不使用假进度或虚构的内部思考过程。
5. **估值分析**：比较 DCF / P/E / EV/EBITDA 每股估值区间，查看收入与 FCFF 预测、WACC × g 敏感性热力图。点击热力格只查看该情景，不修改假设。未运行、不可用和无效组合明确显示为空。
6. **数据与依据**：查看实际财务快照、来源、假设说明和已配置规则的审核结果。阻断时可以提交更正 JSON 并重算，或恢复执行。
7. **导出研究**：下载 JSON 复算包，包含任务参数、结果、版本、事件和工具证据。PDF / Excel 报告导出仍待接入。

前端开发可在第二个终端运行 `npm.cmd --prefix web run dev`，默认 `http://127.0.0.1:5173`，将 `/api` 与 `/health` 代理到 `http://127.0.0.1:8000`。开发服务可用环境变量 `VALUATION_BACKEND_URL` 指定另一个后端；独立托管前端时可在构建前设置 `VITE_API_BASE_URL`，并配置后端 CORS。生产单服务方式不需要上述变量。`VALUATION_WEB_DIR` 可覆盖构建文件位置。

页面采用深蓝导航、浅色工作区和青绿色操作重点。宽屏以对话和执行详情并排展示，窄屏收起导航并将详情纵向排列；结果视图保留对话入口。

## Web API

```powershell
valuationagent serve --host 127.0.0.1 --port 8000
```

API 文档：`http://127.0.0.1:8000/docs`。产品网页在 `/`；Swagger 仅用于接口调试。

```text
POST   /api/model-sessions
DELETE /api/model-sessions/{session_id}
POST   /api/model-connections/test
POST   /api/files
POST   /api/research-sessions
GET    /api/research-sessions
GET    /api/research-sessions/{id}
POST   /api/research-sessions/{id}/messages
POST   /api/research-sessions/{id}/model-session
GET    /api/research-sessions/{id}/events
GET    /api/research-sessions/{id}/sources/{file_id}
GET    /api/research-sessions/{id}/export?format=json|html
POST   /api/runs
GET    /api/runs
GET    /api/capabilities
GET    /api/workflow-definition
GET    /api/runs/{id}
GET    /api/runs/{id}/results
GET    /api/runs/{id}/messages
POST   /api/runs/{id}/messages
POST   /api/runs/{id}/model-session
GET    /api/runs/{id}/events
GET    /api/runs/{id}/events/history
GET    /api/runs/{id}/artifacts
GET    /api/runs/{id}/revisions
POST   /api/runs/{id}/reviews
POST   /api/runs/{id}/revisions
POST   /api/runs/{id}/resume
```

reviews 保存更正为新版本，返回其 run_id；resume 启动该版本。revisions 的 POST 则创建版本并安排执行。对话触发修改时，回复的 related_run_id 指向新版本，前端应切换订阅。

新版研究 `messages` 接收文字、文件 ID，或 `question_id + option_id`。确认只作用于当前问题，过期选择返回 409；文字修改不会自动确认。`events?after=<序号>` 支持增量获取，Web 当前在处理期间轮询会话快照。研究会话使用逐次修订与事件记录，不套用估值任务的重算版本机制。

`POST /api/runs/{id}/model-session` 接收 `{"model_session_id":"…"}`，为已有任务附加临时模型会话。不会执行或修改估值；执行中的任务返回 409，未知会话返回 404。

SSE 支持 Last-Event-ID，从已读序号之后读取。单次运行进入完成/复核/失败状态且事件已发送完毕后流结束；后续动作需重新订阅对应任务。正式金额以十进制字符串返回，避免 float 损失精度；前端图表显式转换，复算保持 Decimal。

## 当前边界

已实现参考 DCF、P/E、EV/EBITDA、WACC × 永续增长率敏感性、区间比较、JSON 复算包，以及财务/假设复核和版本重算。正式摘要的数字和审核标识由结构化结果渲染，LLM 自由文本不能改写。

参考模型新增显式首年剩余现金流与年中折现政策，样例结果与 0.1 版本不同；详见契约。完整三表联动、收入驱动模型、治理因子、PDF/Excel 导出和市场取数仍待金融团队/适配器接入。无效情景被排除时结果附警告；全部估值方法不可用时进入复核。

CLI 的布局借鉴 [TradingAgents](https://github.com/boyiwei007-afk/TradingAgents/tree/main/cli) 的分步选择和实时分区，界面与业务代码为本项目实现。兼容工具协议参照 [OpenAI function calling](https://developers.openai.com/api/docs/guides/function-calling?api-mode=chat)。

## 验证

```powershell
conda activate economic_agent
python -m pytest -q
npm.cmd --prefix web test
npm.cmd --prefix web run lint
npm.cmd --prefix web run build
```

前端真实接口与组件渲染联调：启动一个独立测试后端后，设置 `VALUATION_TEST_URL` 并运行 `npm.cmd --prefix web run test:integration`。该检查会创建明确标记的演示任务及重算版本，不使用真实模型密钥。它验证 API / SSE 与组件服务端渲染，不等同于浏览器点击或视觉验收。

若系统临时目录权限受限，为 pytest 指定一个全新的项目内目录，例如 `--basetemp var/test-20260912-new`。不要把已有任务目录作为 basetemp。

测试覆盖正常计算、数据隔离、文件角色、精确输入、财务阻断、情景边界、工具调用、会话撤销、错误脱敏、版本与重算、恢复与并发、API/SSE 和终端排版。既有 0.1 验收报告保留为历史记录，不能作为修复后状态清单。

## 项目结构

```text
src/valuationagent/   Python 包：Schemas、Workflow、Finance、LLM、Storage、CLI、API
web/                  React/Vite 工作台（与 CLI 共用后端任务和执行事件）
tests/                Python 回归与 API/SSE 测试
examples/             可直接运行的结构化请求、假设和 WACC 更正示例
docs/                 架构计划、验收记录、接入契约和 CLI 预览素材
```

项目地址：[github.com/boyiwei007-afk/ValuationAnalyst](https://github.com/boyiwei007-afk/ValuationAnalyst)。
