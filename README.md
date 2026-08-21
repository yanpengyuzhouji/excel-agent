# Excel Agent

独立轻量 FastAPI 微服务，抽取自 DB-GPT 0.8.1 的「Excel 工作簿」功能流（原地升级，含 2026-08-07 的流式解析 OOM 修复）：

**上传 Excel → 解析生成审核草稿 → 人工审核(改表名/字段/类型/单元格/确认警告)→ 确认建 SQLite 库 → Text2SQL 只读问答 → ReAct 循环 → 生成 HTML 分析报告**

供 RongNengRAG 知识库问答系统调用，实现 **Excel 智能问答** 与 **分析报告生成** 两个功能。

- **端口**: `127.0.0.1:8090`(默认,仅本机访问)
- **元数据库**: `data/workbook_imports/workbook_meta.db`
- **业务库**: 每个 workbook 一个 SQLite(`data/workbook_imports/{workbook_id}/v{version}/database.sqlite`)
- **报告**: `data/workbook_imports/{workbook_id}/reports/{report_id}.html`（PDF 通过同路径追加 `/pdf` 下载）

PDF 字体：服务端会自动查找 Noto Sans CJK、文泉驿和 WSL 的微软雅黑/宋体，并为报告中的 Emoji 图标查找 Segoe UI Emoji/Noto Color Emoji。部署环境若没有这些字体，请设置 `EXCEL_REPORT_PDF_FONT_PATH`（中文）或 `EXCEL_REPORT_PDF_EMOJI_FONT_PATH`（图标），也可在 `server.pdf_font_path` 中填写中文 `.ttf`、`.otf` 或 `.ttc` 字体路径；否则中文报告会返回明确的字体缺失错误，不会生成乱码 PDF。
- **示例前端**: `http://127.0.0.1:8090/ui`(单文件,独立演示用)

## 启动

```bash
# 1. 安装依赖
python -m venv .venv
.venv\Scripts\activate          # Windows
pip install -r requirements.txt

# 2. 配置环境变量（首次）
copy .env.example .env          # Windows
# cp .env.example .env          # Linux
# 编辑 .env 填入 DEEPSEEK_API_KEY

# 3. 启动（默认监听 0.0.0.0:8090，局域网可访问）
python run.py
```

WSL/Linux 使用仓库内的 `.venv-linux`：

```bash
source .venv-linux/bin/activate
python -m unittest discover -s tests -v
python run.py
```

> 完全独立：自带 `.env` 配置，不依赖 DB-GPT / RongNengRAG。局域网访问用 `http://<本机IP>:8090/ui`。

## 配置 `config.yaml`

```yaml
server:
  host: "127.0.0.1"
  port: 8090
  storage_path: "data/workbook_imports"   # 工作簿/草稿/库/报告存储根
  preview_page_size: 100
  query_row_limit: 500
  max_import_bytes: 209715200             # 200 MB 上传上限（防 OOM 防线一）
  max_import_rows: 300000                 # 单 sheet 行数上限（防线二）
excel:
  llm:                                   # Excel 专用 LLM(OpenAI 兼容 /v1)
    base_url: "https://api.deepseek.com/v1"
    model: "deepseek-v4-flash"
    api_key: ""
    api_key_env: "DEEPSEEK_API_KEY"      # 优先读环境变量; 未设置则回退读 env_file
    env_file: "D:/git/DB-GPT-0.8.1/DB-GPT-0.8.1/.env.deepseek"   # 共享 .env(key 不入 config)
    temperature: 0.1
    max_tokens: 4096
    timeout: 60                # provider failure is surfaced within one minute
    max_retries: 0              # do not hide a failed request behind SDK retries
  agent:
    max_loops: 100       # ReAct 最大轮次(复杂分析+HTML报告需充足轮次)
    sql_max_rows: 500
```

## API

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/v1/workbook-imports` | 上传(multipart:`file`,`source_channel`)→ 解析 → 草稿 |
| GET | `/api/v1/workbook-imports` | 列表(前端「我的 Excel 库」) |
| GET | `/api/v1/workbook-imports/{id}` | 草稿摘要(sheets + validation) |
| GET | `/api/v1/workbook-imports/{id}/sheets/{sid}/rows` | 数据预览(分页) |
| PATCH | `/api/v1/workbook-imports/{id}/draft` | 编辑草稿(revision 防并发,不符 → 409) |
| POST | `/api/v1/workbook-imports/{id}/validate` | 重新校验 |
| POST | `/api/v1/workbook-imports/{id}/confirm` | 确认建库(原子切换,激活版本) |
| POST | `/api/v1/workbooks/{wid}/versions/{v}/activate` | 版本回滚 |
| DELETE | `/api/v1/workbooks/{wid}` | 删除工作簿(元数据+所有磁盘产物) |
| POST | `/api/v1/workbooks/{wid}/query` | 只读 SQL 查询(SELECT 仅限,authorizer+sqlglot 双重防护) |
| POST | `/api/v1/workbooks/{wid}/ask` | SSE 双路由问答：DeepSeek/30B+ 保留 ReAct，自 30B 以下模型默认走规则模板报告；可带 `conv_id` 续接多轮 |
| GET | `/api/v1/workbooks/{wid}/analysis-profile` | 获取通用数据画像、字段质量与候选业务域 |
| GET | `/api/v1/workbooks/{wid}/reports/{rid}` | 获取 HTML 报告文件 |
| GET | `/api/v1/workbooks/{wid}/reports/{rid}/pdf` | 将 HTML 报告转换为 PDF 并下载 |
| GET | `/api/v1/workbooks/{wid}/conversations` | 历史会话列表 |
| GET | `/api/v1/workbooks/{wid}/conversations/{conv_id}` | 单会话消息列表(含工具调用记忆) |
| DELETE | `/api/v1/workbooks/{wid}/conversations/{conv_id}` | 删除单会话 |
| GET | `/api/v1/health` | 健康检查 |

### `/ask` SSE 事件(均带 `type` 字段)

```
data: {"type":"status","status":"thinking","done":false}
data: {"type":"analysis_route","route":"small|large","reason":"..."}
data: {"type":"sql","sql":"...","columns":[...],"rows":[...],"row_count":N,"truncated":false}
data: {"type":"table","columns":[...],"rows":[...],"row_count":N}
data: {"type":"profile","table_count":N,"row_count":N,"column_count":N}
data: {"type":"finding","finding_id":"FND-001","severity":"medium","title":"...","evidence":["FCT-..."]}
data: {"type":"html","title":"...","report_id":"...","url":"/api/v1/workbooks/{wid}/reports/{rid}"}
data: {"type":"plan.update","tasks":[...]}
data: {"type":"done","done":true,"full_answer":"...","report_url":"..."}
data: {"type":"error","error":"...","done":true}
```

完整接口契约见 [docs/API.md](docs/API.md)。

## 与 RongNengRAG 集成

RongNengRAG 通过 `src/api/excel_proxy.py` 把 `/excel/*` 反向代理到本服务（默认 `127.0.0.1:8090`），前端组件已完整覆盖全部功能，**API 兼容即可零改动接入**。详见 [docs/INTEGRATION_RONGNENGRAG.md](docs/INTEGRATION_RONGNENGRAG.md)。

## 设计要点

- **移植来源**: `parser.py` / `materializer.py` 原样移植自 DB-GPT(零 dbgpt 依赖);`service.py` 重写(去 SystemApp/Result/_register_datasource/多用户)。
- **流式解析**(2026-08-07 同步): `load_workbook(read_only=True)` 双流式读取,内存较旧版降 ~93%;merged/hidden 布局经 `_sheet_metadata_from_xml`(iterparse)轻量回收。
- **防护阈值**: `max_import_bytes`(200MB) / `max_import_rows`(30万),超限报明确错误而非 OOM。
- **多用户**: 已移除(所有用户共用一套存储,服务仅绑 127.0.0.1)。
- **安全**: 只读 SQL 双保险(sqlite authorizer + sqlglot AST 只允许单条 SELECT);ReAct 仅 4 个白名单工具,无任意代码执行。
- **双路由**: `excel.llm/llms` 可用 `analysis_route: large|small` 显式配置；未配置时 DeepSeek 和 30B+ 走原 ReAct，自 30B 以下或未知规模模型走通用 profile/模板/facts/findings/固定 HTML 链路。小路由不让模型自由生成 SQL/CSS。
- **与 DB-GPT 的有意裁剪**: 未移植 `code_interpreter` / `shell_interpreter`(任意代码执行是安全与复杂度黑洞;本链路聚合用 SQL `GROUP BY/COUNT/SUM` 表达)。若未来需要,可在 `agent/tools.py` 增补并重新加入 `workflow_prompt` 工具清单。
- **Windows**: sqlite file URI 使用 `.as_posix()` 规避反斜杠问题。
- **跨环境恢复**: 若元数据库保留了 Windows 绝对路径、但服务在 WSL/Linux 启动，会按当前存储根和版本目录自动解析 `source/draft/database` 产物。

## 测试

```bash
# 全流程冒烟(上传→编辑(含单元格)→confirm→只读查询/拒绝写→防护阈值):
.venv\Scripts\python scripts\smoke_test.py
```

## 文档

- [docs/IMPLEMENTATION.md](docs/IMPLEMENTATION.md) — 实现全程记录(需求/决策/基线/分阶段/差异/限制/回滚)
- [docs/DEEPSEEK_AGENT_ROUTE.md](docs/DEEPSEEK_AGENT_ROUTE.md) — DeepSeek API Agent 路由架构、ReAct 工具链、SSE 与会话闭环
- [docs/API.md](docs/API.md) — 接口契约(端点/SSE/配置/依赖/启动/错误码)
- [docs/INTEGRATION_RONGNENGRAG.md](docs/INTEGRATION_RONGNENGRAG.md) — 与 RongNengRAG 集成说明 + 可选增强
- [examples/frontend/README.md](examples/frontend/README.md) — 示例前端使用说明
