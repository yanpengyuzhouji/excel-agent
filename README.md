# Excel 会话附件问答服务

独立的 Excel 问数核心 API：上传工作簿，解析表头与逻辑区域、校验完整性、生成 SQLite 查询库，再由 Agent 查询、计算和回答。支持多工作簿比较、连续会话、HTML 报告和 PDF 下载，可由 Spring Boot 或其他后端接入。

当前版本 **1.2.0-rc.39**。核心 Docker 镜像仅包含 API，聊天页面作为独立测试前端运行。附件不进入向量知识库，但原文件、查询库、会话和报告会持久保存。

## 快速部署

需要 Docker Engine / Docker Desktop 的 Linux 容器环境及 Docker Compose。将 `.env.example` 复制为 `.env`，填写 `DEEPSEEK_API_KEY`，已有配置请勿覆盖。

```sh
# 核心 API
docker compose up -d --build

# 可选：启动项目自带测试页面
docker compose -f compose.yaml -f compose.test.yaml up -d
```

| 入口 | 地址 |
|---|---|
| API | `http://127.0.0.1:8091/api/v1` |
| OpenAPI 交互文档 | `http://127.0.0.1:8091/docs` |
| 测试页面 | `http://127.0.0.1:8092/ui/` |
| 健康检查 | `http://127.0.0.1:8091/api/v1/health` |

部署已验收镜像归档时先 `docker load`，再用 `docker compose up -d --no-build` 保留原镜像。Windows/Ubuntu、配置和持久化说明见[部署文档](docs/docker-deployment.md)。

本地 Python 运行：安装 `requirements.lock`，执行 `python run.py`，打开 `http://127.0.0.1:8090/ui/`。PDF 需要原生库及中文字体；Docker 已包含 WeasyPrint、Pango 和中文字体。开发测试依赖见 `requirements-dev.txt`。

## 数据与 Agent 链路

```text
创建会话 → 上传附件 → 文件安全与规模检查 → 解析及来源留痕
         → 完整性校验 → 自动建库／严重告警确认 → 字段目录与 SQLite
         → 模型选择工具 → 参数、来源、预算和重复调用检查 → 查询／计算
         → 结果及执行状态反馈 → 必要时补查 → 一次发布前观察
         → 解析缓存引用 → 回答／报告 → 保存会话、证据和上下文
```

采用自研单 Agent 循环。模型负责理解需求、选择字段和统计口径，程序负责只读查询、确定性计算、引用及资源约束。大目录未提供字段含义时先定向检查 schema，已有结果可按 ID 复用。固定输入的重复计算受到循环保护。

当前有 **9 个业务工具和 1 个结束动作**，兼容别名不重复计数：

| 工具 | 能力 |
|---|---|
| `search_workbooks` | 检索授权工作簿目录 |
| `inspect_workbook` | 检查字段、类型、样例及来源备注 |
| `query_workbook` | 执行只读 SQL |
| `profile_workbook` | 统计行数、NULL、空白及非空值 |
| `verify_record_count` | 检查计数范围及疑似非业务行 |
| `read_query_result` | 分页读取缓存结果 |
| `aggregate_results` | 计算及跨工作簿匹配 |
| `observe_execution` | 查看证据、失败来源、截断和计算依赖 |
| `html_interpreter` | 根据证据保存分析报告 |
| `terminate` | 发布回答并结束本轮 |

计算支持合并、求和/分组求和、差额、增长率、比值、加权平均、全范围占比、两期整体比较、精确键匹配。期间整体比较可从同一输入原子返回两期总额、差额和增长率；NULL、零基数和截断输入明确处理。

只有明确要求报告才生成报告。当前路径由模型提交公开回答模板，服务解析缓存引用并统一渲染文本、HTML 和 PDF，避免重新生成造成数值不一致。执行过程和报告预览在完成后默认收起，支持历史恢复与下载。

引用校验确认值来自哪个缓存单元格，不能证明业务筛选和任务覆盖必然正确。SQLite REAL 聚合仍可能有浮点尾差，Decimal 工具与精确投影也不能恢复源数据已经丢失的精度。当前没有全文数字扫描、强制前置分析计划或第二模型语义裁判硬门禁。

## 模型与上下文

默认使用官方 DeepSeek，选择器 `deepseek-v4-flash`，实际模型 `deepseek-flash`，接口 `https://api.deepseek.com/v1`，密钥变量 `DEEPSEEK_API_KEY`。默认温度0.1、最大输出8192 tokens、首个非空正文10秒、单次调用总时限90秒。

选择顺序为请求指定模型、会话选择、服务默认。候选模型保留兼容配置，各供应商参数和配额不同；`GET /models` 返回配置，不代表实时可用性。DeepSeek 控制阶段使用 JSON 动作协议并关闭思考；思考模型可单独配置首正文时限。

容器通过外部 API 调用模型，不加载模型权重，无 GPU 要求。必要的目录、数据样本、查询结果和历史内容会发送到所选供应商。非秘密配置 `config.yaml` 随镜像提供，运行密钥通过 `.env` 注入；可用 `EXCEL_CONFIG_PATH`、`EXCEL_STORAGE_PATH`、`EXCEL_LLM_BASE_URL`、`EXCEL_LLM_MODEL`、`EXCEL_LLM_API_KEY` 等覆盖。容器内 `localhost` 指容器自身。

上下文由项目自研管理，默认应用窗口32k、输出预留8192，按字符数量估算 Token，接近阈值时提取式压缩。原消息和证据保留，被省略的数据可按结果 ID 读取；压缩会省略信息，不清零累计预算。证据发布回答通常先解析完整候选答案再分块发送，逐字显示不等于逐个模型 Token 立即透传。

## 附件、会话与数据

- 支持 XLSX、XLSM、CSV；旧 XLS、WPS ET 不属于保证支持格式。
- 支持可可靠识别的多层表头、合并单元格、多逻辑区域与键值汇总；保留原始值、坐标、类型、格式、公式和缓存，建库前核对源事实和覆盖完整性，禁止静默有损回退。
- 安全识别并完整留痕的备注、重复表头等可自动处理；其他严重告警需要确认，硬错误不能强制放行。备注与业务明细分开保存，可供问答读取。
- 没有完整 Excel 公式重算引擎，缺失或未经验证的公式缓存可能需要确认。解析更新不会自动重建旧附件，需要重新上传。
- 未选择附件时走通用聊天，选择附件时走问数；本轮范围限定为会话内已就绪附件。
- 持久保存消息、附件关联、模型选择、工具证据、数据快照、耗时、报告及压缩检查点，容器重启后可恢复。
- 首问生成默认标题，历史支持右键重命名、置顶和删除。删除会话/附件时清理独占数据，仍被其他资源引用的工作簿保留。
- 没有自动 TTL，仍存在的会话和附件会持续保存。

命名卷 `excel-data` 挂载 `/data`，保存元数据库、附件、草稿、查询库和报告。替换容器不等于删除数据。`.env`、业务文件、私有测试结果和镜像归档不提交 Git、不进入核心镜像。

## 资源与运维

| 项目 | 默认配置 |
|---|---:|
| 核心 CPU / 内存上限 | 4核 / 8GiB |
| 单文件上传 | 200MiB |
| 单工作表逻辑行 / 列 | 100,000 / 512 |
| 导入单元格预算 | 600,000 |
| 每会话附件 / 每轮查询工作簿 | 5 / 3 |
| 并发导入 / SQL / Agent | 2 / 4 / 10 |
| 单条 SQL 时限 / 默认返回行 | 5秒 / 500 |
| 单轮 Agent 时限 | 300秒 |
| 单轮模型 / 工具 / SQL 调用 | 最多100 / 100 / 50次 |
| 单轮 / 会话累计 Token 估算预算 | 200k / 500k |

单实例准入控制在繁忙时返回429及 `Retry-After`，没有分布式队列和跨副本锁；供应商另有独立限频和额度。直接增加容器副本不能自动解决 SQLite、会话和锁的共享问题。

容器以非 root 用户运行，提供健康检查、异常退出重启、日志轮转（10MiB × 3）和模型诊断编号。数据采用一致性冷备及独立卷恢复，已有崩溃与实际 OOM 演练；自动备份、异地保存及保留周期需另外安排。见[备份恢复文档](docs/backup-recovery-drill.md)。

## 接口接入

统一前缀 `/api/v1`，完整参数与 SSE 协议见[接口文档](docs/API.md)。接入流程为创建会话、上传附件、处理告警、发送问题、消费 SSE、读取历史或下载报告。

```json
{
  "conv_id": "会话ID",
  "question": "统计各区域投资及占比",
  "model": "deepseek-v4-flash",
  "scope": {"mode": "selected", "workbook_ids": ["附件工作簿ID"]},
  "max_rows": 500
}
```

| 接口 | 用途 |
|---|---|
| `POST /agent/conversations` | 创建会话 |
| `GET /agent/conversations` | 会话列表 |
| `GET/PATCH/DELETE /agent/conversations/{id}` | 详情、修改、删除 |
| `POST /agent/conversations/{id}/attachments` | multipart 上传，字段名 `file` |
| `POST /agent/conversations/{id}/attachments/{import_id}/confirm` | 确认严重告警 |
| `POST /agent/ask` | SSE 问答 |
| `GET /agent/conversations/{id}/reports/{report_id}` | HTML 报告 |
| 同一报告路径追加 `/pdf` | PDF 下载 |
| `GET /models`、`/capabilities`、`/health` | 配置、能力及健康 |

SSE 事件包括 `session`、`step.start`、`step.done`、`context.usage`、`answer.delta`、`html`、`done`、`error`。按空行分隔事件，HTTP 200仅表示流建立，需等待终止事件。Spring Boot / 网关需关闭缓冲并透传状态、文件和 SSE。当前不提供可靠断线续传、问答幂等键或后台任务取消。

默认关闭鉴权，访问者共用匿名会话与附件。适用于可信网络或鉴权网关后的单实例辅助问数。正式系统需落实用户身份及资源归属授权；共享服务本身没有用户级多租户隔离。

## 验证状态

官方 DeepSeek 的RC39冻结验收见[发布记录](docs/release-1.2.0-rc.39.md)：Linux测试376通过、1项字体条件跳过；两轮业务回归分别64/64自动通过，36道数字题已复核；隔离运维14项通过，约15分钟640次模拟请求无失败，含崩溃、实际OOM和恢复验证。合成压力不代表真实模型10路并发或24小时 SLA。

魔搭 Qwen3.8-27B 已跨日覆盖100题，最新合并采用后续复测和用户手工回答：95题自动初筛通过、3题手工复测通过、1题人工复核接受，当前保留1题自行舍入导致的精度丢失。手工结果依据最终回答，未提供对应工具轨迹；不能解释为一次全量首轮99%准确率。历史见[魔搭复测记录](docs/rc39-modelscope-regression-2026-10-17.md)，完整记录由 `scripts/consolidate_modelscope_regression.py` 整理至本地私有 `data/business_acceptance/`。

真实业务黄金集、标准答案、逐题结果和截图不随仓库发布；复现需准备授权测试数据。候选模型配置存在不代表通过同等生产验收。报告仍有排版改进空间，关键业务结论需复核。测试脚本位于 `tests/`、`scripts/`，模型回归会消耗供应商额度。

旧单工作簿后端接口保留兼容。旧实验资料归档在本地 `cleanup-archive/`，不进入镜像和远程仓库。
