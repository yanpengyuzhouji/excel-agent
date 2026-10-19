# RC21：通用分析结果链路重构

交付日期：2026-10-17。状态：已实现、离线验证并部署；未完成真实模型准确性放行。

## 本次改动

保留Excel解析、建库、SQL沙箱、精确计算工具和会话。新增分析结果模块，将多工作簿生产链路从“工具调用后自由写答案”改为“声明任务—绑定证据—程序计算—冻结结果—统一发布”。没有新框架或新运行依赖，没有按C01等题号添加生产规则。

- 模型先声明本轮指标、来源、口径和运算；每项叶任务必须绑定缓存坐标。多指标不再由一个关键词运算门禁代表完成。
- 减法用明确的左值/右值；求和、平均、加权、比值、增长和有限单位换算由程序执行，输出明确公式。引用检查、单位和数据完整性集中在同一模块。
- 原始备注/规格/编号走原值或表格路径；无记录、NULL和零分母分别呈现。开放解释与澄清分模式处理，不恢复全文数字拦截。
- 数字回答与HTML/PDF共用冻结结果，不发布模型自由草稿，也不再调用分母正则修改这条生产路径中的公式说明。报告链接由代码提供。
- analysis.planned/resolved和最终结果包持久化；外部问答请求参数不变，响应为可加性扩展。前端支持新工具名及计划展示。
- 修复共享前端渲染的反斜杠、带竖线的表格单元格、HTML样式字面量及PDF链接截断问题。数据不作为脚本执行。
- 黄金业务评分增加按标签、单位、符号值及可选运算的结果包检查；候选不唯一需复核，不再只借全文其他位置的数字证明通过。旧记录、期待值和容差不改。

详细协议：[分析结果契约](analysis-result-contract.md)。

## 验证结果

- 最终镜像隔离测试323项：322通过、1项emoji字体条件跳过，无失败/错误。报告：data/business_acceptance/rc21-unit-final-acceptance.json。
- 测试容器无网络、无业务凭证、不挂业务卷；只读挂载开发用httpx/httpcore包，未加入生产依赖。之前316/322项成功阶段记录保留。Windows本地API测试有沙箱临时目录权限错误，不据此宣称Windows原生完整测试通过。
- 离线重放历史SQL证据：C01差额49000.6912、比值2.3481；C03记录数2202、投资225997.5736；C22总体2021=3919.5213、2022=130794.6680、差额126875.1467、增长3237.01%。目录：data/business_acceptance/rc21-contract-replay/。
- 重放计划是明确写定的测试夹具，未调用模型。它证明编译/计算/发布，不证明模型能生成正确计划，不能计为3道真实模型复测通过。
- 三份重放HTML浏览器可读，无页面错误；PDF均为一页，逐页查看中文、来源、数值和公式，未见裁切。不是对任意大表分页的全面验收。
- 只读前端浏览器测试通过：00001、1*35、竖线和<script>字面量正常呈现，未生成脚本节点；PDF链接保留/pdf。记录：data/business_acceptance/rc21-ui-acceptance.json。
- 51个部署源码/配置/锁文件与本地一致，无差异：data/business_acceptance/rc21-source-consistency-final.json。

可重跑命令：

```powershell
.venv/Scripts/python.exe -m unittest discover -s tests -p test_analysis_result.py
.venv/Scripts/python.exe -m unittest discover -s tests -p test_screening_repairs.py
.venv/Scripts/python.exe -X utf8 scripts/test_analysis_rendering.py
.venv/Scripts/python.exe -X utf8 scripts/replay_analysis_contract.py data/business_acceptance/gold-v2/runs/deepseek-v4-flash-0731-20261016-203303 data/business_acceptance/rc21-contract-replay
```

完整测试应按本轮Linux隔离容器流程执行；本地Windows原生环境的SQLite临时文件句柄与PDF依赖限制仍是独立问题。

## 部署

- 镜像：excel-workbook-service:1.2.0-rc.21。
- 最终ID：sha256:d9163d89f7c18e0c0194105d67a5dfdbf24f02bdc403ea3700065f19a2e4e69f。
- 8091核心、8092测试UI；健康接口返回1.2.0-rc.21/ok。
- 默认仍为百炼deepseek-v4-flash-0731；原模型选择器、密钥注入、32k窗口、10秒首正文、90秒调用及资源设置未变。
- 复用excel-workbook-service_excel-data；无数据库结构迁移，不清空历史、不删除原文件。生成和移动的只有本轮测试证据。
- RC20旧镜像保留；回退可改镜像标签并重建容器，不能删除数据卷。旧版忽略新增meta.analysis。

## 未声称解决的边界

1. 计划由模型提出。若初始计划就漏指标、反向理解任务、错选解释模式、错误定义有效记录或生成错误SQL，结构正确仍可能业务错误；源码校验不能证明自然语言理解正确。
2. scope是声明文本；来源检查确认缓存工作簿/表及输入链，不证明WHERE/JOIN/CASE条件等价于用户意图。G004类口径错误不能据离线结果宣布已彻底消除。
3. SQL浮点查询仍是原精度；Decimal不恢复查询阶段已丢失的原始精度。未知单位不做猜测换算。
4. 模型通常增加一次声明任务轮次，不增加最终自由润色调用。未测量本版本真实token、延迟或模式遵循率。
5. 未重跑付费模型、完整100题、长期稳定性或备份恢复，不沿用RC20结果作为RC21准确性证明。

下一步先做同一模型的小规模原题对照，检验计划生成与模式路由，再决定是否全量。当前可以测试新链路，不可据此正式生产放行。
