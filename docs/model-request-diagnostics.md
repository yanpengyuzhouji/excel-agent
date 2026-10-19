# 模型请求异常与日志

每次模型调用生成独立 `request_id`（前端显示为诊断编号），日志通过
`conv_id`、`phase` 和 `agent_round` 关联会话、控制轮次与最终回答。
错误 SSE 保留原有 `type/error/done` 字段，并增加：

```json
{"type":"error","error":"模型响应已结束，但未返回可显示正文，请重试或切换模型。（诊断编号：示例）","done":true,"error_code":"empty_response","request_id":"示例","retryable":true,"partial_output":false}
```

| error_code | 含义 |
|---|---|
| first_content_timeout | 超过首正文时限，心跳或推理字段不算正文 |
| request_timeout | SDK连接/读取超时，或整次调用/Agent剩余时限耗尽 |
| empty_response | 正常结束但无可见正文，不据此断言供应商路由故障 |
| reasoning_only | 仅收到推理字段，未收到正文 |
| unexpected_tool_calls | 返回未约定的原生工具调用格式 |
| output_limit | 输出额度耗尽但无正文 |
| content_filtered | 上游安全过滤或拒答字段，未返回正文 |
| authentication_failed | HTTP 401/403，管理员检查凭证与权限 |
| quota_exhausted | HTTP 402，管理员检查余额 |
| rate_limited | HTTP 429，可能频率或额度限制，不盲目判为余额不足 |
| upstream_unavailable | HTTP 5xx |
| connection_failed | 无法连接上游 |
| request_rejected | 其他HTTP 4xx；检查模型、参数、上下文 |
| upstream_stream_error | SSE流内返回错误对象 |
| stream_protocol_error | 其他响应处理或协议异常 |

`retryable` 代表可尝试重试。1.2.0候选版对无正文的空响应、仅推理和首正文超时
最多自动恢复一次（可用empty_response_retries=0关闭），不自动切换模型；已输出部分文本时
`partial_output=true`，不能把重试结果直接拼接到已有文本。10秒首正文阈值、
总调用超时、SDK重试配置不变；两次尝试共享原调用总时限，最多等待两次10秒首正文。
鉴权、限流和已有部分正文不自动恢复。正常有正文但 `finish_reason=length` 时仍沿用
原有截断标记，不把合法的部分回答误判为空响应。

RC4起，纯空白正文不视作已输出正文，也不解除首正文倒计时；前导空白仅有界暂存，
收到实际正文后再发出。若最终只有空白和推理，仍属于可进行一次恢复的无正文响应。

日志行以 `llm_request` 开头，记录诊断编号、模型、会话、阶段、轮次、耗时、
首正文耗时、分片数、正文/可见正文/推理/拒答字符数、工具调用分片数、结束原因、
可获得的HTTP状态/供应商请求编号，以及流中实际返回的token用量。
未提供的token用量不会臆测；供应商请求编号是可选字段。

不记录请求问题、模型正文、推理正文、工具参数、供应商错误原文、API密钥、
完整请求URL或任意响应头。排障按前端诊断编号定位：

```bash
docker compose logs excel-agent | grep '<诊断编号>'
```

诊断能力不等于保证供应商永不空输出；上下文、协议与计算口径修复及其验收结果见各候选版发布说明。
