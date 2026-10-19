# 会话附件接口协议

所有路径以 `/api/v1` 为前缀。当前默认关闭鉴权，请求无需 Authorization 或 X-API-Key。访问者共用 anonymous 空间，可查看共享会话、附件和报告。OpenAPI 位于 `/docs`；只读 SQL、路径校验和本次问答的附件范围限制仍保留。

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | /agent/conversations | 创建空会话，JSON `{"title":"新对话"}` |
| GET | /agent/conversations | 当前身份会话历史 |
| GET | /agent/conversations/{id} | 历史消息、scope.attachments、查询范围、上下文 |
| PATCH | /agent/conversations/{id} | 重命名或置顶，JSON `{"title":"标题"}` / `{"pinned":true}` |
| DELETE | /agent/conversations/{id} | 删除消息、独占附件及报告，保留共享引用 |
| POST | /agent/conversations/{id}/attachments | multipart 字段 file，自动解析和建库 |
| POST | /agent/conversations/{id}/attachments/{import_id}/confirm | 严重告警确认，JSON revision、accept_severe_warnings |
| DELETE | /agent/conversations/{id}/attachments/{import_id} | 移除附件，清理无其他引用的工作簿 |
| POST | /agent/ask | POST SSE 流式问答 |
| GET | /agent/conversations/{id}/reports/{report_id} | 安全 HTML 报告 |
| GET | /agent/conversations/{id}/reports/{report_id}/pdf | PDF（需要原生依赖） |
| GET | /models | 已保留的模型配置，不是实时供应商健康保证 |
| GET | /capabilities | PDF 原生运行库是否可加载及附件能力 |
| GET | /health | 存活检查，不包含模型推理健康 |

`/health` 返回 `{"status":"ok","version":"1.2.0-rc.8"}`；version是服务软件版本，
不是附件版本，不必显示在聊天界面。实际返回值随部署镜像变化。

附件响应包含 import_id、workbook_id、filename、version、revision、status、errors、severe_warnings、warnings。status 为 ready / needs_review / blocked。ready 才能问数；硬错误不允许强制建库。当前每会话最多 5 个附件，默认一次执行最多查询 3 个工作簿。

删除响应附带 deleted_workbooks、retained_shared_workbooks、cleanup_warnings。清理被运行中的分析/上传阻止时返回 429，不修改记录。文件暂存失败时事务回滚；提交后暂存文件删除失败会明确返回 cleanup_warnings，并在下次启动时重试。所有文件路径校验限定在服务存储目录内，不删除用户电脑上原始文件所在目录。

问答请求：

```json
{"conv_id":"会话ID","question":"统计金额","model":null,"scope":{"mode":"selected","workbook_ids":["附件工作簿ID"]},"max_rows":500}
```

新附件会话必须使用 selected 范围，且工作簿必须是本会话 ready 附件。历史会话保持旧接口兼容。scope.attachments 会跨问答轮次保留。只读 SQL、资源预算和版本快照校验沿用原执行引擎。

`scope: {"mode":"selected","workbook_ids":[]}` 表示通用问答，不加载目录、不运行问数 Agent，也不生成文件报告。该模式支持历史对话与自动上下文压缩，流式事件新增 `answer.delta`（text 为增量文本），终止事件 `done.analysis_route` 为 general。非空附件范围仍走 agent；已有附件但主动取消所有勾选也可选择通用问答。

SSE 使用 `data: {JSON}\n\n`，事件 type 包括 session、scope.resolved、step.start、step.done、context.usage、context.compacted、html、done、error。step.done 的完成仅代表工具步骤完成；done/error 是本轮终止事件。断流无终止事件应显示失败，不得视为完成。客户端使用 POST fetch 流式读取；Spring Boot 不可缓存整个响应后再返回。

模型调用失败的error事件提供安全的`error`说明、`error_code`、`request_id`、
`retryable`、`partial_output`。按request_id关联容器日志，不向用户展示上游原始错误体。
retryable不是客户端自动重发POST的指令；服务已对无正文做最多一次有界恢复，
已有部分正文不自动重放。详见[模型诊断协议](model-request-diagnostics.md)。

当前未提供可靠断线续传、后台任务取消、附件自动 TTL 和跨副本会话协调。正式接入时 Spring Boot 保存用户与 conv_id 的归属关系并逐次授权；Excel 服务共享模式未实现用户级 JWT 委托。

## 地址与协议

## 数据证据与精度边界（RC28）

RC29增加内部工具profile_workbook（授权列的总行/NULL/空白/非空统计）及compare_totals（完整缓存多行的两期总额、差额、增长率），仍复用查询缓存与现有SSE事件。公开问答入参不变。RC30将供应商Arrearage分类为quota_exhausted并说明账户计费异常；这类错误不因HTTP400而误报成模型参数错误，也不自动重试。同一账户切换模型不保证恢复。

附件Agent默认查询优先，不要求预先声明分析计划。模型通过缓存结果ID、零基行号、物理列名引用数据；程序验证引用存在、输入完整性及支持的运算，统一解析回答与HTML/PDF。只有明确要求报告才保存报告。`done.publication` 增加 `references`、`evidence_ids`、`unit_normalizations`、`validation_scope`，历史消息保留相同审计。该审计证明引用来自工具，**不证明模型的SQL口径、文字解释或全部业务结论正确**。

未定义有效性口径时，提示模型以导入后可查询的记录为默认范围；缺少某个描述字段不自动删除整条记录。该约定不是代码按关键词过滤数据。计算工具支持整列求和与指定缓存单元格求和，二者不混用；空输入与零分母分别保留缺失/未定义状态，不伪造0。

SQL响应新增可选字段 `decimal_columns`、`execution_sql`、`precision_scope`，输入协议不变。附件Agent内部可对简单、有显式别名的输出加减乘/ROUND使用Decimal；没有二次查询或事后心算。WHERE筛选、排序、分组、聚合、CTE、窗口及其他复杂表达式不标记精确Decimal；除法保持SQLite原语义，避免整数除法变更。公开只读SQL接口默认仍为SQLite原生执行。Decimal不能恢复导入前已丢失的原始精度，不能宣称所有SQL均精确十进制。

RC35允许与改写输出独立的原始列排序（例如`ORDER BY rowid`）保留精确投影；输出别名、序号和计算表达式排序仍保持原生语义。Agent的`source.query.result`可附带`query_advisories`（code/columns/message数组），说明授权schema里的数值列转文本过滤风险；它不是查询失败，不改变SQL、原数据或结果，不是发布硬门禁。消费者应保留但不必弹窗。

RC37起，大目录可只给表目录并标记`schema_available=false`，不把被省略的业务列名当成已提供的schema。若拟执行的查询/缺失值统计依赖尚未提供的字段定义，先发出`status=schema_inspection`并执行定向`inspect_workbook`；原查询尚未执行，不应计为成功查询或重复查询。实际可见的相同字段定义可以在同一工作簿复用；不跨工作簿猜测含义。

RC38内部计算新增`aggregate_results {"operation":"shares","result_ids":["result-1"],"value_column":"cnt"}`：完整且同列结构的输入保留原行，增加`share_total`、`share_pct`，每行分母为所有选中行的该列总和。重复行不去重；NULL不补0，零分母的比例为NULL。它不替调用者决定源范围，不可混合汇总行与明细；与一个明确已存总计比较时仍使用`ratio`。对外问答请求参数不变。

除非用户要求舍入，提示模型保留工具返回精度。已知工具物理单位可规范化紧邻引用的单位并留痕，不改数值；未知单位不猜测。不再以第二个模型审阅作为线上拒答门禁；独立业务验收不能由被测模型自评代替。

## 地址与协议（续）

Docker 默认 API 为 `http://127.0.0.1:8091/api/v1`。完整自动生成的字段约束见 `/openapi.json`，交互页面为 `/docs`。JSON 使用 UTF-8；上传为 multipart/form-data；问答为 text/event-stream；报告为 HTML/PDF。客户端应允许返回对象增加字段。部署见 [docker-deployment.md](docker-deployment.md)。

## 附件接入流程

1. POST `/agent/conversations`，JSON `{"title":"新对话"}`，保存响应 conv_id。响应另含 title、scope；首问会替换默认标题为问题前 80 字符。
2. POST `/agent/conversations/{conv_id}/attachments`，文件字段名 file。接口同步等待解析和建库，并非返回 job_id 的异步 API。浏览器 FormData 不手动设置 Content-Type boundary。支持 XLSX、XLSM、CSV；旧 XLS/WPS ET 不属于保证支持格式。
3. ready 可直接提问；needs_review 展示 severe_warnings，确认时提交当前 revision 和 accept_severe_warnings=true；blocked 展示 errors，不能通过接受告警绕过。
4. POST `/agent/ask`，传入该会话及 ready 附件的 workbook_id。刷新后从 GET 会话详情的 scope.attachments 恢复附件。

上传与确认响应示例（ID 为占位符）：

```json
{"import_id":"import-id","workbook_id":"workbook-id","filename":"项目.xlsx","version":1,"status":"ready","revision":1,"errors":[],"severe_warnings":[],"warnings":[]}
```

version 是兼容数据字段，可不在前端展示。确认 JSON 示例：`{"revision":1,"accept_severe_warnings":true}`。409 表示版本已变化，重新加载后处理。

## 问答参数

| 字段 | 类型与默认 | 约束 |
|---|---|---|
| question | string，必填 | 至少 1 字符 |
| conv_id | string/null，默认 null | 推荐预先创建；省略时服务新建会话 |
| model | string/null，默认 null | GET /models 的 name，不是 label；null 沿用会话模型或默认模型 |
| scope.mode | auto/selected/space | 附件接入固定 selected |
| scope.workbook_ids | string[]，默认 [] | 最多 5 个，必须为本会话 ready 附件；空数组为通用问答 |
| max_rows | integer，默认 500 | 请求允许 1–1000；实际还受 server.query_row_limit 限制（默认 500）。不代表上传规模或总记录数，truncated=true 时需分页或改用聚合查询 |

GET `/models` 返回数组，含 name、label、default、context_window_tokens、output_reserve_tokens 等；列表不检查供应商在线状态。GET `/capabilities` 返回 pdf_available、session_attachments；PDF 依赖可加载不代表任意报告都能成功渲染。

## SSE 消费细则

使用 POST fetch 流或后端流式客户端，按 UTF-8 增量解码；网络 chunk 不等于事件。缓存半行/半个事件，按空行分隔，解析 data 中 JSON，通过 JSON.type 分发；忽略冒号开头的注释与填充。服务不依赖 SSE event 字段，也不提供事件 ID 重放。

```text
data: {"type":"session","conv_id":"会话ID","done":false}

data: {"type":"answer.delta","text":"分析结果"}

data: {"type":"done","done":true,"full_answer":"分析结果","partial":false}

```

| type | 主要字段 | 行为 |
|---|---|---|
| session | conv_id、model_name、model_label、scope_mode | 保存会话 ID，启动执行展示；不代表模型已输出 |
| status | status/message/round（可选） | 展示状态，容忍字段缺省 |
| scope.resolved | sources | 保存本轮来源 |
| step.start | id、action、step | 按 id 新增步骤 |
| step.done | id、action、status、result | 更新步骤；failed 可能被后续 Agent 恢复 |
| source.query.result / source.query.error | 查询观察对象 | 展示证据/局部失败，不作为终止事件 |
| context.usage / context.compacted | context 等 | 更新使用量/压缩，estimated 时显示估算 |
| answer.delta | text | 追加最终回答增量 |
| numeric.validated | references、可选 target、validation_scope | 仅显式单元格引用解析成功，不代表答案数字、单位或业务口径整体正确 |
| numeric.observed | target、mode、risks、evidence_count | 非阻断旁路观察，不作为计算事实或回答正文 |
| html | 报告对象 | 保存报告；最终可优先使用 done.report_url |
| done | full_answer、partial、sources、failed_sources、report_url、timing、attachments | 用完整答案校准增量并结束；通用问答可能不含 report_url |
| error | error、done | 显示本轮失败并终止 |

timing 含 elapsed_ms 和 UTC ISO 时间 completed_at。不同工具的 result 结构不同，客户端容忍未知事件；context.checkpoint 为内部事件。partial=true 时明确显示部分来源失败。HTTP 200 只代表流建立，仍可能收到 error；无终止事件就断流应显示中断。关闭连接不保证立即取消模型调用或计费，不自动重试问答 POST。

RC14 起不再扫描全文数字或强制每个数字附带引用。SQL/计算工具仍负责计算；`aggregate.result` 的精确十进制字符串不要转换成浮点重算。显式引用通常可选，未知、越界、格式错误或文本编号舍入仍拒绝，`status=reference_review` 表示修正引用，`error_code=result_reference_invalid` 表示引用失败。`numeric.observed` 只旁路记录启发式风险，不保证数学、单位或业务口径正确。带引用草稿解析后直接分块输出；其他最终润色仍先缓冲，避免泄露无效引用。

RC16增加窄范围计算路由：加权问题及明确精度要求的差额问题，必须执行并引用对应Decimal工具结果；`status=calculation_review`表示要求补算，多次失败返回`precise_calculation_required`。`aggregate_results`支持具名`baseline`/`current`引用及`weighted_average`操作，详见[RC16交付记录](release-1.2.0-rc.16.md)。这不是恢复全文数字门禁，普通原文读取不强制计算。报告正文中的`{{report_url}}`由服务解析为实际URL。

RC36当前默认的证据发布链路在生成报告/输出答案前发出一次`status=publication_observation`，对已有证据的问数最多一次，用同一模型检查实际SQL与候选答案。它通常增加一次模型调用，可继续原工具纠错；不是已通过校验的证明，也不是另一个模型的否决门禁。完成后仍只发布同一份最终文本/HTML/PDF，没有发布后润色。无证据的澄清不强制复核。调用方显示该状态消息即可，不改变请求参数。上面的RC14/RC16说明为历史路径；当前默认链路与边界以本段及证据发布协议为准。

RC39的`observe_execution`及内部执行状态在`calculations`中补充`input_result_ids`、`derivation`、`derivation_complete`、`sample_rows`、`sample_only`、`row_count`、`column_units`。样本不是完整结果，`derivation_complete=false`时不可据此断言依赖完整，仍可用缓存ID取回。联合期间比较优先用`compare_totals`从同一输入原子返回基期、本期、差额和增长率；不把旧局部差额拼接为新全局差额。元数据可见和同一输入计算是可验证范围，不等于自动保证所有业务语义正确。

## 历史与报告

会话详情包含 conv_id、title、scope、sources、context、messages 和模型信息。messages 按顺序恢复，区分 user、ai、tool 等角色，工具记录放入过程展示；回答元数据中的附件/耗时随该消息恢复，不使用当前选择的附件覆盖历史。

直接使用返回的 report_url/url，将 report_id 作为不透明标识，不拼接标题或猜测文件名。PDF 为同一报告 API 路径追加 `/pdf`。网关有额外前缀时映射返回的相对链接。HTML 策略限制脚本和跨源嵌入，建议同源代理加受限 iframe；测试 Nginx 将页面与 `/api/` 放在同一来源，满足 frame-ancestors self。

## 错误与后端透传

非流式错误通常为 `{"detail":"说明"}`；参数校验的 detail 可能是数组。当前没有统一稳定业务 code，SSE error.error 也是文本，不依赖中文文字匹配分支。

| HTTP | 处理 |
|---|---|
| 400 | 输入或解析规则不满足，读取 detail |
| 403/404 | 无权访问或资源不存在 |
| 409 | 草稿 revision 冲突，重新加载 |
| 413 | 上传体积超出服务/网关限制 |
| 422 | 字段、附件范围、数量不合法 |
| 429 | 并发或资源忙，遵循 Retry-After，先检查是否已有任务运行 |
| 500/503 | 服务异常或能力禁用，检查详情与日志 |

目前无 Idempotency-Key 去重；创建会话、上传或问答超时后先查询会话状态，再决定是否重新提交。Spring Boot 逐次校验资源归属，透传 HTTP 状态、Retry-After、文件响应头和 SSE，关闭整包聚合/缓冲；读取超时建议 600 秒，上传上限与服务 200 MiB 对齐。用户管理由 Spring Boot 实现，核心服务共享模式下不能直接向用户开放全部资源 API。

## RC21 补充：分析结果事件

问答请求参数不变。多工作簿问答增加SSE事件`analysis.planned`、`analysis.resolved`；`done.analysis`和历史回答`meta.analysis`包含本题结构化分析结果。事件是可加性扩展，不改变原有`answer.delta`、`html`、`done`和报告/PDF路径。

`analysis.validation_scope=declared_task_coverage_provenance_and_computation`仅代表已声明任务的缓存来源与计算检查，不证明自然语言理解或SQL业务筛选正确。协议与限制见[分析结果契约](analysis-result-contract.md)。
