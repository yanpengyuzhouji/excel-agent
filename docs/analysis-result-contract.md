# RC22：逐题分析结果契约

> 历史设计记录，当前生产默认已关闭 `analysis_contract`，仅保留兼容路径和测试。
> RC23起改为查询优先、显式缓存引用、同源回答/报告；当前接口边界见
> [API协议](API.md#数据证据与精度边界rc28)。下文描述RC22，不代表当前默认行为。

## 职责

保留现有附件、解析、SQLite、SQL沙箱、计算工具和会话。生产ExcelLLM启用analysis_contract；无附件通用聊天不走此链路。旧的单工作簿接口及无该属性的兼容测试客户端保留原路径。

新路径：检查结构 → define_analysis声明本题任务 → 查询/已有计算 → 绑定缓存坐标 → 程序计算并冻结结果 → 同源回答/HTML。不是按题号或业务表名加规则，也不恢复全文数字扫描。

新增模块app/agent/analysis_result.py集中处理计划结构、来源检查、结果绑定、Decimal运算、格式化和HTML。结构检查与新模块共用物理列映射，包括混合字段的_value/_raw。

## 内部工具协议

define_analysis接收tasks。每项包含id、label、operation，以及source或sources；来源必须是授权工作簿/表/物理列。scope是模型声明的统计口径文本，不是已证明的SQL语义。

```json
{"action":"define_analysis","action_input":{"tasks":[
  {"id":"left","label":"甲金额","operation":"value","source":{"workbook_id":"w","table_name":"data","column":"amount"}},
  {"id":"right","label":"乙金额","operation":"value","source":{"workbook_id":"w","table_name":"data","column":"amount"}},
  {"id":"delta","label":"甲减乙差额","operation":"subtract","inputs":["left","right"],"decimal_places":4}
]}}
```

source也可以换成sources数组，用于多表汇总或跨附件缓存计算。每个声明来源都须有缓存来源证据。

查询后，terminate或html_interpreter传bindings，将每个叶任务绑定到result_id/row/column；表格绑定columns。程序计算的任务不接受模型填数。

```json
{"action":"terminate","action_input":{"bindings":{
  "left":{"result_id":"result-1","row":0,"column":"amount"},
  "right":{"result_id":"result-2","row":0,"column":"amount"}
}}}
```

支持value、table、sum、average、count、weighted_average、subtract、ratio、percentage、growth_rate、convert。

- subtract输入固定为左、右；growth_rate固定为基期、本期；显示公式和操作数标签。
- 每个叶指标都需绑定；sum可分别汇总行数和金额，不能用一次金额求和代表整题完成。
- 完整缓存行才能求和、平均、计数；截断、未知引用、错误工作簿/表、来源链缺失均拒绝。
- count计算缓存行数。SQL COUNT的标量用value引用；SQL统计范围仍由模型负责。
- 计算使用未舍入的输入，最终按decimal_places进行ROUND_HALF_UP；默认派生Decimal长小数显示至12位。SQLite原有浮点结果不会因此变成财务级原始十进制。
- 单位从真实表头/元数据提取，不把整个unit_hint当单位；相减/求和拒绝不兼容单位。元、万元等同量纲换算由convert完成，不接收模型换算因子；未支持的单位需澄清，不猜测。
- 未查到记录、NULL、零分母和输入缺失分别呈现，不补零。原始编号/规格保留文本，不能请求数字舍入。
- 原始备注可绑定inspection-N表格；分页备注仅在连续收齐后作为完整证据。普通样本不冒充全表。
- 结果冻结前允许完整修订计划，先验证再替换，保留已有查询缓存；模型需保留用户全部要求，程序不宣称能语义证明其完整性。结果冻结后不允许替换绑定或继续查询改变报告口径。
- SQL聚合、分组、窗口及计算工具结果不能再次作为原始行集聚合；使用value/table读取已计算结果。SQL变换/未知血缘单位需用result_unit显式声明，不继承单个物理列单位。解释summary支持{{task:ID}}，标为模型解释；引用值与同一结果包一致。
- 单题最多40项任务、结果包200000字符；超限应改用聚合或缩小展示字段。修正提示最多3次，仍受原预算约束，失败返回analysis_incomplete，不输出半截答案。

## 非数值场景

clarification模式输出明确的澄清问题；explanation模式要求已有检查/查询证据ID，标明“模型解释”，不声称程序已经验证其叙述。它适用于开放解释和缺字段说明，不应替代用户明确要求的指标。

这两个模式由模型选择；模型选错模式、漏列任务、错误理解“有效记录”、生成错误SQL筛选仍可能发生。本模块不以关键词扫描假装能通用证明自然语言含义。

## 统一发布

生产路径不再让自由草稿、第二次润色或分母正则替换决定核心事实。文字、HTML及PDF由同一冻结结果生成。html_interpreter在此路径使用bindings及title；模型传入的html不作为事实载体。报告仍经过原有消毒、CSP和存储路径。

PDF链接由保存成功的报告URL拼接，模型不再撰写“系统不支持PDF”等能力说明。PDF实际可用性仍取决于已有原生依赖；失败由真实接口返回，而不是从文字承诺推断。

外部POST /api/v1/agent/ask请求不变。SSE增加analysis.planned、analysis.resolved、analysis.rejected；拒绝事件记录原因、绑定、尝试序号并持久化。done增加analysis。结构化结果持久化到工具记录及回答meta.analysis；旧客户端可忽略新增字段。前端过程展示define_analysis名称及任务口径。

通常增加一次define_analysis控制轮次，不增加最终自由润色调用。真实耗时、token和模型遵循程度尚未回归测量，不能宣称费用或延迟一定下降。

## 评分

业务回归除原文本检查外，新增packet_checks，按指标标签和数值（可选期待operation）核对结构化结果。多个候选或标签无法对应时需要复核，不借用全文其他位置的正确数字判通过。没有修改历史首轮记录、业务期待值和容差。评分器依然不是SQL业务语义证明。

## 验证与边界

见release-1.2.0-rc.21.md。离线重放使用明确写定的测试计划，只证明缓存证据到结果发布的正确性，不证明真实模型会生成这些计划。下一步应先做小规模同模型同题对照，覆盖计划遗漏、方向、备注、澄清及报告，再决定完整100题回归；不能直接生产放行。
