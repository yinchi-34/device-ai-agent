# 部署指南

[English](DEPLOYMENT.md)

本文说明如何把 Device AI Agent 部署到云服务器上，供内部正式使用。步骤与云厂商无关：
只要有 Linux 云主机、七层负载均衡和安全组（防火墙规则），做法都一样。

## 目录

- [目标架构](#目标架构)
- [准备工作](#准备工作)
- [1. 身份提供方](#1-身份提供方)
- [2. 服务器](#2-服务器)
- [3. 配置](#3-配置)
- [4. 首次启动](#4-首次启动)
- [5. 负载均衡和防火墙](#5-负载均衡和防火墙)
- [6. 验证](#6-验证)
- [升级](#升级)
- [备份与恢复](#备份与恢复)
- [日志与监控](#日志与监控)
- [安全检查清单](#安全检查清单)

## 目标架构

```text
用户 / 程序
      │  HTTPS 443
      ▼
负载均衡 ── 终止 HTTPS、托管证书、对 /health 做健康检查
      │  HTTP，只走内网
      ▼
服务器（安全组：只允许负载均衡访问 8000 端口）
  gateway（Caddy）:8000 ── IP 白名单，通过 oauth2-proxy 登录
    ├── oauth2-proxy ── 对接身份提供方
    └── control-api ── 再次校验 JWT，写审计记录
          ├── db（PostgreSQL + pgvector，卷 db-data）
          ├── redis
          └── document-worker（卷 document-files）
```

服务器的内网地址上只有网关在监听。API、数据库、Redis 和 oauth2-proxy 都只能在
Docker 内部网络里访问（数据库另外在 `127.0.0.1` 上开放，便于运维）。

## 准备工作

- 一台 Linux 云主机：起步 2 核 4 GB 就够；OCR 任务多、文件大时再加内存。安装 Docker
  Engine 和 Compose v2。
- 一个域名，例如 `device.example.com`，以及配在负载均衡上的证书。
- 一个 OIDC 身份提供方（Keycloak、飞书、Azure AD、Okta 等）。生产环境不要用仓库自带的
  本地 Keycloak。
- 所用模型服务的 API Key（抽取、视觉、Embedding、诊断）。

## 1. 身份提供方

需要登记三类对象（细节见
[接入正式的身份提供方](DEVELOPMENT.zh-CN.md#接入正式的身份提供方)）：

1. **网关**：一个机密（confidential）OIDC 客户端，回调地址为
   `https://device.example.com/oauth2/callback`。客户端 id 填到 `DEVICE_OIDC_AUDIENCE`，
   密钥填到 `DEVICE_GATEWAY_CLIENT_SECRET`。
2. **人**：允许登录的用户，邮箱需要已验证。
3. **程序**：每个程序单独一个 client credentials 客户端，加 audience mapper，让 `aud`
   包含网关的客户端 id，并带上 `email` claim，这样审计记录里能看到是哪个程序。

从身份提供方的 `/.well-known/openid-configuration` 中记下 `issuer`、
`authorization_endpoint`、`token_endpoint` 和 `jwks_uri`。

## 2. 服务器

```bash
# 在服务器上，用有 sudo 权限的账号
sudo apt-get update && sudo apt-get install -y git
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker "$USER"   # 重新登录后生效

sudo mkdir -p /opt/device-ai-agent && sudo chown "$USER" /opt/device-ai-agent
git clone https://github.com/yinchi-34/device-ai-agent.git /opt/device-ai-agent
cd /opt/device-ai-agent
git checkout <发布 tag 或 commit>   # 部署固定版本，不要直接跟着分支走
```

## 3. 配置

```bash
cp .env.example .env
chmod 600 .env
```

编辑 `.env`，下面这些值都必须改掉示例值：

| 变量 | 生产环境的值 |
| --- | --- |
| `COMPOSE_PROFILES` | 删除这一行（不启动本地 Keycloak） |
| `DEVICE_DATABASE_PASSWORD` | 足够长的随机值，只用 URL 安全字符 |
| `DEVICE_ADMIN_PASSWORD` | `/admin` 审核台的随机长密码 |
| `DEVICE_OIDC_ISSUER` | 身份提供方的 `issuer` |
| `DEVICE_OIDC_AUDIENCE` | 网关客户端 id |
| `DEVICE_OIDC_LOGIN_URL` | `authorization_endpoint` |
| `DEVICE_OIDC_TOKEN_URL` | `token_endpoint` |
| `DEVICE_OIDC_JWKS_URL` | `jwks_uri` |
| `DEVICE_GATEWAY_PUBLIC_URL` | `https://device.example.com` |
| `DEVICE_GATEWAY_CLIENT_SECRET` | 网关客户端密钥 |
| `DEVICE_GATEWAY_COOKIE_SECRET` | 新生成的随机值，见下 |
| `DEVICE_GATEWAY_COOKIE_SECURE` | `true` |
| `DEVICE_GATEWAY_BIND` | 服务器内网 IP，例如 `10.0.1.15` |
| `DEVICE_GATEWAY_ALLOWED_CIDRS` | 负载均衡所在网段，例如 `10.0.0.0/24` |
| `DEVICE_DOCUMENT_*`、`DEVICE_DIAGNOSIS_*` | 你的模型服务地址、模型名和 Key |

生成密钥：

```bash
python3 -c "import secrets; print(secrets.token_urlsafe(24))"   # cookie 密钥（32 个字符）
python3 -c "import secrets; print(secrets.token_urlsafe(32))"   # 各类密码
```

`.env` 不要提交到版本库，也不要贴到聊天里。如果云平台有密钥管理服务，也可以由它注入同样的
环境变量，不用把密钥留在文件里；应用只读取环境变量。

## 4. 首次启动

```bash
docker compose build
docker compose up -d db redis
docker compose run --rm control-api alembic upgrade head
docker compose up -d
docker compose ps        # 所有服务都应是 running / healthy
```

所有服务在崩溃或服务器重启后都会自动拉起。

## 5. 负载均衡和防火墙

负载均衡（七层，通常叫 ALB、CLB 或应用型负载均衡）：

- 监听 **HTTPS 443**，绑定 `device.example.com` 的证书。
- 可选：再加一个 HTTP 80 监听，重定向到 HTTPS。
- 后端：服务器内网 IP，**端口 8000，协议 HTTP**。
- 健康检查：`GET /health`，期望 `200`。
- 空闲超时至少 120 秒，避免大文件上传被中断。
- 请求体上限至少 30 MB（上传文件上限由 `DEVICE_DOCUMENT_MAX_FILE_BYTES` 控制，默认 25 MB）。

服务器安全组，入方向：

| 端口 | 来源 | 用途 |
| --- | --- | --- |
| 8000/tcp | 只允许负载均衡网段 | 网关 |
| 22/tcp | 只允许管理员 IP（或使用云平台的会话管理） | SSH |
| 其他 | 全部拒绝 | |

数据库、Redis、身份提供方都不需要入方向规则。服务器需要能通过出方向 HTTPS 访问身份提供方和
模型服务。

如果没有负载均衡：把 `DEVICE_GATEWAY_SITE_ADDRESS` 设为 `device.example.com`，对外开放
80 和 443 端口（代替 8000），Caddy 会自动申请和续期证书。

## 6. 验证

```bash
curl -s https://device.example.com/health                     # {"status":"ok"}
curl -s -o /dev/null -w '%{http_code}\n' https://device.example.com/api/v1/device-models   # 401
```

然后在浏览器打开 `https://device.example.com/docs`：应该先跳到身份提供方登录，登录后回到
API 文档页。程序的话，从身份提供方申请一个 client credentials token，带上
`Authorization: Bearer <token>` 调用 API，应返回 `200`。

## 升级

```bash
cd /opt/device-ai-agent
./backup.sh                                   # 见下文；升级前一定先备份
git fetch --tags && git checkout <新 tag>
docker compose build
docker compose run --rm control-api alembic upgrade head
docker compose up -d
docker compose ps
```

数据库迁移在新容器启动之前执行。需要回滚时，切回上一个 tag，并恢复升级前的备份；不要在生产
数据上直接跑 `alembic downgrade`，除非先在副本上验证过。

## 备份与恢复

有两处保存状态，必须一起备份：数据库（元数据、审核记录、向量）和 `document-files` 卷（上传的
原始文件）。把下面的脚本保存为 `/opt/device-ai-agent/backup.sh`：

```bash
#!/usr/bin/env bash
set -euo pipefail
cd /opt/device-ai-agent
set -a; . ./.env; set +a
stamp=$(date +%Y%m%d-%H%M%S)
mkdir -p backups
docker compose exec -T db pg_dump -U "${DEVICE_DATABASE_USER:-device}" -Fc \
  "${DEVICE_DATABASE_NAME:-device_ai_agent}" > "backups/db-$stamp.dump"
docker compose exec -T control-api tar czf - -C /data/documents . \
  > "backups/documents-$stamp.tgz"
find backups -type f -mtime +14 -delete
```

```bash
chmod 700 backup.sh
( crontab -l 2>/dev/null; echo "30 2 * * * /opt/device-ai-agent/backup.sh >> /var/log/device-backup.log 2>&1" ) | crontab -
```

还要把 `backups/` 复制到服务器以外的地方（对象存储或另一台机器）；和数据放在同一块盘上的备份，
盘坏了就一起没了。云盘快照可以作为额外一层保护。

恢复（先停掉会写数据的服务）：

```bash
docker compose stop control-api document-worker gateway
docker compose exec -T db pg_restore -U device -d device_ai_agent --clean --if-exists \
  < backups/db-<stamp>.dump
docker compose run --rm --no-deps -T control-api sh -c 'tar xzf - -C /data/documents' \
  < backups/documents-<stamp>.tgz
docker compose up -d
```

正式依赖备份之前，至少在一台备用服务器上完整演练一次恢复。

## 日志与监控

```bash
docker compose logs -f gateway control-api document-worker
docker compose logs --since 1h oauth2-proxy      # 排查登录问题
```

容器日志会自动轮转（每个服务 10 MB × 5 个文件）。最低限度的监控：

- 负载均衡健康检查失败时告警；
- 磁盘使用率超过 80% 时告警（上传文件和备份会持续增长）；
- 每天看一眼 `/var/log/device-backup.log` 或 `backups/` 里最新的文件。

## 安全检查清单

- [ ] 已删除 `COMPOSE_PROFILES=local-idp`，`.env` 里没有残留示例密钥。
- [ ] `.env` 权限为 `600`，且不在 git 里。
- [ ] `DEVICE_GATEWAY_COOKIE_SECURE=true`，对外地址使用 `https://`。
- [ ] 安全组只允许负载均衡访问 8000 端口。
- [ ] `DEVICE_GATEWAY_ALLOWED_CIDRS` 已收窄到负载均衡网段。
- [ ] 身份提供方中只有该用的人能登录网关客户端；每个程序有自己的客户端和密钥。
- [ ] 备份每晚执行、已复制到服务器以外，并演练过恢复。
- [ ] 部署的是 tag 或固定 commit，升级按上面的步骤进行。
