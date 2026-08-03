# 单服务器生产部署说明

本项目的生产结构是：Caddy 只公开 80/443，Chatwoot 通过客服域名提供挂件和坐席工作台，运营域名反向代理本项目 API；Dify、PostgreSQL、Redis、Weaviate 和应用端口只绑定本机或容器网络。

## 部署前需要的信息

- 服务器公网 IP、Linux 发行版和 CPU 架构。
- CPU、内存、系统盘和数据盘容量。单机同时运行 Chatwoot、Dify、Docling 和数据库，建议至少 4 核 8 GB；16 GB 更适合真实业务和 PDF 解析。
- 一个具有 `sudo` 权限的 SSH 用户和 SSH 公钥登录。不要在聊天中发送 root 密码。
- 两个已解析到服务器的域名：`SUPPORT_DOMAIN` 和 `OPERATIONS_DOMAIN`。
- Chatwoot 邮件发送配置，以及独立/异地备份挂载点。
- 真实商家的公开品牌配置和经过确认的知识资料。
- 钉钉机器人 AppKey/AppSecret、测试员工 staffId 白名单、门店宽表只读操作者
  unionId，以及机器人应用的 `Notable.Base.Read.All` 权限。

## 配置文件

```bash
cp .env.production.example .env.production
cp config/enterprise.example.json config/enterprise.local.json
chmod 600 .env.production config/enterprise.local.json
```

`.env.production` 保存部署参数和服务端密钥，不提交 Git。`config/enterprise.local.json` 只保存企业名称、欢迎语、知识库/应用名称和挂件颜色，不允许放任何密钥。
`config/store-directory.local.json` 只保存门店宽表、数据表和已批准字段的 ID；
AppSecret 与操作者 unionId 只通过 `.env.production` 或密钥管理服务注入。

## 传输已验证的应用镜像

Docling 模型已经固化在应用镜像中，内地服务器不需要在部署时访问 Hugging Face。开发机完成构建和回归后执行：

```powershell
.\scripts\export-production-images.ps1 -Force
```

把 `artifacts/production/ai-customer-service-images.tar`、同名 `.sha256` 文件和项目源码传到服务器。服务器先校验再加载：

```bash
cd /opt/ai-customer-service
sha256sum --check ai-customer-service-images.tar.sha256
docker load --input ai-customer-service-images.tar
docker image inspect ai-customer-service/docling-api:0.1.0 >/dev/null
```

镜像归档不包含 `.env`、数据库或业务资料。生产密钥只能在服务器本地重新生成和配置。

生产 Compose 校验：

```bash
docker compose --env-file .env.production \
  -f compose.yaml -f compose.production.yaml config --quiet
```

应用容器以 UID/GID `10001` 运行。首次启动前应创建并授权状态目录：

```bash
install -d -m 700 .runtime/customer-service
sudo chown -R 10001:10001 .runtime/customer-service
```

## HTTPS 与网络

`compose.production.yaml` 使用固定版本且锁定摘要的 `caddy:2.11.4-alpine`，自动申请和续期证书，并持久化 `/data` 和 `/config`。防火墙只开放 SSH、80/TCP、443/TCP 和 443/UDP；5001、3000、8081、5432、6379 等端口不能直接暴露公网。

Chatwoot 的 webhook 必须设置为：

```text
https://OPERATIONS_DOMAIN/v1/chatwoot/webhook
```

生产环境不要启用 `SAFE_FETCH_ALLOW_PRIVATE_NETWORK`。Dify 管理控制台默认不对公网显示；如确有独立保护的管理入口，再设置 `DIFY_CONSOLE_PUBLIC_URL`。

## 启动与验收

```bash
docker compose --env-file .env.production \
  -f compose.yaml -f compose.production.yaml up -d --no-build
bash scripts/production/health-check.sh
```

上线前依次验证：HTTPS 证书、未知 Host 拒绝、`/health`、`/ready`、运营密钥、知识上传、真实 Chatwoot 挂件消息、Dify 回答、转人工、真人公开回复、超时托底、恢复 AI 和旧消息不补答。钉钉 Stream 还需单独验证测试账号白名单、会话去重、敏感问题拦截，以及在 `DINGTALK_STORE_PROVIDER=openapi` 时读取一条真实门店；验收期间不应依赖部署人员的 DWS 登录态。

## 备份与恢复

安装 Docker、GnuPG、tar 和 coreutils，创建权限为 `600` 的随机备份口令文件。`BACKUP_ROOT` 必须是独立磁盘或会同步到另一物理位置的挂载点。每日执行：

```bash
bash scripts/production/backup-production.sh
```

备份包含应用 SQLite、Chatwoot PostgreSQL/附件/配置、Dify 全量 volumes/配置和项目生产配置；产物经过 GPG AES-256 加密并生成 SHA-256 校验文件。Dify 全量快照期间会有短暂停机窗口。

恢复会替换业务数据，只能在维护窗口执行，并要求精确确认词：

```bash
bash scripts/production/restore-production.sh \
  --backup /path/ai-customer-service-TIMESTAMP.tar.gz.gpg \
  --confirm RESTORE-PRODUCTION
```

脚本保留恢复前的 SQLite、Dify volumes 和 Chatwoot 附件归档。恢复后必须先运行健康检查和核心 E2E，再开放流量。每月至少在隔离环境做一次恢复演练；从未演练过的备份不能视为可靠。
