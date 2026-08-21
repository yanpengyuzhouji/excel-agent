# 通用 Excel 数据分析双路由实施方案

> 状态：执行中
>
> 目标：在不知道 Excel 属于物料、采购、设备清单还是其他业务类型时，先完成通用数据分析能力；保留当前 DeepSeek 的大模型自由分析链路，同时新增面向 30B 以下模型的确定性规则/模板链路。

## 1. 本次方案的边界

### 1.1 输入类型不预设

系统不能把工作簿默认解释成“物料清单”。类型识别只提供置信度和候选标签，不阻断通用分析：

- 通用层：工作簿、工作表、记录、字段、空值、重复、唯一值、数值、日期、分布、异常、关联线索。
- 候选域：物料、采购、设备、项目/计划、人员/组织、财务/金额、时间序列、未知。
- 领域层：只有语义匹配达到阈值时才启用领域规则；领域规则不能改变通用事实的原始含义。

### 1.2 两条分析路由

```text
用户问题 / 报告请求
          │
          ├─ 路由显式指定 large，或模型 >= 30B，或 DeepSeek
          │       └─ large：保留当前 ReAct，自由生成 SQL + HTML
          │
          └─ route=small（默认用于 30B 以下模型）
                  └─ profile → 语义字段 → 标准指标模板
                     → 规则 findings/facts → 结构化 Report JSON
                     → 固定主题渲染 HTML
```

路由依据优先级：请求/配置显式值 > DeepSeek 提供商识别 > 模型参数规模。小模型路由的默认阈值为 `< 30B`；配置可覆盖，不把模型名称猜测当成安全边界。

### 1.3 事实来源约束

- 行数、唯一数、空值率、数值率、日期范围、重复、排名、比率由 SQLite/Python 确定性计算。
- 模型不能产生事实数字，也不能把 `/`、`-`、`N/A` 等占位符默认为 0。
- Report JSON 只能引用 facts/findings 中已存在的 `fact_id`/`finding_id`。
- HTML 默认由固定渲染器生成；大模型路由保留现有 `html_interpreter`，但继续经过清洗。

## 2. 当前基线与改造点

已确认的当前基线：

- [x] FastAPI、SQLite、导入草稿/审核/确认链路可用。
- [x] 只读 SQL 已有 SQLite authorizer、SQL AST 和超时/VM 步数限制。
- [x] 现有 Agent 工具为 `sql_query`、`html_interpreter`、`todowrite`、`terminate`。
- [x] DeepSeek 与 Qwen 等模型当前共用一份 ReAct 提示词和自由 HTML 生成链路。
- [x] 仓库已有物料、采购、设备/配电等历史工作簿，可作为通用分析回归样本。
- [x] 缺少统一的工作簿数据画像和字段语义合同（已由 Phase 1 开始补齐）。
- [x] 缺少标准指标模板、事实集、规则洞察和结构化报告协议（已由 Phase 2 补齐首版）。
- [x] 缺少按模型/提供商选择 large/small 的路由协议（已接入 `/ask`）。
- [x] 缺少固定主题报告渲染器和报告事实引用校验（已接入小路由）。

## 3. 目标模块设计

### 3.1 数据画像 `app/analysis/profile.py`

输出 `WorkbookProfile`（JSON 可序列化）：

```json
{
  "workbook_id": "...",
  "tables": [{
    "table": "sheet1",
    "display_name": "Sheet1",
    "row_count": 49,
    "columns": [{
      "name": "quantity",
      "label": "数量",
      "sql_type": "TEXT",
      "semantic_type": "numeric_candidate",
      "null_count": 25,
      "placeholder_count": 3,
      "distinct_count": 18,
      "numeric_rate": 0.49,
      "examples": ["12", "/"]
    }]
  }],
  "candidate_domains": [{"name": "material", "score": 0.82}],
  "warnings": []
}
```

画像只依赖已确认数据库和受限 SQL；字段语义由通用词典、值形态和列名匹配给出候选，不把候选当作事实。

### 3.2 标准指标模板 `app/analysis/templates.py`

第一批模板覆盖所有工作簿：

1. 工作表/记录概览；
2. 字段质量（空值、占位符、唯一率、数值可用率）；
3. 文本/数值/日期候选分布；
4. 重复记录和高重复字段；
5. 数值候选的总和、最小值、最大值、异常文本；
6. 日期候选的最小/最大日期和时间覆盖；
7. 候选主键、候选外键以及一对多/多对一线索；
8. 域候选的轻量扩展（物料编码、采购批次、设备编码等），不覆盖通用结果。

模板必须通过字段白名单生成 SQL，并经过既有只读 SQL 执行器；不把用户输入直接拼到 SQL。

### 3.3 规则事实层 `app/analysis/findings.py`

规则输出：

- `facts`：原子事实，如 `row_count`、`null_rate`、`distinct_count`、`top_values`；
- `findings`：有证据的判断，如“字段 A 缺失率高”“字段 B 可能是候选标识字段”；
- `evidence`：事实 ID、实际值、来源表/字段；
- `severity`：`info`、`low`、`medium`、`high`；
- `confidence`：仅表示规则匹配置信度，不表示业务结论确定性。

规则先找问题，报告层再决定表达。任何规则都必须能在没有 LLM 的情况下测试和复现。

### 3.4 小模型报告链路 `app/analysis/small_route.py`

默认流程：

```text
profile_workbook
  → run_standard_templates
  → derive_findings
  → build_report_spec（结构化 JSON）
  → validate_report_spec
  → render_report_html（固定主题）
```

小模型如需参与，只允许在已验证 facts/findings 上做标题、摘要、解释和建议；第一版采用确定性摘要作为兜底，避免模型不可用导致报告失败。

### 3.5 大模型链路 `app/agent/react_loop.py`

- DeepSeek 继续使用当前 ReAct 自由链路。
- 统一注入通用 profile/数据合同，减少对原始字段的误读。
- 当前 SQL/HTML 工具和历史会话协议保持兼容。
- 后续可把 profile 作为上下文增强，但不改变大模型路由默认行为。

### 3.6 固定报告渲染 `app/analysis/report.py`

首版提供 `executive`、`data-quality`、`generic-analysis` 三个主题。模型/规则只提供内容和数据，渲染器负责：

- 指标卡、风险标签、章节、表格和图表数据展示；
- HTML 转义、长度限制、禁止脚本/外链；
- 报告标题和文件名安全处理；
- 打印/窄屏布局的固定 CSS。

## 4. 路由协议

### 4.1 配置

每个模型可选：

```yaml
analysis_route: large   # large | small
model_size_b: 14
provider: modelscope
```

`large` 继续走现有 Agent；`small` 走规则模板。显式配置优先于自动判断。

### 4.2 API 兼容

- 现有 `/ask` 请求不变，`model` 仍可选。
- SSE 新增 `route`、`profile`、`facts`、`finding`、`report_spec` 事件仅供增强前端使用；保留 `sql`、`html`、`done`。
- 已新增 `/workbooks/{id}/analysis-profile` 只读接口，便于调试和验收。

## 5. 分步执行清单

### Phase 0：基线盘点

- [x] 盘点导入、物化、SQL、ReAct、LLM 配置和 `/ask` 入口。
- [x] 确认当前所有模型共用大模型自由 SQL/HTML 链路。
- [x] 确认仓库已有多业务类型工作簿样本。

### Phase 1：通用数据画像与数据合同

- [x] 新增 profile 数据结构和 SQLite 确定性画像器。
- [x] 新增通用字段语义词典、占位符归一化和候选域评分。
- [ ] 将 profile 持久化到版本目录，或按需可重建并保证结果一致。
- [x] 为空表/混合数字文本/manifest 缺失场景补首批测试；多表、宽表和日期字段回归仍待补齐。

### Phase 2：标准事实、模板和规则洞察

- [x] 新增标准指标模板执行器和 SQL 字段白名单。
- [x] 新增 facts/findings 结构与证据引用。
- [x] 新增首版通用质量、候选标识、数值内容和候选域规则；重复/日期/关联规则待补。
- [x] 新增域候选规则，但不把领域假设写入通用结论。

### Phase 3：小模型规则模板路由

- [x] 增加模型路由元数据、自动判断和显式覆盖。
- [x] 实现小模型链路：profile → templates → findings → report spec。
- [x] 固定主题渲染 HTML，新增报告规范校验和事实引用校验。
- [x] 接入 `/ask`，保留 SSE/会话兼容字段，并新增 `analysis_route`/`profile`/`finding`/`report_spec` 事件。

### Phase 4：DeepSeek 大模型路由兼容

- [x] 明确 DeepSeek 的 `large` 配置并保持当前 ReAct 行为。
- [ ] 给 large 路由增加 profile/数据合同上下文，不改变自由分析能力。
- [x] 验证大模型路由不会误进入模板链的路由选择规则，并通过 SSE 暴露路由信息；真实 DeepSeek 回归待联网环境执行。

### Phase 5：测试、回归和验收

- [x] 通用 profile 单元测试覆盖首批边界样本。
- [x] 小路由报告数字全部来自 facts，非法引用校验可拒绝。
- [x] HTML 固定渲染、无脚本/外链资源；最终 sanitizer 回归仍需在完整依赖环境执行。
- [ ] 同一工作簿大/小路由输出事实一致。
- [x] 运行现有全量测试和新增双路由测试：`.venv-linux/bin/python -m unittest discover -s tests -v`，61 项全部通过。
- [x] 更新 README/API 文档和本文件的当前限制、回滚点；未完成项仍保留为后续执行清单。

### Phase 6：示例前端双路由对接

- [x] 模型下拉展示后端返回的 `small/large` 路由，并支持自动/规则模板/模型 Agent 覆盖。
- [x] 请求体发送 `analysis_route`，保持原有 `model`、会话和 SQL/HTML 兼容。
- [x] 接收并展示 `analysis_route`、`profile`、`finding`、`report_spec` SSE 事件。
- [x] 对小路由直接 `html` 事件自动切换到报告预览，对大模型 `step.*` 事件保持原展示。
- [x] 更新示例前端说明，完成 JavaScript 语法检查和后端相关回归测试。

### Phase 7：故障修复与跨环境回归

- [x] 修复 DeepSeek 返回 XML 风格 `<todowrite>` 时被误判为最终答案的问题。
- [x] 修复规则路由已生成 HTML、但前端 `report` 步骤未进入详情渲染的问题。
- [x] 兼容 Windows 元数据路径在 WSL/Linux 当前存储根下恢复 `v{version}` SQLite 产物。
- [x] 保留 XML 工具格式纠正；large 恢复原始 terminate 语义，不增加强制回退报告。
- [x] 使用现有工作簿完成真实规则链 smoke：7 张表、2480 条记录，并生成 `/reports/` HTML。

### Phase 8：恢复 large 原有行为

- [x] 移除新增的 large 专用 SQL/HTML 强制分析协议，恢复自由 ReAct prompt。
- [x] 恢复 large 的模型调用余量：`max_loops=100`、`max_llm_calls=100`、`max_tool_calls=100`。
- [x] large 恢复原始 `terminate` 语义；规则模板链不受影响。

### Phase 9：大模型 provider 超时保护

- [x] 定位 DeepSeek 卡顿发生在首个 HTTP 请求返回前，而不是 SQL/HTML 工具链。
- [x] DeepSeek 使用 60 秒超时、0 次 SDK 自动重试，provider 异常通过原有 SSE error 返回。
- [x] 增加 `react llm start/done` 日志，区分模型等待与 ReAct/工具执行。
- [x] 不修改 large 的自由 ReAct 提示词、工具协议和报告生成路径。
- [x] 保留 XML 工具解析和跨 Windows/WSL 产物路径修复，不改变正常模型输出。
- [x] 全量 61 项测试通过。

## 6. 验收指标

| 维度 | 第一阶段目标 |
|---|---:|
| profile 可生成率 | 100%（合法已确认工作簿） |
| 数字事实来源 | 100% 来自 SQLite/Python |
| 小路由非法字段/SQL | 0 |
| 小路由事实引用缺失 | 0 |
| 必备通用章节 | 100% |
| HTML 危险标签/外链脚本 | 0 |
| 大路由兼容回归 | 现有测试不退化 |

## 7. 当前执行记录

| 时间 | 阶段 | 结果 |
|---|---|---|
| 2026-08-12 | Phase 0 | 完成仓库和现有链路盘点，开始 Phase 1 |
| 2026-08-12 | Phase 1 | 完成通用 profile、语义提示、候选领域评分和首批单测 |
| 2026-08-12 | Phase 2/3 | 完成标准模板、facts/findings、固定报告、模型路由和 `/ask` 接入 |
| 2026-08-12 | Phase 5 | WSL `.venv-linux` 依赖安装完成；YAML/真实 SQLite smoke 通过；全量 61 项测试通过 |
| 2026-08-12 | Phase 7 | 定位并修复 XML 工具调用误判、规则报告前端不展示、Windows/WSL 数据库路径失效；真实工作簿规则报告 smoke 通过 |
| 2026-08-12 | Phase 6 | 示例前端完成双路由状态、画像、规则线索、Report JSON 和直接 HTML 事件对接 |
