# Excel Agent × RongNengRAG 集成说明

RongNengRAG（`D:\git\RongNengRAG`）通过反向代理接入 Excel Agent（`excel-workbook-service`），实现 Excel 智能问答与分析报告生成。

## 架构

```
浏览器 (Vue3, :5174)
  │
  ▼
RongNengRAG 后端 (FastAPI, :8000)
  /excel/* ──▶ excel_proxy.py (httpx 反向代理)
  │
  ▼
Excel Agent (127.0.0.1:8090)
  /api/v1/workbook-imports / drafts / confirm / query / ask / reports
```

## 代理转发（已存在，无需改动）

`src/api/excel_proxy.py` 把 `/excel/*` 1:1 转发到 `http://127.0.0.1:8090/api/v1`：

- 普通请求：整体转发，透传状态码与 content-type
- SSE 端点（路径以 `/ask` 结尾）：httpx 流式直通，防缓冲
- 目标地址可用环境变量 `EXCEL_SERVICE_BASE` 覆盖（默认 `http://127.0.0.1:8090`）

## 前端零改动前提

RongNengRAG 前端组件（`src/ui-vue2/src/excel/`）已完整覆盖全部功能，**前提是 API 保持兼容**：

| 组件 | 功能 | 依赖端点 |
|---|---|---|
| `ExcelWorkbench.vue` | 工作簿列表 + 上传 | GET/POST `/workbook-imports` |
| `ExcelDraftReview.vue` | 审核：Sheet/字段/预览/警告/确认 | GET `/{id}`、GET `.../rows`、PATCH `/draft`、POST `/validate`、POST `/confirm` |
| `ExcelQA.vue` | 问答：SSE 流式 | POST `/workbooks/{wid}/ask` |
| `HtmlReportViewer.vue` | 报告 iframe | GET `/workbooks/{wid}/reports/{rid}` |

`src/ui-vue2/src/api.js` 已封装全部 Excel API；`sse.js` 的 `consumeStream` 支持类型化事件（`sql/table/html/plan.update/done/error`）。

**兼容红线**（本次升级已守住）：端点路径、schema 字段名、SSE 事件格式均未变，仅新增配置项和 `/ui` 静态挂载。

## 可选增强（当前未实现，均属 RongNengRAG 仓库改动，不在本项目范围）

### 1. `ExcelDraftReview.vue` 不发 `cell_updates`（单元格编辑缺失）

- **现状**：后端 `update_draft` 已完整支持 `cell_updates`（service.py），前端预览表只读、无编辑 UI。
- **后端已支持证据**：`docs/API.md` 的 PATCH /draft 示例含 `cell_updates`；smoke_test 验证单元格编辑后建库反映编辑值。
- **前端改造要点**：在数据预览 `el-table` 的单元格中嵌入 `<el-input>` 或监听 `edit` 事件，blur/change 时调用：

  ```js
  updateDraft(importId, {
    revision: current.revision,
    cell_updates: [{ sheet_id, column_id, source_row, value }],
  })
  ```

  成功后用返回值更新本地 draft（revision 会 +1）。

### 2. `ExcelQA.vue` 不渲染 `plan.update`（任务列表缺失）

- **现状**：后端 `react_loop.py` 已发 `{type:"plan.update","tasks":[...]}`；前端第 121-123 行注释 `// could render a task plan; ignored for now`。
- **前端改造要点**：`onEvent` 里 `event.type === 'plan.update'` 时，把 `event.tasks` 渲染为 `el-steps` 或列表即可。

## 验证清单

1. 启动 Excel Agent（`python run.py`，127.0.0.1:8090）
2. 启动 RongNengRAG 后端（`uvicorn api.main:app --port 8000`）
3. 启动 RongNengRAG 前端（`npm run dev`，5174）
4. 导航「Excel 分析」→ 上传 → 审核 → 确认建库 → 问答 → 报告
5. 若 Excel 问答报 LLM 相关错误，检查 `config.yaml` 的 `excel.llm.base_url` 是否可达
