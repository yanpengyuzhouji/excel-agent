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

附件响应包含 import_id、workbook_id、filename、version、revision、status、errors、severe_warnings、warnings。status 为 ready / needs_review / blocked。ready 才能问数；硬错误不允许强制建库。当前每会话最多 5 个附件，默认一次执行最多查询 3 个工作簿。

删除响应附带 deleted_workbooks、retained_shared_workbooks、cleanup_warnings。清理被运行中的分析/上传阻止时返回 429，不修改记录。文件暂存失败时事务回滚；提交后暂存文件删除失败会明确返回 cleanup_warnings，并在下次启动时重试。所有文件路径校验限定在服务存储目录内，不删除用户电脑上原始文件所在目录。

问答请求：

```json
{"conv_id":"会话ID","question":"统计金额","model":null,"scope":{"mode":"selected","workbook_ids":["附件工作簿ID"]},"max_rows":500}
```

新附件会话必须使用 selected 范围，且工作簿必须是本会话 ready 附件。历史会话保持旧接口兼容。scope.attachments 会跨问答轮次保留。只读 SQL、资源预算和版本快照校验沿用原执行引擎。

`scope: {"mode":"selected","workbook_ids":[]}` 表示通用问答，不加载目录、不运行问数 Agent，也不生成文件报告。该模式支持历史对话与自动上下文压缩，流式事件新增 `answer.delta`（text 为增量文本），终止事件 `done.analysis_route` 为 general。非空附件范围仍走 agent；已有附件但主动取消所有勾选也可选择通用问答。

SSE 使用 `data: {JSON}\n\n`，事件 type 包括 session、scope.resolved、step.start、step.done、context.usage、context.compacted、html、done、error。step.done 的完成仅代表工具步骤完成；done/error 是本轮终止事件。断流无终止事件应显示失败，不得视为完成。客户端使用 POST fetch 流式读取；Spring Boot 不可缓存整个响应后再返回。

当前未提供可靠断线续传、后台任务取消、附件自动 TTL 和跨副本会话协调。服务密钥模式的身份隔离依据 principal；生产网关应按真实用户映射凭证，不得伪装成已实现用户级 JWT 委托。
