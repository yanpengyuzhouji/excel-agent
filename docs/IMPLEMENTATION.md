# Excel Agent 实现记录

> 本文档全程记录 `excel-workbook-service` 原地升级为 **Excel Agent** 的过程：需求、决策、现状基线、分阶段实现与验证、与 DB-GPT 的差异、已知限制、回滚方法。

## 1. 目标与范围

把 DB-GPT 0.8.1 的「Excel 工作簿」全链路（上传 → 弹窗审核 → 编辑 → 确认入库 → 工具调用 → ReAct 循环 → 问答 → 报告生成）封装成独立 FastAPI 微服务，供 RongNengRAG 知识库问答系统调用，实现 **Excel 智能问答** 与 **分析报告生成** 两个功能。

- **构建方式**：基于已有 `D:\git\excel-workbook-service` 原地升级（该服务已覆盖全链路）
- **目录名不换**：文档中称 **Excel Agent**，目录仍为 `excel-workbook-service`
- **API 保持兼容**：RongNengRAG 前端零改动

## 2. 关键决策

| # | 决策 | 理由 |
|---|---|---|
| 1 | 基于现有服务升级，不重做 | 现有服务已覆盖全链路，避免重复劳动 |
| 2 | 纯后端 + 附最小示例前端 | RongNengRAG 前端已完整；示例前端独立演示用 |
| 3 | 纳入流式解析 + 防护阈值 | 现有 parser 是旧版非流式，大文件会 OOM（DB-GPT 今日已修） |
| 4 | 原地升级不换目录名 | 减少集成变动 |
| 5 | API 路径/字段/SSE 事件格式不变 | RongNengRAG 前端零改动前提 |
| 6 | 防护阈值放 `server` 节 | 与 storage_path/preview_page_size 同层，语义清晰 |
| 7 | parser 整文件替换为 DB-GPT 新版 | 两文件同源，仅需删内联词表 + 恢复 `.constants` 导入 |

## 3. 现状基线（升级前）

- 旧 `parser.py` 使用 `load_workbook(read_only=False)` **双加载**，把整工作簿物化两份，60k 行≈+573MB 内存，真实大文件会触发 OOM killer 杀进程。
- 旧 smoke_test 通过（SMOKE_OK）。
- `excel-workbook-service` **不是 git 仓库**，升级前将旧 parser 备份为 `scripts/legacy_parser.py`（阶段末删除）。

## 4. 分阶段实现记录

### Phase 0 — 基线备份
- 备份 `app/service/parser.py` → `scripts/legacy_parser.py`
- 跑基线 `scripts/smoke_test.py`：`SMOKE_OK`
- 目的：回滚与新旧一致性比对的基础

### Phase 1 — 流式解析同步（核心）
**改动**：`app/service/parser.py` 整文件替换为 DB-GPT 流式版
- 新增：`_sheet_xml_targets` / `_sheet_metadata_from_xml`（ZipFile + iterparse 回收 merged/hidden）/ `_grid_value` / `_parse_streamed`（逐行 zip 迭代）
- 改签名：`parse_workbook(path, file_name=None, max_rows=None)`
- 删内联词表三块，恢复 `from .constants import COMMON_COLUMN_NAMES, HEADER_FIELD_WORDS, PLACEHOLDER_VALUES`

**验证**：
- import 冒烟：`max_rows` 参数存在、streamed helpers 齐全、constants 接线正确
- parity 比对：CSV 逐字节一致；xlsx 核心一致（材料表 header_row=3 rows=49 新旧相同）
- smoke 回归：`SMOKE_OK`
- 合成 60k 文件：新旧均 header_row=2 rows=59999（**非回归**，合成数据固有启发式边界）

**已知良性差异**：真实样例「封面」sheet row_count 22→23（新版保留完整行含尾部空行；旧版只到最后一个非空行）。仅 metadata sheet，不产生 metadata，无业务影响。

### Phase 2 — 防护阈值
**改动**：
- `config.yaml` `server:` 加 `max_import_bytes: 209715200` / `max_import_rows: 300000`
- `app/config.py` `AppConfig` + `build_app_config` 增加两字段（带默认值向后兼容）
- `app/service/service.py` `create_import` 上传后校验字节数；`parse_workbook(..., max_rows=...)` 传行数上限

**验证**：小阈值单测全绿（byte 拒绝、row 拒绝、界内导入正常）。注意：CSV 的 `len(rows)` 含表头行。

### Phase 3 — 测试扩展
**改动**：`scripts/smoke_test.py`
- 追加 cell_updates 用例：编辑首行 store_id → revision+1 → get_sheet_rows 值变更 → 建库后 `SELECT` 反映 999
- 追加防护阈值用例：独立小配置，byte/row 超限均 ValueError

**验证**：`SMOKE_OK`（含 cell edit materialized -> 999、guardrail bytes/rows rejected）
- 清理全部临时验证脚本（legacy_parser / parser_check / parity / header_compare / guardrail_check）

### Phase 4 — 示例前端
**新增**：
- `examples/frontend/index.html` 单文件（原生 JS + fetch + ReadableStream 消费 SSE）
- `examples/frontend/README.md`
**改动**：`app/main.py` 加 `/ui` 静态挂载（`.exists()` 守卫）+ 标题改 "Excel Agent"，版本 1.1.0

**验证**：
- `/ui` 200、`/health` ok、OpenAPI 标题 "Excel Agent"、11 条 API 路径齐全
- E2E HTTP：上传 Walmart_Sales.csv → ready → confirm active → 查询 COUNT=6435
- `/ask` 需 LLM 可达（`excel.llm.base_url`），本环境无 LLM 服务，ReAct 代码原样保留未改

### Phase 5 — 文档
- 新增 `docs/IMPLEMENTATION.md`（本文件）
- 新增 `docs/API.md`
- 新增 `docs/INTEGRATION_RONGNENGRAG.md`
- 更新 `README.md`

### Phase 6 — 可选增强（仅文档记录）
见 `docs/INTEGRATION_RONGNENGRAG.md`，不改 RongNengRAG 代码。

## 5. 与 DB-GPT 的差异清单

| 项 | DB-GPT 0.8.1 | Excel Agent |
|---|---|---|
| 框架 | SystemApp / BaseService / Result | 普通类 + FastAPI app.state / 直接返回 Pydantic |
| 词表 | 内联在 parser.py | 抽到 `app/service/constants.py` |
| 多用户 | user_name / sys_code / 每用户目录 | 已移除（单实例共享存储） |
| 数据源注册 | `_register_datasource`（dbgpt_serve.datasource） | 已移除 |
| Windows URI | 无 | `.as_posix()` 修复 mode=ro URI |
| parser | 流式 read_only（2026-08-07 修复） | 同（本升级同步） |
| 工具链 | code_interpreter / shell_interpreter / html_interpreter / sql_query / todowrite | 仅 4 个白名单工具（无任意代码执行） |

## 6. 已知限制

- **整 sheet 内存**：流式解析降到单份，但 `_parse_streamed` 仍把整 sheet rows 留在内存并写入 draft.json；30 万行上限必要，draft.json 可达数十 MB。
- **failed 残留**：行数/字节超限时 source 文件已落盘且 import 标记 failed（与 DB-GPT 同行为）。
- **read_only 布局依赖 XML 元数据**：merged/hidden 靠 `_sheet_metadata_from_xml` 流式回收，依赖 OOXML 结构。
- **封面尾部空行**：metadata sheet 保留完整行（含尾部空行），无害。
- **/ask 依赖外部 LLM**：需 OpenAI 兼容 `/v1/chat/completions` 服务可达。

## 7. 回滚方法

- parser 回滚：本升级前旧版已备份，如异常可从 git（DB-GPT 源）或本文记录的差异恢复
- 防护阈值回滚：删除 `config.yaml` 的 `max_import_bytes` / `max_import_rows`（AppConfig 有默认值，不删配置也能跑，只是无上限）
- `/ui` 挂载回滚：删除 `app/main.py` 末尾的 mount 块（`.exists()` 守卫保证目录缺失不报错）

## 8. 后续优化记录（2026-08-07）

### 8.1 警告/错误信息中文化
- `app/service/service.py` 的 `_validate_draft`：全部 error/warning 的 `message` 改为中文（`code` 保持英文——RongNengRAG 前端逻辑依赖 code，改动 code 会破坏兼容）。
- 示例前端 `index.html`：新增 `CODE_LABELS` 映射表，把英文 `code` 显示为中文标签（如 `MERGED_CELLS` → `[合并单元格]`）。
- 影响：RongNengRAG 前端直接显示 `message` 字符串，自动受益于中文化；`code` 未变，兼容性无破坏。

### 8.2 审核一键确认
- 示例前端 `index.html`：警告默认**全部预勾选**，新增「一键全选 / 取消全选」按钮；确认建库按钮在无错误时默认可用（无需逐个勾选警告）。
- 后端 `confirm_import` 本就需要全部警告 code 在 `accepted_warning_codes` 中，预勾选后点击确认即自动携带全部警告，实现一键确认。

> 注意：RongNengRAG 前端的 `ExcelDraftReview.vue` 仍是逐个勾选 + 英文 code 显示（message 已中文）。如需同步优化，改该 Vue 组件即可（逻辑与示例前端相同），属 RongNengRAG 仓库改动。

### 8.3 问答接口改用 deepseek
- `config.yaml` 的 `excel.llm`：`base_url` → `https://api.deepseek.com/v1`，`model` → `deepseek-v4-flash`，`api_key_env` → `DEEPSEEK_API_KEY`。
- 新增 `excel.llm.env_file` 配置：指向共享 `.env` 文件（如 DB-GPT 的 `.env.deepseek`）。当环境变量 `DEEPSEEK_API_KEY` 未设置时，`ExcelLLM`（`app/service/llm.py`）回退从该文件读取同名变量，**密钥不写入 config.yaml**。
- 新增 `_read_env_file()` 解析 `KEY=value` / `export KEY=value` 格式。
- 验证：ExcelLLM 解析到 deepseek base/model、key 从 env_file 加载；真实 `/ask` 端到端测试通过（deepseek 返回「该表格共有 6435 行」）。

### 8.4 问答展示还原 DB-GPT Manus 风格（step 事件 + 步骤面板）
- **后端 `react_loop.py`**：每轮 ReAct 发出 DB-GPT Manus 风格 step 事件：
  - `step.start`（title=工具名，detail="Thought/Action/Observation"）
  - `step.meta`（thought/action/action_input，思考由 `_extract_thought()` 从 reply 提取）
  - `step.chunk`（`output_type`: `code`=SQL/HTML 源码实时回显、`table`=结果、`html`=报告含源码、`error`）
  - `step.done`（status: done/failed）
  - **向后兼容**：保留旧 `sql`/`table`/`html`/`plan.update`/`done` 事件，RongNengRAG 前端零改动。
- **示例前端 `index.html`**：QA 部分重写为 Manus 风格双栏：
  - 左侧步骤卡片列表（工具图标 + 标题 + 状态 执行中/完成/失败）
  - 右侧详情：思考块（琥珀色）、SQL 高亮卡片（SQL Query + READ ONLY 徽章 + 复制按钮）、结果表格、**HTML 完整报告 iframe**；Action Input 和 HTML 源码只作为执行数据保留，不在用户报告区展示
  - 底部回答块；HTML tab 用事件委托切换
- 验证：真实 deepseek `/ask` 端到端通过——sql_query → code SQL 回显 → table → html_interpreter → code HTML 回显 → html 报告 → done 完整回答。
- 参考：DB-GPT `web/new-components/chat/content/ManusRightPanel.tsx`（SQL 高亮卡片、HtmlTabbedRenderer 渲染/源码双 tab）与 `react-sse-parser.ts`（step 协议）。

### 8.6 ReAct 最大轮次调大（max_loops 8 → 30 → 100）
- **问题**：生成报告的任务（探索多表 + html_interpreter）在 8/30 轮内探索未完成就耗尽，报 `ReAct loop reached the maximum`。
- **改动**：`config.yaml` 的 `excel.agent.max_loops` 8 → 30 → 100（`endpoints.py` 从 `service.config.agent["max_loops"]` 读取，改配置即生效）。
- 验证：`build_app_config().agent["max_loops"] == 100`。

### 8.7 记忆系统移植（会话历史 + 多轮 + 工具记忆 + 日志落盘）

按 `docs/DBGPT-MEMORY-CLOSED-LOOP.md` 第 6 节移植 DB-GPT 的 memory/message 闭环：

**① 会话历史持久化**
- 新增 `app/models/conversation.py`：`workbook_conversation` + `workbook_message` 两张 SQLite 表（`BaseDao`/`Model` 复用 `db.py`）
- 新增 `app/conversation.py`：`ExcelWorkbookConversation`（conv_id/workbook_id/messages/chat_order，`create/load/start_new_round/add_user_message/add_ai_message/add_tool_message/get_history_messages/delete`）

**② 多轮对话上下文**
- `/ask` 加可选 `conv_id`（不传自动新建）；加载历史 → `get_history_messages(keep_end_rounds=10)` 窗口裁剪 → 拼入 prompt
- `react_loop.run_react` 加 `history_messages` 参数，把 user/ai/tool 历史注入 LLM
- `done` 事件回传 `conv_id`，结束后 `add_ai_message(full_answer)` 回写

**③ 工具调用记忆**（对应 DB-GPT write_memories）
- `run_react` 加 `on_tool_step(thought, action, observation)` 回调，每步 act 后触发
- `/ask` 把 `_record_tool_step` 传入 → `conv.add_tool_message(thought, action, observation[:500])`

**④ 后端日志落盘**
- `app/main.py`：`_setup_logging()` 用 RotatingFileHandler 写 `<storage>/logs/excel-agent.log`（10MB 滚动×5）+ 保留控制台；模块顶层 + lifespan 各调一次（uvicorn 会重置 root logger，用 handler 检测避免重复）
- `/ask` 记录 conv_id/workbook_id/question；完成记录 answer 前 200 字

**API 新增**：
- `GET /workbooks/{wid}/conversations` — 会话列表
- `GET /workbooks/{wid}/conversations/{conv_id}` — 消息列表
- `DELETE /workbooks/{wid}/conversations/{conv_id}` — 删除会话

**验证**：隔离测试（创建/重载/多轮/窗口/删除全绿）+ 真实 deepseek E2E（Q1"多少行"→6435；Q2 同 conv_id"总销售额"→67.37 亿，AI 记得上文；conversation roles `['user','tool','ai','user','tool','ai']` 含工具记忆；excel-agent.log 落盘）。

### 8.8 前端多轮续接（修复"新 ask 覆盖旧对话"）
- **问题**：示例前端每次 ask 不带 `conv_id`，后端每次都新建会话，旧对话记录在 UI 上"消失"。
- **改动**：`examples/frontend/index.html`
  - 新增 `qaConvId` + `qaConvByWorkbook`（workbook_id → conv_id 映射）
  - `doAsk` 带 `conv_id`（有则续接同一会话）；`done` 事件保存返回的 `conv_id`
  - `renderQA` 恢复该工作簿的 `conv_id`（切视图/工作簿不丢会话；同一工作簿连续对话累积）
- 验证：真实 deepseek 三轮（多少行→总销售额→平均单价）同一 `conv_id` 续接，会话 3 轮 9 条消息未覆盖；Q3 体现上下文记忆（AI 记得表结构）。

### 8.10 报告生成修复（空 action 当回答 + 不收敛）
- **问题**：问"生成分析报告"时模型反复 sql_query（24+ 轮）从不调用 html_interpreter；模型用 `Tool:` 前缀（而非 `Action:`）时 `parse_react` 解析失败 → 空 action 被当最终回答返回 `Tool: sql_query\nAction Input: {...}`。
- **根因**（对比 DB-GPT）：① `parse_react` 只认 `Action:` 前缀，不兼容 `Tool:`；② 空 action 直接当最终回答（DB-GPT 是重试反馈）；③ 提示词对报告生成约束不足 + 无收敛引导。
- **修复**：
  - `react_loop.py parse_react()`：`action is None` 时尝试 `Tool:` 前缀匹配（`_TOOL_RE`）
  - `react_loop.py` 主循环：空 action / 未知 action → 追加 retry 提示让模型自我修正（仿 DB-GPT `is_exe_success=False` 重试），不再当最终回答；`terminate` 正常处理
  - `workflow_prompt.py`：明确必须 `Action:` 前缀（禁 `Tool:`）、禁止把工具调用当最终回答、SQL 数据够了立即调 html_interpreter
  - `react_loop.py`：连续 `sql_query` ≥5 次追加系统提醒"数据足够就生成报告"（收敛机制）
- **验证**：
  - 单元（假 LLM）：`Tool:` 前缀解析 OK、空 action 重试（2 次调用）、未知 action 重试、收敛触发
  - 真实 deepseek E2E：`sql_query×18 → html_interpreter×2`，报告 URL 生成，`done` 带 report_url，92.8s 完成，报告文件 8908 字节可访问；此前 24 轮纯 sql_query 不收敛、报告后空 action 无限重试的问题解决

### 8.17 修复 Qwen terminate result 解析（多行 JSON）
- **问题**：Qwen2.5-7B 生成报告后，最终回答带 `Action: terminate\nAction Input: {"result": ...}` 前缀（原始 LLM 输出被当回答）。
- **根因**：Qwen 的 `Action Input` JSON 里 `result` 值含**真实换行**（未转义 `\n` 控制字符），`json.loads` 失败 → `action_input={}` → terminate 分支 `final = action_input.get("result") or reply` 回退到完整 `reply`（含 `Action:` 前缀）。
- **修复**：`react_loop.py parse_react` 的 fallback 把 JSON 内真实换行/制表符转义为 `\n`/`\t` 后重试。
- 验证：单元（Qwen 多行 result 解析 OK）+ 真实 E2E——Qwen 报告回答干净文本"已生成销售分析报告"，无 `Action:` 前缀。

### 8.16 多模型支持（deepseek + 本地 Qwen2.5-7B vLLM）
- **需求**：问答页可选 deepseek 或本地 vLLM 的 Qwen2.5-7B。
- **改动**：
  - `config.yaml`：新增 `excel.llms` 列表（deepseek + `http://192.168.0.201:18000/v1` 的 `/data/models/Qwen/Qwen2.5-7B-Instruct`）；保留 `excel.llm` 作默认
  - `app/config.py`：`AppConfig` 加 `llms` 字段并读取
  - `app/api/endpoints.py`：`AskRequest` 加 `model` 字段；`/ask` 按 `model` 从 `llms` 选配置（默认 `excel.llm`）；新增 `GET /api/v1/models` 返回模型列表（去重）
  - `examples/frontend/index.html`：QA 输入栏加模型下拉（`#qaModel`），`loadQaModels()` 从 `/models` 加载，ask 带 `model`
- 验证：`/models` 返回 3 项（去重后 2 个唯一模型）；真实双模型问答——deepseek "6435 行"、Qwen2.5-7B "6435行"、默认 deepseek，全部成功。

### 8.15 完全独立 + 局域网访问
- **配置独立**：从依赖 DB-GPT 的 `.env.deepseek` 改为**项目自带 `.env`**（`config.yaml` 的 `excel.llm.env_file` → `".env"`）；新增 `.env.example` 模板。完全独立于 DB-GPT / RongNengRAG。
- **`_read_env_file` 增强**：相对路径按 config 目录（项目根）解析，`env_file: ".env"` 无论 cwd 在哪都能找到。
- **局域网访问**：`config.yaml` 的 `server.host` `127.0.0.1` → `0.0.0.0`。
- 验证：`DEEPSEEK_API_KEY` 从本地 `.env` 读取（35 位 `sk-`）；`ExcelLLM` 用本地 key；服务监听 `0.0.0.0:8090`，`http://192.168.0.7:8090/api/v1/health` → HTTP 200。

### 8.14 HTML 报告渲染修复（srcDoc）+ PDF 下载
- **问题**：报告 HTML 渲染结果未呈现。原因：前端 iframe 用 `src="${url}"` 请求报告 URL，URL 含中文文件名（如 `_国网批次分析报告`）未编码，iframe 加载失败白屏。
- **修复**（`examples/frontend/index.html`）：
  - `renderHtmlTabs`：优先用与落盘文件一致的 **`srcdoc`** 直接渲染完整报告 HTML，历史记录无源码时回退到报告 URL；不再按“富布局/简陋表格”套不同展示壳
  - 新增 **"⬇ 下载 PDF"** 按钮（`data-pdf`）：点击后 `iframe.contentWindow.print()` 触发浏览器打印 → 另存为 PDF（对齐 DB-GPT `handleExportPdf`）
  - `.pdf-btn` 点击在现有 tab 切换委托处理器中处理
- 验证：真实 deepseek E2E——`step.chunk html` 携带完整 HTML 源码（7673 字节）+ 报告 URL，srcDoc 渲染与 PDF 按钮数据就绪。

### 8.13 右侧执行记录联动（点击左侧轮次 → 右侧展示该轮完整执行）
- **问题**：左侧能点开每轮的工具调用/回答，但右侧执行记录区只显示最新一轮，历史轮次完整执行数据（SQL/表格/HTML）不联动。
- **改动**：
  - `react_loop.py`：新增 `on_tool_step_full(thought, action, observation, detail)` 回调，sql_query 传 `{sql, table{columns,rows,row_count,truncated}}`，html_interpreter 传 `{title, html{url,title,report_id,html}}`（保留 `on_tool_step` 向后兼容）
  - `conversation.py add_tool_message`：新增 `detail` 参数，meta 存 `{action, thought, observation, detail}`
  - `endpoints.py`：`_record_tool_step_full` 一次持久化完整 detail
  - `examples/frontend/index.html`：`qaTurnSteps`（round→steps 缓存）+ `qaCurrentRound`；step 事件同时写缓存；点击左侧 turn → `showTurnInRightPanel(round)` 从缓存或 `rebuildStepsFromTurn`（用 meta.detail 重建 sql/table/html 步骤）填充右侧 `qaSteps` 并渲染
- 验证：真实 deepseek E2E——25 条 tool 消息全部含结构化 detail（sql+table / html+url），右侧可据此重建历史轮次执行记录。

### 8.12 对话按轮分组展示（对齐 DB-GPT SessionTurn）
- **问题**：`qaChat` 平铺 user/ai 气泡，工具调用不显示；`qaSteps` 每次 doAsk 重置只保留最新一轮，历史轮次的智能体结果看不到。
- **改动**：
  - `app/conversation.py add_tool_message`：meta 存结构化 `{action, thought, observation}`（前端能按轮渲染每个工具调用）
  - `examples/frontend/index.html`：`qaChat` 改为**按 round_index 分组的 turn 卡片**（`{round_index, user, ai, tools[]}`）
    - 卡片头：user 问题 + 轮次 + 工具次数 + 完成状态
    - 点击展开：显示该轮每个工具调用的 思考/结果 + AI 回答（DB-GPT SessionTurn 风格）
    - `loadChatHistory` 从会话 API 按 round 重建；`handleEvent` step.done 时把工具调用追加到当前轮
- 验证：隔离单元（round 分组 + 结构化 tool meta）+ 真实 deepseek E2E（两问 → 会话 2 轮，每轮 user/tools/ai 齐全）。

### 8.11 报告生成二次修复（空 action 无限重试 + html_interpreter 隐藏 bug）
- **问题 1**：报告生成后模型输出**纯文本总结（无 Action 前缀）**，被空 action 逻辑当格式错误无限重试，耗尽 max_loops 报错。
- **修复**：`react_loop.py` 空 action 处理改为**区分性质**——回复含工具调用痕迹（`Tool:`/`Action Input:`/`Observation:`/```json```/工具名）→ 重试；干净文本回答 → 当最终回答（成功结束）。空回复 → 重试。
- **问题 2（latent bug）**：`html_interpreter` 分支 `step.done` 检查 `"error" not in obs`，但成功路径 `obs` 从未赋值（用的是 `rep`）→ `UnboundLocalError` 崩溃。改为统一 `rep`。
- 验证：单元 4 项（工具痕迹→重试、报告后纯文本→最终、空回复→重试、纯 markdown→最终）+ 真实 deepseek E2E（92.8s 完成，报告 8908 字节）。
- **问题**：QA 面板只显示当前轮步骤，用户看不到历史问答（"前面的就没了"）。
- **改动**：`examples/frontend/index.html` QA 面板改为**三栏**：左侧「💬 对话记录」（完整历史 user/ai 消息）+ 中间步骤列表 + 右侧详情。
  - `qaChat` 数组存聊天记录；`renderChatList()` 渲染；`appendChatMsg()` 追加
  - `loadChatHistory()`：`renderQA` 时若 `qaConvId` 已存在，调 `GET /conversations/{conv_id}` 加载历史消息
  - `doAsk` 提交后 `appendChatMsg('user', q)`；`done` 事件收到回答后 `appendChatMsg('ai', answer)`
- 验证：`/ui` 服务端返回 200 且含 qa-chat/loadChatHistory/appendChatMsg/「对话记录」。

### 8.5 Excel 库表删除功能（新增）
- **现状**：原服务无删除端点，用户无法删除已入库的工作簿。
- **改动**：
  - `app/models/workbook.py`：`WorkbookDatasetDao.delete_by_workbook_id` + `WorkbookImportDao.delete_by_workbook_id`（清所有版本的 import 行）。
  - `app/service/service.py`：`delete_workbook(workbook_id)` —— 校验存在 → 收集各版本 `draft_path` 定位 workbook 目录 → 清 DB 行 → `shutil.rmtree` 删整个 workbook 目录（source/draft/database/reports）。
  - `app/api/endpoints.py`：`DELETE /api/v1/workbooks/{workbook_id}`，404→400（workbook 不存在）。
- 验证：真实 E2E —— 上传→确认(active, db 存在)→DELETE(200)→db 文件已删→GET 400→列表不含→二次 DELETE 400。RongNengRAG 前端删除按钮属仓库改动。

### 8.18 Qwen3-4B vLLM 配置迁移（2026-08-12）
- `config.yaml` 的本地模型条目已从 Qwen2.5-7B-Instruct 更新为 `Qwen3-4B`。
- 当前配置：`name: Qwen3-4B`、`model: /data/models/Qwen/Qwen3-4B`、`base_url: http://192.168.0.201:18000/v1`。
- `app/service/llm.py` 的无配置回退模型、`AskRequest.model` 注释和 Qwen 结构化报告测试同步更新。
- 旧版本曾按模型名包含 `qwen` 自动启用结构化报告模式；该行为已在 8.21 对比链路对齐中移除。
- 如果 vLLM 使用了不同的 `served-model-name`，应以 `/v1/models` 返回的精确 `id` 替换 `config.yaml` 中的 `name`/`model`。

### 8.19 Qwen3 Agent 流适配与能力测试（2026-08-12）
- `config.yaml` 为 Qwen3 vLLM 请求增加 `extra_body.chat_template_kwargs.enable_thinking: false`，减少 ReAct 工具调用中的格式噪声。
- `app/service/llm.py` 支持将可选 `extra_body` 透传到非流式和流式 Chat Completions 请求。
- `app/agent/react_loop.py` 在 `Action` 解析和公开回答前剥离 `<think>...</think>` / `<analysis>...</analysis>`，避免私有推理泄漏或干扰工具动作解析。
- Qwen3 专项测试覆盖：SQL 生成问答、结构化分析报告、`<think>` 包裹 Action、最终回答清理；共 9 项测试通过。

### 8.20 SQL 与 HTML 报告提示词强化（2026-08-12）
- `app/agent/workflow_prompt.py` 新增 SQL 分析协议：多单元 `UNION/UNION ALL` 必须携带 `unit` 标签；完整性统计必须返回 `total_rows`、`non_empty_count`、`null_count`、`distinct_count`，并校验数量关系；物料频次必须返回 `unit`、`material`、`occurrence_count`。
- 提示词禁止依据结果行顺序推断字段归属，避免把 `column_b`/`column_c` 的空值错误归到 `column_a`。
- HTML 报告提示词要求先完成概览、单元统计、物料频次和数据质量所需 SQL，再生成报告；所有数字必须能追溯到带标签的 SQL 结果。
- 旧版本曾强制 Qwen3 使用 `report_spec → terminate`，该模式现仅保留兼容代码，不参与默认模型对比。
- 进入 `large` 路由的 Qwen3 与 DeepSeek 使用同一 `html_interpreter` 报告链路；当前 Qwen3-4B/8B/14B 配置为 `small` 模板路由，只有显式覆盖为 `large` 才进入 Agent 链路。
- 回归测试覆盖提示词约束和错误 HTML 路径重试，共 11 项通过。

### 8.21 Qwen3/DeepSeek 公平对比链路对齐（2026-08-12）
- 移除按模型名自动选择 Qwen `report_spec` 协议的行为。
- `large` 路由下的 Qwen3 和 DeepSeek 共用同一份系统提示词、ReAct 工具集合、`sql_query → html_interpreter → terminate` 报告链路和终止逻辑；`small` 路由不调用 LLM，但沿用同一 HTML 落盘、SSE 和前端渲染契约。
- 增加闲聊/问候规则：不涉及工作簿的问题不调用 SQL 或报告工具。
- 旧 `structured_report` 参数仅保留兼容性，不再由模型名自动启用。

### 8.22 恢复 large 原有行为（2026-08-12）
- 双路由的边界明确为：`small` 使用 profile/模板/固定报告；`large` 继续使用原有自由 ReAct。
- 移除 8.20/8.21 引入的 large 专用 SQL/HTML 强制分析协议，避免改变 DeepSeek 的探索方式和报告风格。
- 恢复 large 运行预算：`max_loops=100`、`max_llm_calls=100`、`max_tool_calls=100`、`max_sql_calls=50`。
- XML 工具格式兼容和 Windows/WSL 路径兼容属于底层容错，不改变正常 large 模型调用协议。

### 8.23 修复大模型请求假死（2026-08-12）
- **现象**：DeepSeek 请求开始后数分钟没有 `react round` 或 `ask done` 日志。
- **根因**：同步 ReAct 调用使用 OpenAI SDK 默认 2 次重试，且 DeepSeek 单次超时为 300 秒；网络不可达时前端只收到初始 thinking 状态。
- **修复**：DeepSeek 配置改为 `timeout: 60`、`max_retries: 0`；`ExcelLLM` 将超时与重试预算显式传给 SDK；ReAct 增加 LLM 请求开始/结束日志。
- **边界**：只限制 provider 请求等待时间，不改变 large 的 prompt、工具、SQL 探索或 HTML 报告流程。
- **验证**：63 项单元测试通过；外网探测在当前环境 15 秒无响应，已确认必须 fail-fast，不能继续默认重试。
