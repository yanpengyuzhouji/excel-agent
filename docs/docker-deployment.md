# Excel API 封装与部署

当前冻结候选为RC39，默认直接使用官方DeepSeek Flash，不经百炼；同版本完整100题和隔离运维状态见[RC39收敛验收](release-1.2.0-rc.39.md)。历史RC13/RC14/RC16结论不能代表当前镜像；本页说明封装、测试与部署步骤，最终放行以当前版本的独立完整验收记录为准。

## 交付边界

核心镜像由根目录 Dockerfile 构建，包含 FastAPI、Excel 解析/校验/SQLite 建库、会话上下文、多工作簿 Agent、模型适配器、HTML/PDF 报告及 Linux PDF 库和中文字体。模型服务通过网络调用，模型权重不包含在镜像中。

核心镜像不包含前端、.env、业务文件和历史数据。容器内没有 `/ui/`，提供 `/api/v1`、`/docs`、`/openapi.json`。旧单工作簿、数据空间等兼容 API 仍存在；本次不删除这些路由，新系统接入使用 [附件会话协议](API.md)。

Excel 服务管理附件、查询库、上下文、执行记录和报告。Spring Boot 管理身份、业务权限及用户与 conv_id 的映射，并转发 SSE 和文件。当前服务使用共享匿名空间，正式系统应保证 Excel 服务仅被可信后端访问，并在后端逐次检查会话访问权限。

## Windows 本地测试

安装 Docker Desktop，启用 WSL 2 后端并切换 Linux containers。启动 Docker Desktop，执行 `docker version` 应同时有 Client 和 Server；只有 Client 或提示 dockerDesktopLinuxEngine 不存在时，需要先启动引擎。本地无需安装 Python、Node 或数据库。

在项目目录的 PowerShell 操作：

```powershell
Set-Location D:\git\excel-workbook-service
# 仅首次执行；已有 .env 时不要覆盖
if (-not (Test-Path .env)) { Copy-Item .env.example .env }
notepad .env
```

在 .env 填写真实 DEEPSEEK_API_KEY；需要魔搭时填写 MODELSCOPE_API_KEY。默认模型沿用 config.yaml，也可设置 EXCEL_LLM_BASE_URL、EXCEL_LLM_MODEL、EXCEL_LLM_API_KEY。不要将真实密钥写入镜像或提交仓库。模型列表表示已配置，不代表密钥有效或供应商在线。

切换供应商时还需同步config.yaml中的provider及extra_body等协议选项，不能仅更换URL和模型ID。
例如DeepSeek控制步骤使用其thinking参数；其他OpenAI兼容服务未必接受该参数，需单独验证。

启动核心 API 和可选测试页面：

```powershell
docker compose -f compose.yaml -f compose.test.yaml config --quiet
docker compose -f compose.yaml -f compose.test.yaml up -d --build
docker compose -f compose.yaml -f compose.test.yaml ps
Invoke-RestMethod http://127.0.0.1:8091/api/v1/health
Start-Process http://127.0.0.1:8092/ui/
```

API 地址 http://127.0.0.1:8091，Swagger http://127.0.0.1:8091/docs，测试页面 http://127.0.0.1:8092/ui/。原生 8090 服务可以继续运行；测试页面所有请求代理到新容器，不读取原生服务的数据。测试 UI 是单独的 Nginx 容器，只读挂载现有页面，代理关闭 SSE 缓冲；浏览器不需要跨域配置，报告预览保持同源。

可在 .env 设置 EXCEL_PUBLISHED_PORT、EXCEL_TEST_UI_PORT 调整宿主机端口，容器内 API 固定 8090。若调用 Windows 本地模型，使用 `http://host.docker.internal:模型端口/v1`，并保证模型监听宿主机可达接口；容器内 127.0.0.1 指容器本身。

不需要测试页面时，仅运行：

```powershell
docker compose up -d --build
```

## 验收步骤

1. health 正常；查看 capabilities，确认 pdf_available。health 不检查模型。
2. 新建会话，不加附件发送问题，验证通用回答和逐字流式输出。
3. 上传简单 XLSX，确认 ready；复杂文件需要确认时展示严重告警，blocked 不允许强制通过。
4. 选择 ready 附件，核对求和、分组与记录数；刷新页面检查历史及附件恢复。
5. 明确要求生成报告，检查 HTML 预览和 PDF；未要求报告时不应强制生成。
6. 删除测试会话，检查独占数据清理；保留另一会话，重启容器检查持久化。

测试页面和模型问答需要实际启动后验证，不应将配置检查或 health 成功等同于完整验收。

## 日常操作和存储

```powershell
docker compose logs --tail 100 -f excel-agent
docker compose restart excel-agent
docker compose -f compose.yaml -f compose.test.yaml down
```

命名卷挂载 `/data`，保存元数据库、源附件、草稿、查询库、报告和文件日志。Compose 项目名决定卷名前缀；迁移时保持项目名或显式迁移卷。down 保留卷，`down -v` 会删除数据，不作为普通重启命令。现有 Windows 原生 data 目录不会自动迁移到容器。

备份应覆盖整个数据卷。初期采用停服后备份，再启动；恢复到独立环境验证附件、会话和报告。正在使用的 SQLite 数据文件不宜直接逐个复制充当一致性备份。当前无自动 TTL，删除会话按引用关系清理，清理警告应检查并跟进。

默认单实例，2 CPU、2 GiB 内存；导入并发 1、Agent 并发 2、SQL 并发 4，超限 429，通常带 Retry-After: 5。导入限制为全工作簿合计 600,000 个网格单元格、每表 100,000 行（包含表头）、512 列。200 MB 上传限制不是可解析规模承诺，复杂样式和稀疏区域仍可能显著增加内存。资源上限不是任务级内存隔离，不保证所有接近文件上限的导入都能在 2 GiB 完成。不要用多个实例共享数据卷直接横向扩容：并发锁与任务状态主要是进程内的。

## Ubuntu 和正式接入

安装 Docker Engine 与 Compose 插件，复制项目、运行时 .env，执行 `docker compose up -d --build`。构建需要访问基础镜像、Debian 和 Python 软件源；离线环境先在同架构环境构建并通过 docker save/load 分发镜像。当前候选版见 [RC8发布说明](release-1.2.0-rc.8.md)：基础镜像固定摘要、Python运行依赖由 requirements.lock 精确锁定。Debian软件源未做历史快照，不声称未来重新构建逐字节一致；验收部署应分发同一已核验镜像，记录image ID与导出包SHA256。

Spring Boot 在同一宿主机可访问 127.0.0.1:8091；在另一个容器时应使用共享 Docker 网络下的服务名 excel-agent:8090。公网入口由正式网关管理，测试页面不是交付依赖。代理需放行实际上传大小，关闭流式缓冲，读取超时建议 600 秒；不自动重试问答 POST，避免重复执行和计费。

仅分发离线核心服务时，交付已验收镜像tar、compose.yaml、.env.example及本部署文档即可；
业务原文件、验收原始证据和测试前端不随镜像发布。在Ubuntu上：

```bash
sha256sum excel-workbook-service-1.2.0-rc.8-linux-amd64.tar
docker load -i excel-workbook-service-1.2.0-rc.8-linux-amd64.tar
# 与验收清单核对摘要后，配置当前目录.env中的真实密钥
cp .env.example .env
chmod 600 .env
docker compose up -d --no-build
curl --fail http://127.0.0.1:8091/api/v1/health
```

上述离线包面向linux/amd64。不要加`--build`，否则验收镜像可能被新构建覆盖。
Compose从当前项目的.env读取并注入声明的环境变量；容器并不读取宿主机固定路径。
默认config.yaml已烘焙在镜像中，改宿主机同名文件不会自动生效。如需外置配置，应通过
只读挂载并设置EXCEL_CONFIG_PATH明确指定；修改配置后需重新做相应能力验收。

当前不提供可靠断线续传、取消任务、跨副本协调和自动过期。Docker 构建、PDF 输出及模型连通性需要在运行 Docker 引擎的机器实际验收。

## 常见问题

| 现象 | 检查与处理 |
|---|---|
| Docker engine 管道不存在 | 启动 Docker Desktop，检查 Linux containers / WSL 2 |
| 端口占用 | 修改 .env 中宿主机端口并重建容器 |
| health 正常但模型失败 | 检查运行环境密钥、额度、模型 ID、外网访问，查看 SSE error |
| 413 或文件被拒绝 | 同时检查代理上传上限与解析服务资源限制，保留原始错误详情 |
| 429 | 任务忙或资源限流，遵循 Retry-After；问答先查会话状态再决定是否重试 |
| 回答最后一次性出现 | 检查所有代理是否缓冲，按 API.md 增量解析 SSE |
| 测试页面 502 | 检查 excel-agent 是否健康及 Nginx 日志 |
| PDF 不可用 | capabilities 仅检查依赖可加载，实际渲染错误需看 API 状态及容器日志 |
| 容器 OOM / exit 137 | 查看 Docker 内存分配及容器状态，调整并发/文件规模/资源 |
