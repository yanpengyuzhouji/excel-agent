# Excel 解析、问数方案与企业发布评估

评估日期：2026-10-15（Asia/Hong_Kong）。范围：当前代码审查、合成工作表的解析/建库复现、现有相关回归测试、官方文档/仓库/论文检索。不修改业务代码、配置或既有数据；未上传企业工作簿到外部服务，也未进行生产压测或第三方产品实测。

## 结论

当前系统适合作为受控内网试点，尚不建议按“通用企业 Excel 问数产品、支持任意复杂报表且数据可靠”发布。主要阻碍是语义解析和类型转换会产生无告警的数据改变，而不是 openpyxl 本身不适合企业使用。Docker 封装不是准确性或安全验收。

建议保留现有确定性解析、SQLite 只读查询、证据追溯的主线；先修数据正确性，再按真实业务样本评估是否增加 calamine/fastexcel 快速通道。读取引擎替换不能修复项目自己的 `_build_draft`、类型推断和建库规则。

## 当前架构与已有能力

路径：上传限额与哈希 → OOXML 包预检查 → openpyxl 两次只读读取（公式/缓存值）及 XML 布局读取 → 草稿 → 自动校验/严重告警确认 → 独立 SQLite → 受限 SQL/Agent → 查询证据 → 流式回答/显式请求才生成报告。

- 支持 `.xlsx`、`.xlsm`、`.csv`，不支持旧 `.xls`、`.xlsb`、`.ods` 或原生 WPS 文件格式。WPS 保存的标准 OOXML 是否兼容取决于具体文件，不能因来自 WPS 就承诺全部支持。
- 保存源文件哈希、源工作表/源行、公式及合并填充的 lineage。
- 检查上传大小、ZIP 解压大小/压缩比/数量、XML 部件大小、行列/单元格/字符上限和合并展开规模。
- 导入、SQL、Agent 有并发限制；SQL 使用只读连接、语句检查、SQLite authorizer、执行时限和结果截断标记。
- 公式缺少缓存值会要求确认，而不是无提示自动建库。
- 问数有来源范围、有效查询证据、有限的业务计数核验与重复工具调用保护。

相关代码：`app/service/parser.py`、`materializer.py`、`service.py`、`attachment_policy.py`、`upload_stream.py`、`app/agent/multi_workbook.py`。

## 已复现的正确性缺陷

实验通过真实 `_build_draft` → `_validate_draft` → `build_database` 函数进行，工作表在内存中构造，SQLite 仅写入 TemporaryDirectory。不是对历史用户文件下结论，也不是完整文件格式兼容率评测。

| 场景 | 原始预期 | 实际结果 | 风险 |
| --- | --- | --- | --- |
| 项目编号为文本长整数 | `9007199254740993` | `9007199254740992` | 标识符被改变，校验无错误/告警 |
| 项目编号有前导零 | 文本 `00000` | 整数 `0` | 原始标识丢失，校验无错误/告警 |
| 表中出现一次重复表头 | 12 条业务数据 | 导入 13 行，两列变为 TEXT | 表头当业务记录，校验无错误/告警 |
| 普通 Excel 表只有两行数据 | 两条有效明细 | 被分类为 metadata，`NO_DATA_SHEET` | 有明确报错，但通用小表能力不足 |

根因：

1. `_infer_column` 用 `float(value)` 判断整数；建库 INTEGER 又执行 `int(float(raw))`。中文字段只对含“编码”和以 id 结尾的列强制文本，“项目编号”不在保护范围内。即使 Excel 本身不能准确表示某些长数值，文本单元格中的长编号本来是可以完整保存的，本次复现属于项目的二次损失。
2. `_build_draft` 默认只在前 25 行选一个表头，且一个 sheet 对应一个数据表。它不分离中间重复表头；不存在通用的多区域、多级表头或交叉表重构。
3. Excel 默认至少 5 条数据记录才倾向于判为数据表，较小的合法表容易被误归类。

最小复现要点（不访问真实文件）：

```python
from app.service.parser import _build_draft
from app.service.service import WorkbookImportService

rows = [['项目编号', '金额']] + [[str(9007199254740993+i), i+1] for i in range(6)]
draft = _build_draft('audit.xlsx', [('Sheet1', rows, {}, [], [], {})])
sheet = draft['sheets'][0]
assert sheet['columns'][0]['sql_type'] == 'INTEGER'  # 当前危险推断
assert int(float(sheet['rows'][0]['values'][0])) != 9007199254740993
assert not WorkbookImportService._validate_draft(draft)['warnings']
```

## 代码审查发现的能力边界（尚未逐项文件实测）

- 数字的 `number_format`、百分比显示、单位、编号显示格式未作为通用单元格语义保存，原始显示值和底层值不能被等同。
- REAL 使用二进制浮点；金融精度要求需要明确金额单位、舍入规则，并考虑 Decimal 或按最小货币单位存储。
- 记录 hidden rows/columns 不等于明确“隐藏/筛选掉的数据是否计入”业务口径；当前隐藏行会进入数据表，只在 lineage 标记。隐藏 sheet 的选择政策也没有在草稿里完整表达。
- 合并区域填充值是布局重建，不一定是业务意义上的重复值；对汇总金额不能无差别向下填充后求和。
- 标准 openpyxl 路径不计算公式；已有缓存也可能陈旧。检测“缓存有值”不能证明正确或最新。
- 异常包回退到自研 OOXML 读取器，日期样式等能力不与 openpyxl 完全对齐；回退应记录引擎/原因，必要时降低可信度或拒绝，而非无声降级。
- 输入读取是 `read_only`，但后续仍保存全量 rows、草稿、副本、insert_rows；这不是端到端固定内存流水线。仅有数据量限额，未证明生产峰值内存和解析时限。
- 未看到独立解析 worker 的进程级硬终止/内存隔离；两路并发并不构成故障隔离。
- 安装环境 openpyxl 3.1.5，`defusedxml` 不在 requirements，当前 venv 也未安装。openpyxl 官方明确建议该 XML 防护；应结合自研 ElementTree 路径审查，而不是只保护一个引擎。此项是防护审查缺口，不代表已验证成功的攻击。
- `requirements.txt` 仅下限约束，缺少可复现的发布锁定；Docker 构建、恶意文件、恢复演练与负载性能未在本次验证。

## GitHub 候选与使用建议

以下是面向本项目的适配建议，不是“全球准确率排行榜”。

| 方案 | 所属层 | 对本项目的价值 | 不应误认为 |
| --- | --- | --- | --- |
| openpyxl | OOXML 读取与布局访问 | 保留为复杂工作簿主路径，当前没有证据证明底层换库能解决上述问题 | 自动识别全部业务表，或计算所有公式 |
| calamine / python-calamine | Rust、多格式读取 | 对规则大表做性能与兼容性 A/B，扩展格式候选 | 业务语义识别器 |
| fastexcel / Polars | calamine、Arrow 数据路径 | 大型规则明细表快速读取和列式处理；注意精确类型覆盖 | 编码、金额、复杂表头天然正确 |
| DuckDB Excel | XLSX → SQL/分析引擎 | 有跨表大规模分析需求后考虑；初期 SQLite 无须强制替换 | 无参数读取能识别所有非规则 Excel |
| SheetBrain | 结构理解—执行—验证 Agent | 借鉴明确的理解层和验证反馈层；复杂版式按需辅助理解 | 可直接替换成生产后端、继承论文成绩 |
| SpreadsheetLLM / SheetCompressor | 结构编码/上下文压缩研究 | 借鉴锚点、结构保留与按需检索 | 一个已验证的 drop-in 商业解析 SDK |

来源：

- [openpyxl 官方文档](https://openpyxl.readthedocs.io/en/stable/) 与 [只读模式说明](https://openpyxl.readthedocs.io/en/stable/optimized.html)。
- [calamine 官方仓库及其基准方法](https://github.com/tafia/calamine)。官方速度对比是特定版本、硬件和大型规则 XLSX 工作负载，不是语义准确率。
- [fastexcel 官方仓库](https://github.com/ToucanToco/fastexcel)：Rust、calamine、Arrow 通路和手动类型覆盖。
- [DuckDB 官方 Excel 导入说明](https://duckdb.org/docs/lts/guides/file_formats/excel_import) 与 [官方扩展仓库](https://github.com/duckdb/duckdb-excel)。默认 sheet/range/type inference 有边界，`ignore_errors=true` 会将不能转换的值替换为 NULL，企业准确性路径不应默默启用。
- [微软 SheetBrain 官方仓库](https://github.com/microsoft/SheetBrain)。其结构理解、受限执行与验证可参考，但未复跑其 benchmark。
- [SpreadsheetLLM 论文](https://arxiv.org/abs/2407.09025)。未核实 `microsoft/SpreadsheetLLM` 为可用官方仓库，不将第三方复现包装成微软官方实现。

## “解析榜单”应该怎样看

1. **读取速度**：calamine 官方 benchmark 可参考，比较的是加载/遍历给定数据集，未覆盖本项目的完整转换流程，不外推固定倍数。
2. **格式/功能保真**：[ExcelBench](https://github.com/SynthGL/ExcelBench) 有读写保真、模板修改、公式计算和性能的分离测试。仓库与 WolfXL 生态明显相关，按社区实现/特定快照看待，不能作为中立全行业认证。
3. **表格问答**：[HiTab](https://github.com/microsoft/HiTab) 更贴近层级表头与聚合问答，适合选取样本建立评测，不等同所有 Excel 文件格式解析。
4. **Excel Agent 编辑/工作流**：[SpreadsheetBench 官方页面](https://spreadsheetbench.github.io/) 明确区分 V1 的真实表格操作与 V2 的端到端业务工作流。这里的模型/Agent 排名不能拿来证明读取库的可靠性，也不能比较不同任务、子集、模型和 scaffold 的成绩。

本次未找到能够统一衡量“任意 Excel 文件 → 正确业务数据 → 正确企业问答”的公认单一排行榜。也未亲自复跑上述第三方成绩。

## 企业发布前的优先级

### P0：正式发布阻断项

1. 保留 raw value / display value / cell type / formula / cached value / coordinate / style；业务类型推断不能覆盖原始事实。
2. 标识符保守按文本；禁止 `int(float(raw))`；建立金额、日期、百分比的显式语义规则。
3. 区分表头/重复表头/业务记录/小计/总计/备注，支持小表；多区域和多级表头未可靠识别则告警或阻断。
4. 自动确认不能仅看解析有没有抛异常：必须做源数据与库的数据覆盖/行列/关键金额/编号一致性核验。低可信内容只在后台人工可审查，前端仅严重告警弹窗，符合现有产品交互要求。
5. 明确支持矩阵与拒绝策略，回退路径标明差异；公式缺失/过时、不支持结构不得用 NULL 或猜测悄悄替代。

### P1：可靠性、安全与交付

- 使用真实脱敏样本进行 Gold 数据验收：电力项目表、采购目录、进度月报、多级合并表、跨 sheet 汇总、公式、隐藏行、错误值、CSV 编码等。
- 给解析进程硬时间/内存限制，验证最大允许文件与两路并发的峰值；保留恢复、备份、日志和审计策略。
- 补全 XML 防护和恶意包/依赖安全检查，锁定发布依赖；在实际 Linux Docker 环境测试。
- 当前 `require_auth=false`、监听 `0.0.0.0`、共享历史是已选择的设计，不擅自改为每个用户输入凭证。但企业系统应通过 Spring Boot/网关鉴权和网络策略保护，客户端不能绕过网关直连共享服务。
- 实际查询证据会进入所配置云模型，敏感数据必须按企业外发政策评估；可改为已获准模型或企业内网推理。

### P2：性能与增强

- 仅在 P0 正确性验收后，用真实样本比较 openpyxl 与 calamine/fastexcel 的时延、峰值内存和一致性，保留 fallback 的可追踪性。
- 复杂工作簿的结构理解可由模型辅助，但必须引用具体坐标并通过确定性检查；SQL 核心仍不开放任意 Python 执行。
- 加入独立结果验证：答案中的数量/金额应与 SQL 证据比对，特别是当前额外最终回答模型调用可能重述错误。

## 建议验收门槛（项目建议，不是行业认证标准）

- 发布支持范围内的所有黄金解析用例：关键编号、金额、日期、单位和行列坐标完全一致；禁止无告警损坏或丢行。
- 不支持/低可信格式：明确阻断或经明确告警接受，不得标记为“完全正确”。
- 问答正确率单独测量；例如设置业务验收集的答案准确率目标，同时要求关键金额/记录数用例全部正确。准确率阈值由业务负责人决定，不能拿库的速度或单元测试数替代。
- 输入、SQL、输出、报告、生命周期、安全、负载、恢复全部有可复跑验收。

## 本次测试与限制

- `tests.test_upload_limits` + `tests.test_attachment_policy`：11 项通过。
- 将这两组与 `test_prompt_boundary`、`test_header_reparse`、`test_atomic_versions`、`test_draft_revision_cas` 合并：20 项中 8 个错误；分组复查对应 Windows `WinError 32` 临时 SQLite 文件占用/清理问题。不能宣称整套通过，也不能仅据清理错误判业务断言都正确。
- 四类上述语义缺陷已用合成数据验证，其中两类实际写入 SQLite 后比对。不属于生产真实文件规模/安全/模型准确率全面验收。
- 未更换解析器、修业务逻辑、进行 Docker 构建或调用云模型。该评估不是安全认证或质量认证。
