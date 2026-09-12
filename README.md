# ValuationAgent · 0.2

可追溯的估值 Agent、交互式命令行与网页研究工作台。CLI 和 Web 共用任务、金融插件、版本、对话与执行事件。

## 启动

在项目目录打开 PowerShell：

```powershell
conda activate economic_agent

# 欢迎页 → 语言选择 → 分步配置 → 实时执行 → 持续对话
valuationagent interactive

# 直接体验合成数据，并进入对话
valuationagent demo --chat

# 运行自己的结构化请求
valuationagent run examples/structured_request.json --chat
```

也可以直接输入 `valuationagent` 进入向导。已有环境使用 editable 安装时，代码修改立即生效；需要更新安装元数据时运行 `python -m pip install -e . --no-deps`。未安装入口时使用 `python -m valuationagent.cli.main interactive`。

命令行采用分步向导、深蓝/青绿配色、实时进度、Agent/工具动态与估值结果分区。宽屏并排，窄屏堆叠；日志重定向自动使用静态输出，也可显式加 `--plain`。建议终端宽度 100 列以上；尊重 `NO_COLOR` 设置。

[启动欢迎页 SVG](docs/assets/cli-welcome-112.svg) · [欢迎页 HTML](docs/assets/cli-welcome-112.html)

[宽屏预览](docs/assets/cli-preview-112.html) · [窄屏预览](docs/assets/cli-preview-70.html)

欢迎页使用青绿 / 蓝色 ValuationAgent 字标、英文简介与六段工作流程。宽高足够时展开字标；较窄的高终端分成两行，小窗口显示紧凑标题，为语言选择保留空间。

## 语言与任务参数

向导第一步可选简体中文（`zh-CN`）或 English（`en-US`）。后续向导、工作流标签、结果摘要和常用对话随选择切换。已有任务继续对话时自动读取保存的语言；旧请求未填写语言时默认中文。

```powershell
# 跳过语言提问，直接使用英文向导
valuationagent interactive --language en-US

# 英文演示及持续对话
valuationagent demo --language en-US --chat

# 使用文件中的 language；也可以用 --language 覆盖
valuationagent run examples/structured_request.json --language en-US --chat
```

英文离线对话支持 `What assumptions were used?`、`Explain sensitivity`、`Set WACC to 8%`、`/set terminal_growth=2.5%` 等明确指令。通用 CLI 帮助、供应商错误及原始资料/插件说明可能保留原文；语言设置不改变币种、金额、公式或数据证据。

CLI 与 Web 共用 `ValuationRequest.language`，API 可在 `POST /api/runs` 的 `request` 中传入 `"language": "en-US"`。语言与公司、估值日、数据来源、假设来源、估值方法、预测期等选择一起持久化；修改假设产生的新版本继承这些字段。结果与 JSON 复算包也记录语言。

开发接入：`request.agent_parameters()` 返回 JSON 可序列化的任务选择；执行前 Agent、对话 Agent 的上下文及财务检查工具已接入它。金融插件仍接收完整的 `ValuationRequest`，可直接读取 `request.language / methods / forecast_years / assumptions` 等字段；API 密钥独立配置，不放在任务参数中。详见[接入契约](docs/金融插件接入契约.md)。

## 对话与更正

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

环境变量由当前进程读取，`.env.example` 只是示例，不会自动加载。Web 通过 `POST /api/model-sessions` 建立内存会话。连接测试会验证工具调用，而不只是问一句 OK。

会话密钥不主动持久化；供应商错误正文不写入事件、报告或响应。删除会话会撤销它创建的客户端。重启程序后，实时任务需要重新提供模型配置；CLI 的 `chat/resume` 会从环境变量读取。

当前实现已经通过模拟供应商与 HTTP 协议测试；本轮没有使用真实供应商密钥联调。

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
- A 股在线取数和 PDF/Excel 解析尚未接入。上传可以接收这些文件，但不能生成假财务；用户可复核后补充结构化数据继续。

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

网页使用方式：

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
