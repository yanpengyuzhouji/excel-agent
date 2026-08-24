# 多工作簿 Agent 评测与灰度清单

## 固定安全不变量

每个工作簿仍是独立、版本化的 SQLite。`/api/v1/agent/ask` 只接受服务端解析的
`workbook_id`，每条 SQL 都经过 SQLGlot、只读连接和 SQLite Authorizer；无权限工作簿
不得出现在目录、候选或来源事件中。

## 回归命令

```bash
.venv-linux/bin/python -m unittest discover -s tests -p 'test_*.py'
```

重点覆盖：

| 场景 | 通过标准 |
| --- | --- |
| 单工作簿问数 | 仍只访问路径中的工作簿，`analysis_route=agent` |
| 自动选库 | 候选目录先按 owner 过滤，最多 5 个候选/3 个实际查询 |
| 指定工作簿 | 混入其他 owner 的 ID 时不执行查询 |
| 数据空间 | 成员只能来自当前 owner 的已存在工作簿 |
| 版本切换 | 同一轮所有查询使用同一 `import_id/version` 快照 |
| 部分失败 | 返回 `partial=true` 与 `failed_sources`，不伪装成完整统计 |
| 关系模型 | 关系只展示 owner 已确认的表列，不自动推断 JOIN |
| 会话恢复 | 刷新后恢复问题、工具结果、模型和来源快照 |

## 线上观察指标

按用户/会话记录以下指标：目录召回延迟、实际查询工作簿数、SQL 成功率、部分失败率、
来源覆盖率、模型调用次数、总耗时和估算 token 成本。灰度时优先开启
`excel.agent.multi_workbook_enabled`，需要回滚时将其设为 `false`；这不会影响单工作簿
入口和既有 SQLite 隔离。

## 召回策略

默认使用 owner-filtered FTS5 元数据索引；不支持 FTS5 的 SQLite 自动退回受限关键词评分。
索引只包含工作簿名称、表名和字段名，不包含业务行。embedding 如后续启用，只能作为候选
召回信号，最终事实仍必须来自只读 SQL 结果。
