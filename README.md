# Excel 会话附件问答服务

一个独立 FastAPI 服务：在聊天输入框添加或拖入 XLSX、XLSM、CSV，后台复用原解析、校验、版本化 SQLite 建库逻辑，再通过受控多工作簿 Agent 查询。附件不加入向量知识库，但文件、临时数据库、会话与报告目前会持久保存；尚未实现自动过期清理。

## 本地运行

复制 `.env.example` 为 `.env`，配置模型 API Key。安装 `requirements.txt`，执行 `python run.py`，打开 `http://127.0.0.1:8090/ui/`。默认开放使用，不需要用户填写连接凭证，访问者共用会话历史与附件。请仅在可信网络使用，不要直接暴露公网。

页面只保留多工作簿问答：左侧会话搜索、重命名、删除；底部附件与模型选择；执行过程与报告预览默认在结束后收起。新会话不自动查询其他会话文件。删除会话会清理其独占附件、原文件、草稿、数据库、目录索引及报告；移除附件也会清理未被其他资源引用的数据。共享附件在最后一个引用消失后清理。

清理前检查全部会话、旧单工作簿会话、数据空间和关系引用；分析或上传进行中返回 429，请稍后重试。文件先暂存，元数据提交失败时恢复；提交后文件暂时无法移除会返回 cleanup_warnings，启动时重试。导入失败的未引用数据会清理，不影响仍需用户处理的告警草稿。不提供自动 TTL，仍存在的会话及其附件会持续保存。

一次性遗留清理脚本为 `scripts/cleanup_orphan_artifacts.py`，默认仅盘点；实际清理必须停止服务并提供已完整归档的存储目录：`--confirm --archive <归档目录>`。执行前逐文件校验 SHA-256。旧的 `clear_conversation_history.py` 仅用于消息表备份/清空，不负责附件生命周期，不应用作生产清理。

会话标题默认取首个问题前 80 字符。列表不显示编辑或删除图标，右键标题可重命名、置顶/取消置顶、删除；键盘 Shift+F10 也可打开菜单。置顶保存于后端并优先排序。

不添加或不选择附件时可直接通用聊天，流式返回文字；不会加载工作簿目录、SQL 证据或调用报告工具。选择可用附件后切换为 Excel 问数。用户消息右对齐浅色气泡，助手回答左对齐，历史会话沿用同样的角色样式。

## 自动导入规则

混合数值、隐藏行、合并单元格属于普通提示。空表头、未识别布局、重复表头/汇总/备注行排除，以及缺失或未经重算验证的公式缓存，暂停自动建库并提示用户确认。多区域或不能可靠解析的多级表头属于硬错误，不可强制确认，需拆表或整理源文件。具有明确字段的小表也支持自动识别；不确定的内容不会被承诺为可靠明细。

P0 解析保护：编号与前导零按文本保留，整数不经过浮点转换；原始值、单元格类型、格式、公式和缓存独立留痕。确认前重新校验源文件 SHA-256，建库在提交前逐项核对值、坐标、类型、行数及源事实；不一致则回滚。解析异常不再静默启用有损 OOXML 回退。金额单位不自动换算，百分比保留底层比例和显示格式；SQLite REAL 汇总仍为近似计算，不用于宣称审计级精度。

这些规则只用于后续导入/确认，不自动重建已经存在的附件。旧附件需要重新上传才会获得完整的新校验与源事实。

## Docker

配置 `.env` 后执行：

```sh
docker compose up -d --build
```

Docker 同样默认关闭鉴权，不需要设置服务访问密钥。任何能访问该服务的人都能查看、修改或删除共享会话，访问其中的附件和报告；如需控制网络访问，请在网关或网络层处理。

容器单实例运行，命名卷 `excel-data` 保存全部数据。镜像不包含 `.env`、现有业务数据或归档。已包含 Linux PDF 原生依赖和中文字体，但实际构建与 PDF 输出仍须在运行 Docker 的环境验证。容器 localhost 不指宿主机；本地模型用宿主机可达地址或同网络服务名。

可配置 `EXCEL_CONFIG_PATH`、`EXCEL_STORAGE_PATH`、`EXCEL_HOST`、`EXCEL_PORT`、`EXCEL_REQUIRE_AUTH`、`EXCEL_LLM_BASE_URL`、`EXCEL_LLM_MODEL`、`EXCEL_LLM_API_KEY`。自定义模型应选择“默认模型”，模型配置与服务密钥都不写入镜像。

## 接入与验证

接口说明见 [docs/API.md](docs/API.md)。SSE 代理关闭缓冲并设置足够的读超时。当前是共享匿名空间，不提供多租户隔离。

```sh
python -m unittest tests.test_attachment_policy tests.test_attachment_api tests.test_context_window tests.test_dsml_protocol tests.test_agent_evidence tests.test_tool_loop_guard tests.test_multi_workbook_agent tests.test_multi_workbook_scope tests.test_qwen_report_recovery
node scripts/test_report_links.cjs
python scripts/test_attachment_ui.py
PYTHONPATH=. python scripts/check_models.py --probe
```

浏览器测试需要本地服务及 Playwright；测试依赖见 `requirements-dev.txt`。模型检查会产生小额度推理请求；协议通过不代表所有业务问题都正确。

旧单工作簿后端接口和数据模型保留兼容，不再提供旧页面入口。过时实验文档、脚本、样式及原配置保存在 `cleanup-archive/2026-10-14/`，可恢复且不进入镜像。
