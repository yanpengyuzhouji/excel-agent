# 1.2.0-rc.3

继承[RC2修复与交付边界](release-1.2.0-rc.2.md)。RC2完整回归中出现三次文本工具
协议解析失败，不能以之后重试成功代替修复。RC3将已实测支持JSON对象输出的
DeepSeek控制步骤切换为单个`action/action_input` JSON对象，最终回答仍逐字流式输出。
其他供应商保持原兼容方式，解析器也继续接受旧Action/DSML格式。

镜像：`excel-workbook-service:1.2.0-rc.3`，以验收的实际image ID为准。
本次认证模型为DeepSeek Flash；其他模型的兼容配置不代表已经通过生产验收。
业务签字、Spring Boot权限验收及24小时以上持续运行不由本轮技术测试替代。
