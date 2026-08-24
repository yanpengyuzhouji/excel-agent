# Excel Agent API 文档

独立 FastAPI 微服务，端口默认 `127.0.0.1:8090`，前缀 `/api/v1`。OpenAPI 文档：`http://127.0.0.1:8090/docs`。

## 端点总览

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/v1/health` | 健康检查 |
| POST | `/api/v1/workbook-imports` | 上传（multipart: `file`, `source_channel`, 可选 `workbook_id`）→ 解析 → 草稿 |
| GET | `/api/v1/workbook-imports` | 列表（query: `status`, `limit`, `offset`） |
| GET | `/api/v1/workbook-imports/{id}` | 草稿摘要（sheets + validation） |
| GET | `/api/v1/workbook-imports/{id}/sheets/{sid}/rows` | 数据预览（query: `offset`, `limit`） |
| PATCH | `/api/v1/workbook-imports/{id}/draft` | 编辑草稿（revision 防并发，不符 → 409） |
| POST | `/api/v1/workbook-imports/{id}/validate` | 重新校验 |
| POST | `/api/v1/workbook-imports/{id}/confirm` | 确认建库（原子切换，激活版本） |
| POST | `/api/v1/workbooks/{wid}/versions/{v}/activate` | 版本回滚 |
| DELETE | `/api/v1/workbooks/{wid}` | 删除工作簿（元数据 + source/draft/database/reports 全部磁盘产物） |
| POST | `/api/v1/workbooks/{wid}/query` | 只读 SQL 查询 |
| POST | `/api/v1/workbooks/{wid}/ask` | SSE 双路由问答：DeepSeek/30B+ 为 ReAct，30B 以下默认走规则模板；可带 `conv_id` 续接多轮 |
| GET | `/api/v1/workbooks/{wid}/analysis-profile` | 通用数据画像（字段质量、语义候选、候选业务域） |
| GET | `/api/v1/workbooks/{wid}/reports/{rid}` | 获取 HTML 报告文件 |
| GET | `/api/v1/workbooks/{wid}/reports/{rid}/pdf` | 将 HTML 报告转换为 PDF 并下载 |
| GET | `/api/v1/workbooks/{wid}/conversations` | 历史会话列表 |
| GET | `/api/v1/workbooks/{wid}/conversations/{conv_id}` | 单会话消息列表（含工具调用记忆） |
| DELETE | `/api/v1/workbooks/{wid}/conversations/{conv_id}` | 删除单会话 |
| GET | `/ui` | 示例前端（单文件，静态挂载） |

## 上传

```bash
curl -X POST http://127.0.0.1:8090/api/v1/workbook-imports \
  -F "file=@materials.xlsx" -F "source_channel=agent"
```

`source_channel` ∈ `{"chat_excel", "agent"}`。返回 `WorkbookImportResponse`（含 `import_id`、`workbook_id`、`revision`、`sheets[]`、`validation`）。

## 编辑草稿（PATCH /draft）

```json
{
  "revision": 1,
  "sheet_updates": [
    { "sheet_id": "sheet_1", "enabled": true, "kind": "data",
      "table_name": "material_list", "header_row": 3, "data_start_row": 4 }
  ],
  "column_updates": [
    { "sheet_id": "sheet_1", "column_id": "sheet_1_column_1",
      "enabled": true, "display_name": "序号", "physical_name": "sequence_no",
      "sql_type": "INTEGER", "nullable": true }
  ],
  "cell_updates": [
    { "sheet_id": "sheet_1", "column_id": "sheet_1_column_1",
      "source_row": 4, "value": "EDITED" }
  ]
}
```

- `revision` 是乐观锁；与当前不符 → `409 Conflict`。
- `cell_updates` 定位方式：`source_row`（原始 Excel 行号，1 起始）+ `column_id`。编辑后该行 `edited_columns` 记录变更列，并反映到建库结果。

## 确认建库（POST /confirm）

```json
{ "revision": 1, "accepted_warning_codes": ["MERGED_CELLS", "HIDDEN_ROWS"] }
```

返回 `ConfirmResponse`：`{ import_id, workbook_id, version, status: "active", datasource_name, database_path }`。
有错误（`error_count>0`）或警告未全部勾选 → `400`。

## 只读 SQL 查询（POST /query）

```json
{ "sql": "SELECT COUNT(*) AS n FROM material_list", "max_rows": 200 }
```

返回 `SQLQueryResponse`：`{ sql, columns, rows, row_count, truncated, elapsed_ms }`。

**安全**：双重防护
1. `sqlglot` AST 解析只允许单条 `SELECT`/`WITH`（无 Insert/Update/Delete/Create/Drop/Alter/Command）
2. SQLite `set_authorizer` 在 C 层拒绝写操作
3. 连接以 `mode=ro` 只读打开

## SSE 问答（POST /ask）

```json
{ "question": "生成工作簿通用数据分析报告", "max_rows": 200, "model": "Qwen3-14B" }
```

**多轮会话**：可选传 `conv_id` 续接已有会话（不传则自动新建，`done` 事件会回传 `conv_id`）：

```json
{ "question": "那最大的类别呢？", "max_rows": 200, "conv_id": "abc123" }
```

`model` 可选，匹配 `excel.llms[].name`。也可以用 `analysis_route: "large"` 或
`analysis_route: "small"` 做受控路由覆盖。默认规则是：DeepSeek 和显式配置为
`large` 的模型继续使用当前 ReAct；模型规模 `< 30B` 默认使用确定性的
`profile → 标准模板 → facts/findings → 固定 HTML` 链路。

每次问答都持久化到会话：用户问题（`user`，包含本轮模型元数据）、AI 回答（`ai`）、每步工具调用（`tool`，含 Action/Observation）。会话同时保存最近一次实际使用的 `model_name`、`model_label`、`model_id`、`analysis_route`、`analysis_reason` 和 `model_size_b`，因此刷新页面后仍可显示会话模型和路由。历史会话经 `GET /workbooks/{wid}/conversations` 查看。

返回 `text/event-stream`，每事件 `data: {json}`，带 `type` 字段。

**Manus 风格 step 事件**（示例前端消费，用于实时回显工具调用/代码/报告）：

| type | 字段 | 说明 |
|---|---|---|
| `step.start` | `step`, `id`, `title`, `detail` | 工具步骤开始（title=工具名，如 sql_query / html_interpreter） |
| `step.meta` | `id`, `thought`, `action`, `action_input` | 思考 + 工具调用参数 |
| `step.chunk` | `id`, `output_type`, `content` | 实时增量；`output_type`=`code`(SQL/HTML 源码)、`table`(结果)、`html`(报告含源码)、`error` |
| `step.done` | `id`, `status` | 步骤完成（done/failed） |

**旧事件**（RongNengRAG 前端消费，向后兼容保留）：

| type | 字段 | 说明 |
|---|---|---|
| `status` | `status` | `"thinking"` |
| `analysis_route` | `route`, `reason`, `model_size_b`, `model_name`, `model_label`, `model_id` | 实际选用的路由和会话模型；`session`/`done` 事件均会携带 |
| `profile` | `table_count`, `row_count`, `column_count`, `candidate_domains` | 小路由的通用数据画像摘要 |
| `finding` | `finding_id`, `severity`, `title`, `evidence` | 可复核规则洞察 |
| `report_spec` | `report` | 固定 HTML 渲染前的结构化报告 |
| `sql` | `sql`, `columns`, `rows`, `row_count`, `truncated` | 执行过的 SQL 与结果（`error` 字段表示失败） |
| `table` | `columns`, `rows`, `row_count` | 供前端渲染表格 |
| `plan.update` | `tasks[]` | ReAct 任务计划（`content`/`status`/`priority`） |
| `html` | `title`, `report_id`, `url`, `html`（可选） | HTML 报告已生成；`html` 与落盘文件一致，前端可直接 `srcdoc` 渲染，`url` 用于刷新/下载 |
| `done` | `full_answer`, `report_url`, `report_title` | 结束 |
| `error` | `error`, `done: true` | 出错终止 |

前端用 `ReadableStream` 逐行解析 `data: ` 前缀即可（见示例前端）。

## 配置（config.yaml）

```yaml
server:
  host: "127.0.0.1"
  port: 8090
  storage_path: "data/workbook_imports"   # 工作簿/草稿/库/报告存储根
  preview_page_size: 100
  query_row_limit: 500
  max_import_bytes: 209715200             # 200 MB 上传上限（防护）
  max_import_rows: 300000                 # 单 sheet 行数上限（防护）
excel:
  llm:                                    # Excel 专用 LLM（OpenAI 兼容 /v1）
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-v4-flash"
    api_key: ""
    api_key_env: "DEEPSEEK_API_KEY"      # 优先读环境变量; 未设置则回退读 env_file
    env_file: "D:/git/DB-GPT-0.8.1/DB-GPT-0.8.1/.env.deepseek"  # 共享 .env（key 不入 config.yaml）
    temperature: 0.1
    max_tokens: 4096
    timeout: 60
    max_retries: 0
  agent:
    max_loops: 100       # ReAct 最大轮次（复杂分析 + 生成 HTML 报告需要充足轮次）
    sql_max_rows: 500
```

## 依赖

```
fastapi, uvicorn[standard], python-multipart, pydantic,
sqlalchemy, sqlglot, openpyxl, openai, weasyprint, PyYAML
```

存储：元数据库 `data/workbook_imports/workbook_meta.db`（SQLAlchemy）；每工作簿一个 SQLite 业务库 `data/workbook_imports/{workbook_id}/v{version}/database.sqlite`；报告 `.../{workbook_id}/reports/{report_id}.html`。

## 启动

```bash
python -m venv .venv
.venv\Scripts\activate        # Windows
pip install -r requirements.txt
python run.py                 # 或 uvicorn app.main:app --host 127.0.0.1 --port 8090
```

## 错误码映射

| 状态 | 场景 |
|---|---|
| 400 | 参数/校验失败（含上传超限、行数超限、SQL 非只读） |
| 403 | 权限（保留字段，当前单用户不触发） |
| 404 | import/workbook/报告不存在 |
| 409 | revision 乐观锁冲突 |
| 500 | 未预期异常 |
| 422 | Pydantic 校验失败 |
