# RC34：固定官方 DeepSeek 的收敛验收

2026-10-17。状态：回归进行中，尚未生产放行。

## 冻结边界

- 默认及本轮回归均使用官方 https://api.deepseek.com/v1 的 deepseek-flash，选择器沿用 deepseek-v4-flash；不使用百炼、不自动换模型。历史模型配置仅为兼容保留。
- config.yaml 和 compose.yaml 同步官方默认；Compose 从 .env 注入凭证，不将凭证打入镜像。原数据卷不变。
- 非思考首内容10秒、思考30秒、单次总90秒策略不变。
- 不新增业务关键词判断、全文数字门禁或强制预计划；先验证现有通用链路。
- 每次模型测试保留原题、原期望、首轮结果、镜像及源代码指纹。修复后的新批次不能覆盖失败批次；补充解释题不能代替原题通过。

## observation 的实际含义

当前 observe_execution 汇总证据ID、查询成功数量、空结果、截断、计算操作、失败与未查询来源，并要求模型检查请求覆盖情况。它不是独立业务审计器，不能自动证明指标行、SQL条件或记录粒度正确。缓存引用验证能够证明数值来自哪个单元格，不能单独证明模型给这个值起的指标名正确。

## 收敛口径

核心阻断：主要指标错误、跨工作簿来源错误、漏答必要计算、未提示的重要口径冲突、执行失败、数据越权、报告或下载不可用。

非阻断：未被问题要求的表格形式、措辞或排版差异。原自动筛查分数照常保留，另行说明误判原因，不在执行中更改题库或评分脚本。数字出现在回答任意位置不等于该指标正确，最终需要对照标签、单位、范围及来源审阅。

任何有限回归均不是零错误保证。“重跑直到碰巧通过”不构成放行证据。确有缺陷才修改，按同一冻结版本重新验收；资源或凭证不可用时如实报告。

## 当前验证

- Linux隔离测试369项，368通过、1字体条件跳过：data/business_acceptance/rc34-unit-linux.json。
- 部署54个文件哈希一致：data/business_acceptance/rc34-runtime-source.json。
- 36题首轮：data/business_acceptance/numeric-gate-v1/runs/deepseek-v4-flash-20261017-103349。
- 9题关键场景首轮：data/business_acceptance/gold-v2/runs/deepseek-v4-flash-20261017-103337。
- 官方模型目录读取成功，包含deepseek-flash、deepseek-v4-pro；合成JSON探测出现过不完整回复，不能以HTTP成功代替协议成功，保留rc34-official-probe.json结果。
