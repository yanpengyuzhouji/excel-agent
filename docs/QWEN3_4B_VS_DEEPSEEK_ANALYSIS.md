# Qwen3-4B vLLM 与 DeepSeek Excel 报告对比

当前对比模型还包括 ModelScope 的 `Qwen3-VL-8B-Instruct` 和 `Qwen3-14B`。
前端通过 `/api/v1/models` 动态加载模型选项。需要区分：DeepSeek、显式配置为
`large` 的模型共用 `sql_query → html_interpreter → terminate` Agent 链路；当前
Qwen3-4B、Qwen3-VL-8B-Instruct、Qwen3-14B 配置为 `small`，走确定性的
`profile → facts/findings → 固定 HTML` 路由，但下游同样经 `html_interpreter`、原始
HTML 文件接口和报告 iframe 展示。显式 `analysis_route: large` 可将 Qwen 切换到 Agent 链路。

**日期**：2026-08-12  
**样本**：工作簿 077b05356a8d40b195cfcd9043522514，sheet1 物料清单  
**问题**：生成分析报告

## 1. 测试状态

已在真实环境完成一轮对齐测试：

- Excel 服务：`http://192.168.0.7:8090`
- Qwen3-4B vLLM：`http://192.168.0.201:18000/v1`
- DeepSeek：`https://api.deepseek.com/v1`
- 工作簿：`077b05356a8d40b195cfcd9043522514`
- 公共变量：新建会话、同一问题、同一工作簿、`max_rows=200`、`temperature=0.1`、`max_tokens=4096`。

Qwen 仅额外使用 vLLM 必需的 `chat_template_kwargs.enable_thinking=false`；这属于接口适配项，系统提示词、ReAct 工具集合和报告链路保持一致。

## 2. 样本事实基线

直接从导入后的 SQLite 校验得到：

| 指标 | 数值 |
|---|---:|
| 数据行数 | 10 |
| 进线单元非空数 | 10 |
| 计量单元非空数 | 5 |
| 出现单元非空数 | 7 |
| 三列合计去重物料数 | 17 |

## 3. 历史端到端对比（Qwen2.5 基线）

历史日志中同一工作簿、同一报告问题的完整调用记录如下。样本不是严格随机实验，部分请求复用了同一个会话，因此只能作为工程基线，不能当作模型排行榜。

| 指标 | Qwen2.5-7B vLLM | DeepSeek |
|---|---:|---:|
| 完整报告请求数 | 4 | 6 |
| 平均耗时 | 8.9 秒 | 28.4 秒 |
| 中位耗时 | 8.8 秒 | 26.2 秒 |
| 平均模型 HTTP 调用数 | 2.8 | 3.5 |
| 典型工具链 | sql_query → report_spec → terminate | sql_query → html_interpreter → terminate，有时多一次 sql_query |
| 报告文件平均大小 | 759 字节 | 2708 字节 |

结论：Qwen 的主要优势是速度和收敛；DeepSeek 的主要优势是报告内容丰富度和最终说明完整度。

## 3.1 对齐链路实测（Qwen3-4B vs DeepSeek，单次）

测试问题一：`请查询当前Excel共有多少条记录，并直接回答数量，不生成报告。`

| 指标 | Qwen3-4B | DeepSeek V4 Flash |
|---|---:|---:|
| LLM 调用/工具链 | 2 次；sql_query → terminate | 2 次；sql_query → terminate |
| 总耗时 | 约 3.2 秒 | 约 3.3 秒 |
| 最终回答 | 10 条 | 10 条 |
| 报告 | 无 | 无 |

测试问题二：`请对当前Excel生成一份完整的物料分析报告，统计各单元数量、主要物料和数据完整性。`

| 指标 | Qwen3-4B | DeepSeek V4 Flash |
|---|---:|---:|
| LLM 调用/工具链 | 7 次；sql_query × 5 → html_interpreter → terminate | 6 次；sql_query × 4 → html_interpreter → terminate |
| 总耗时 | 约 198.0 秒 | 约 27.5 秒 |
| 报告大小 | 665 字节 | 3911 字节 |
| 报告质量 | 错误：生成报告时把样例/错误推断的 6 行当成总行数，未完成目标统计 | 正确覆盖 10 行、各单元 10/5/7 非空、空值 0/5/3 和主要物料 |

### 实测差距定位

- SQL 问答能力在该简单问题上基本一致：两者都能生成正确 SQL 并在一轮查询后结束。
- 报告能力的主要差距来自 Qwen3 的 ReAct 收敛和任务规划：它连续重复 `SELECT * FROM sheet1 LIMIT 5;` 五次，没有根据报告需求切换到聚合 SQL。
- Qwen3 最终虽然调用了同一个 `html_interpreter`，但输入 HTML 只有一个英文简表，且数字来自错误的样例范围；这不是后端报告模板差异。
- DeepSeek 在相同提示词下主动生成了完整的分单元统计、物料频次和数据完整性报告。

## 4. 差距到底来自哪里

### 4.1 原有差距首先来自两套报告协议

旧实现通过模型名中是否包含 `qwen` 选择结构化本地模型模式：Qwen 走
`report_spec`，DeepSeek 走 `html_interpreter`。这会把后端报告模板、可用输出
空间和终止条件一起改变，因此旧数据不能直接作为模型能力对比。

当前大模型 Agent 路由已统一为模型无关的 DeepSeek 对齐链路：进入 `large` 的
Qwen3 和 DeepSeek 使用同一份系统提示词、同一套 ReAct 工具、同一
`sql_query → html_interpreter → terminate` 报告路径。`small` 配置仍是有意保留的
规则模板路由，不应在性能对比中宣称它执行了模型 Agent。

### 4.2 Qwen 报告偏短，且分析深度受最后一次 SQL 限制

历史 Qwen 报告中出现过 198 字节的报告，主要只有标题、概要和计数；这部分
数据受旧 `report_spec` 协议影响，不能直接归因于 Qwen 模型能力。

后端不会根据多个 SQL 结果合并分析，也不会自动生成交叉表、完整性分析或共用物料分析。模型若只做一次简单 SQL，最终报告自然只能覆盖这一份结果。

### 4.3 DeepSeek 更丰富，但更慢且有收敛/格式风险

历史 DeepSeek 报告通常包含数据概况、各单元统计、共用物料、空值/完整性提示和明细表，文件明显更大；代价是：

- 平均耗时约为 Qwen2.5 的 3.2 倍；
- 更容易多跑一次 SQL；
- 曾出现报告已生成但最终回答为“模型未提供可公开的最终答案”或停在“将提供最终摘要”的情况；
- 自由生成 HTML 对提示词、JSON 转义和终止格式更敏感。

### 4.4 配置已统一到 Qwen3-4B

本次已将项目运行时配置统一为：

    name: Qwen3-4B
    model: /data/models/Qwen/Qwen3-4B

如果 vLLM 的 served-model-name 不是 Qwen3-4B 或完整路径，Excel 服务仍可能收到模型不存在错误。应以 vLLM /v1/models 返回的精确模型 id 为最终值。

## 5. Qwen3-4B 需要重点验证的项目

若将 Qwen3 配置为 `large`，它会与 DeepSeek 共用同一条报告链路；当前默认的
Qwen3 小路由不调用 LLM，后续应分别记录模板链和 Agent 链的指标：

1. 是否能稳定输出 Action: / Action Input:，而不是只输出 <think> 或自然语言。
2. 是否能在报告任务中从样例查询切换到带聚合、带单元标签的 SQL，并随后调用 html_interpreter。
3. 是否出现 Qwen3 的 <think> 内容泄漏到最终回答。
4. 是否能正确处理含中文、换行和 JSON 特殊字符的 summary / highlights。
5. 报告是否仍只有固定表格，还是 Qwen3 能主动执行多次 SQL 后给出更完整的统计。
6. vLLM 的实际首 token 延迟、总耗时和显存/并发表现。

## 6. 建议的改进顺序

### P0：先做可比性修正

- 在同一台可联网/可访问局域网的机器上，分别新建两个会话，使用同一工作簿、同一问题、同一 temperature、同一 max_tokens。
- 先请求 /v1/models，把 vLLM 返回的精确 id 写入 config.yaml。
- 每个模型至少重复 5 次，记录首个 HTTP 请求、每轮 action、报告 URL、最终答案、总耗时和错误。

### P1：提高 Qwen 报告上限

在相同 `html_interpreter` 链路上重复测试后，再根据模型实际 SQL 探索深度和
HTML 内容质量判断是否需要针对 Qwen 做提示词或输出修复。

### P1：Qwen3 推理输出清理（已完成）

已对 <think>...</think> 和 <analysis>...</analysis> 做显式剥离，不让它进入 done.full_answer、会话 AI 消息或前端公开答案；同时只使用剥离后的可见内容解析 Action。Qwen3 专项 SQL/报告测试已通过。

### P2：优化 DeepSeek 的终止可靠性

保留当前的空 action 重试，但在 html_interpreter 成功后增加服务端兜底：如果下一轮没有有效 terminate，直接用已生成的报告 URL 结束本轮，并生成固定摘要，避免“报告已经生成但最终答案为空”。

## 7. 当前结论

- **速度**：历史 Qwen2.5 明显领先，约 8.9 秒对 28.4 秒。
- **报告丰富度**：历史 DeepSeek 领先，但历史 Qwen 使用了不同的结构化报告协议；需要用新对齐链路重跑后再判断模型本身差距。
- **稳定性**：Qwen 的报告生成链更短、更容易成功；DeepSeek 的自由 HTML 链更强但终止格式更脆弱。
- **Qwen3-4B 的关键风险**：仍需在实际 vLLM 端确认 served-model id 和 thinking 参数是否匹配。
- **最可能的真正差距**：当前主要是报告协议和后端渲染策略造成的，不是简单替换 Qwen2.5 为 Qwen3 就会自动消失。
