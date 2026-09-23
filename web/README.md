# ValuationAgent Web

React / Vite 研究工作台，与父目录的 Python Agent 共用请求模型、会话、数据和执行记录。前端代码和依赖全部位于本目录。

生产使用：在父目录构建 `npm.cmd --prefix web ci`、`npm.cmd --prefix web run build`，随后在 `economic_agent` 环境运行 `valuationagent serve`，访问 `http://127.0.0.1:8000/`。此工作台依赖本地 Python 服务，不是可以独立运行的静态演示页。

开发：后端运行在 8000 时，在此目录执行 `npm.cmd run dev`。默认 5173；开发代理目标由 `VALUATION_BACKEND_URL` 指定。独立前端部署的 API 地址由构建变量 `VITE_API_BASE_URL` 指定，并需配置后端 CORS。

主要模块：

- `App.jsx`：任务选择、会话、持续对话、复核及版本切换。
- `ResearchDesk.jsx`：资料研究对话、草稿与附件、确认选项、原文、长期上下文及实际执行记录。
- `MessageBody.jsx`：显示模型回复中的列表、表格与代码，禁用原始 HTML、危险链接和远程图片加载。
- `Wizard.jsx`：与 CLI 对应的三步配置。
- `domain.js`：请求映射、精确百分数转换、事件与图表数据处理。
- `useResearch.js`：任务快照、SSE、断线轮询及祖先版本消息。
- `Inspector.jsx`：真实阶段、工具状态和输入输出。
- `Analysis.jsx`：区间对照、收入 / FCFF、敏感性及数据证据。
- `api.js`：后端请求、角色文件上传及 JSON 下载。

金额和假设在请求、存储及导出中沿用十进制字符串；仅绘图和展示转换为 Number。上传结构化请求中的精确金额应使用字符串。热力图选择不修改假设。修改假设通过后端版本重算完成，不在浏览器里模拟估值结果。

API Key 只在连接表单中暂存并提交给本地后端临时会话；不进入浏览器存储或任务。后端重启或页面刷新后可重新连接模型。演示 / snapshot 结果问答不需要密钥；Live 模式需要支持 function calling 的供应商。

验证：`npm.cmd test`、`npm.cmd run test:components`、`npm.cmd run lint`、`npm.cmd run build`。组件回归使用虚拟 DOM 和模拟接口，验证 Markdown 安全、键盘确认、模型连接后保留草稿、确认选择与附件提交隔离，不调用真实模型。

`npm.cmd run test:integration` 使用 `VALUATION_TEST_URL`（默认 8001）对真实服务验证任务、问答、版本、SSE、证据和组件服务端渲染，会创建演示任务；请指向独立测试数据目录。2026-09-22 已完成桌面和 390 px 手机宽度的浏览器验收，覆盖研究创建、选项确认、上传原文与执行记录。本次验收未调用真实供应商模型。
