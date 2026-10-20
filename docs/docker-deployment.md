# Excel API 封装与部署

当前镜像为RC43，默认直接使用官方DeepSeek Flash，不经百炼；RC39完整100题和隔离运维历史记录见[RC39收敛验收](release-1.2.0-rc.39.md)。本次部署配置改为项目目录挂载，不改变镜像内业务代码。

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
New-Item -ItemType Directory -Path .\data\docker -Force
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

默认将项目 `./data/docker` 绑定挂载到容器 `/data`，保存元数据库、源附件、草稿、查询库、缓存索引、报告和文件日志。相对路径以compose.yaml所在项目目录为基准，不依赖固定盘符、主机IP或卷名前缀。`down`保留宿主目录；不要使用带删除数据目的的清理命令。旧Windows原生data/workbook_imports与历史测试集不会混入服务数据。

本机位置示例：`D:\git\excel-workbook-service\data\docker`。Ubuntu示例：`/opt/excel-workbook-service/data/docker`。查看文件可以，直接编辑源附件、草稿或SQLite会破坏缓存及证据一致性。复制项目代码时不要无意将真实业务数据和.env发送到其他环境。

Windows目录跨虚拟机挂载可能增加I/O延迟；Ubuntu生产目录建议使用本地磁盘，不要直接将SQLite工作目录放在共享网络文件系统。

备份应覆盖整个数据卷。初期采用停服后备份，再启动；恢复到独立环境验证附件、会话和报告。正在使用的 SQLite 数据文件不宜直接逐个复制充当一致性备份。当前无自动 TTL，删除会话按引用关系清理，清理警告应检查并跟进。

默认单实例，当前compose限制4 CPU、8 GiB内存；运行时并发以config.yaml为准，附件缓存发布在单进程内串行，超限通常返回429。导入限制为全工作簿合计600,000个网格单元格、每表100,000行（包含表头）、512列。200 MB上传限制不是可解析规模承诺。不要用多个实例共享数据目录直接横向扩容：并发锁与任务状态主要是进程内的。

## Ubuntu 和正式接入

安装Docker Engine与Compose插件，将项目部署文件复制到目标目录，配置该主机自己的.env，然后在项目目录执行以下命令（示例项目路径需替换）：

```bash
cd /opt/excel-workbook-service
mkdir -p data/docker
sudo chown 10001:10001 data/docker
# 已有.env不要覆盖；首次从.env.example复制并填写模型密钥
test -f .env || cp .env.example .env
chmod 600 .env
docker compose up -d --build
docker compose ps
```

容器以UID/GID 10001运行，Ubuntu目录没有写权限会导致SQLite无法创建；不要以chmod 777代替权限配置。默认只监听127.0.0.1，可信局域网访问在.env设置EXCEL_BIND_HOST=0.0.0.0并放行所需端口。仅核心服务不需要compose.test.yaml；如需测试前端，附加该文件启动。

源码构建需要Dockerfile、app、config.yaml、run.py、requirements.txt和requirements.lock；无需复制.venv、node_modules、测试集。构建需要访问基础镜像、Debian及Python软件源。也可直接分发同架构的已验收镜像：目标执行docker load后使用docker compose up -d --no-build，部署包保留compose.yaml、配置及.env即可；需要测试前端时额外带examples/frontend、deploy/test-ui.conf。数据不包含在镜像中，迁移已有业务必须单独迁移整个data/docker。基础镜像固定摘要，依赖用requirements.lock锁定；重新构建不保证逐字节一致。

## 旧命名卷迁移与回退

只在维护窗口执行。先停核心及测试UI，确认没有任务执行，将旧命名卷只读挂载为/source，空的data/docker挂载为/target，独立data/backups挂载为/backup，通过本仓库scripts/migrate_volume_to_bind.py备份并复制。脚本拒绝非空目标，校验归档、所有文件SHA256及SQLite完整性；不覆盖历史目录，不删除旧卷。迁移失败不要启用半成品目录，应先用旧卷恢复服务。

本机迁移后原卷excel-workbook-service_excel-data保留。回退命令（可选测试UI仍放在最后）：

```bash
docker compose -f compose.yaml -f deploy/compose.legacy-volume.yaml -f compose.test.yaml up -d --no-build
```

原卷是切换时的快照，切换后新产生的会话不会回写旧卷；有新数据时回退必须先停服备份并制定反向迁移，不能直接切回造成新记录不可见。

2026-10-20本机已实际完成迁移：41个文件共808,803,197字节，全部文件哈希一致，13个SQLite检查正常；目录所有者对齐10001后服务健康。14个会话、666条消息、11份工作簿与导入记录、1项缓存元数据逐项一致，临时附件写入和清理通过。备份位于data/backups，原命名卷保留。此记录是本机迁移验证，不代表Ubuntu已实际部署或完整备份恢复演练已重新执行。

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
