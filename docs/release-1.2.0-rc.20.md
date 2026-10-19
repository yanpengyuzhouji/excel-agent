# RC20 修复交付与验收阻塞

> 后续进展：候选权限已开放，完整100题（Max及DeepSeek 0731额度接续）已执行，准确性仍未放行。当前默认已改为DeepSeek 0731。下文为切换前的历史记录，最新镜像、配置与结果见[本轮验收](production-gate-rc20-qwen38-2026-10-16.md)。

日期：2026-10-16。当前候选包含RC19的跨结果匹配、直接发布草稿、计算口径和字段单位修复，以及以下补充修复。

## 本次补充

1. 必须的计算已执行、但模型未写引用占位符时，服务端补充该运算的缓存结果引用并解析，不再误报“未计算”。未执行计算仍拒绝，未知/越界显式引用仍拒绝；不通过放宽全局数字扫描或修改业务期待值达到通过。
2. 结构检查的列过滤找不到字段时，返回unknown_requested_columns、明确提示和真实物理字段/样本，避免静默空结构诱导循环。SQL仍只使用真实字段，不自动把h_value猜改成column_h。
3. 百炼403 AllocationQuota.FreeTierOnly识别为quota_exhausted，不再把免费额度耗尽误诊为无效密钥。日志仅增加有限、明确的供应商错误代码，不记录原始错误正文或凭证。
4. 增加两个候选选择器：qwen3.8-max-bailian、deepseek-v4-flash-0731-bailian。默认仍为用户指定的qwen3.7-flash-bailian，禁止普通超时、模型答错时静默替换。实际替换前需要通过模型访问检查。
5. 新评分规则识别“差额（本期−基期）：50”等格式，但不接受说明中任意位置的基期0冒充差额；原始RC19判分不改写。

## 验收状态

- 已部署RC20，容器健康。最终镜像：sha256:2dc2a945c0a4a18f49c0b63667b5dac63e16b4b95024cccff68dfc7655423c82。
- 完整隔离回归298项：297通过、1项字体条件跳过；部署50个源码/配置/锁文件哈希无差异。
- 隔离回归与部署一致性分别记录于data/business_acceptance/rc20-unit-acceptance.json和rc20-source-consistency.json。
- RC19的完整首轮见production-gate-rc19-qwen37-2026-10-16.md。
- RC20重新执行完整100题已获用户授权，但尚未启动：主模型免费额度已耗尽、两个候选被API Key限制拒绝。探测证据为data/business_acceptance/bailian-access-20261016.json。不得把RC19结果沿用为RC20已通过。
- 未修改账户计费、免费额度保护或云端模型白名单；需要用户在百炼控制台放开至少一个候选权限，或选择为主模型启用付费续用。

## 部署配置

密钥位于被Git及Docker构建排除的本地.env.bailian，环境变量BAILIAN_QWEN37_API_KEY；Compose运行时注入。不应提交该文件。迁移主机需自行创建同名环境文件或使用docker run的运行时环境变量注入密钥；镜像内不包含密钥文件。

当前应用上下文仍32k、首正文10秒、调用总超时90秒，保留工作区既有4CPU/8GiB资源和Agent并发设置。本轮没有重复长测、停服备份或修改原业务数据。

官方说明：[百炼免费额度与用完即停](https://help.aliyun.com/zh/model-studio/model-usage-statistics)、[供应商错误码](https://help.aliyun.com/zh/model-studio/error-code/)。关闭免费额度保护将允许产生付费，不是无成本的故障修复步骤。
