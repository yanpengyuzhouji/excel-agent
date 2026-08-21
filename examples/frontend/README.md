# Excel Agent 示例前端

单文件、零依赖、零构建的示例前端，由 Excel Agent 服务自身以 `/ui` 路径静态挂载。

## 访问

1. 启动服务（见根目录 `README.md`）
2. 浏览器打开 <http://127.0.0.1:8090/ui>

## 功能

- **上传**：`＋ 上传` 选择 `.xlsx / .xlsm / .csv`，自动生成审核草稿
- **审核**：切换 Sheet、改类型（数据表/元数据/忽略）、改表名/表头行、编辑字段（显示名/物理名/类型/启用）、**单元格级编辑**（点击数据预览单元格直接改）、勾选警告、确认建库
- **问答**：对已建库的工作簿提问，SSE 流式展示实际分析路由、会话模型、数据画像、规则线索、SQL 查询、结果表格、任务计划、结构化报告和 HTML 报告 iframe；刷新后会从会话记录恢复模型与路由
- **会话管理**：会话下拉框显示最近使用的模型，可用“删除”按钮删除当前会话及其工具调用记录，并同步刷新本地选择状态
- **双路由控制**：模型列表显示对应的“规则模板 / 模型 Agent”路径；默认自动路由，也可临时覆盖为 small 或 large。

## 技术说明

- 纯原生 JS：`fetch` + `ReadableStream` 消费 SSE（解析 `data: ` 行，按 `type` 分发 `analysis_route/profile/finding/report_spec/status/sql/table/plan.update/html/done/error`）
- 同源访问 `/api/v1/*` 与 `/ui`，无 CORS
- 与 RongNengRAG 前端（Vue3）共享同一套后端 API，仅用于独立演示

## 注意

问答需要 LLM 可达（`config.yaml` 的 `excel.llm.base_url` 指向 OpenAI 兼容 `/v1` 服务）。若 LLM 未启动，仍可完成「上传 → 审核 → 确认建库」全流程，并用 `/api/v1/workbooks/{id}/query` 手动验证只读 SQL。
