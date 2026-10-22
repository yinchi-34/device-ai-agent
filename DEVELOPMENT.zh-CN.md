# Device AI Agent 开发文档

[English](DEVELOPMENT.md) | 简体中文

项目结构、配置、API 一览和开发流程。项目介绍见 [README](README.zh-CN.md)。

所有功能运行在一个服务 **Control API**（FastAPI + PostgreSQL）中，另有一个
Celery worker 负责文档处理。项目分为三个阶段：

| 阶段 | 模块 | 功能 |
| --- | --- | --- |
| 1 | `device/`、`testing/` | 设备目录、版本化 Profile、能力模型、命令生成、设备实例与连接元数据 |
| 2 | `document/` | PDF/DOCX 说明书导入、OCR、AI 抽取生成可审核的 Profile 候选、管理审核台、pgvector RAG |
| 3 | `diagnosis/` | 有步数上限、全程审计的只读诊断 Agent，附带评测工具 |

## 目录

- [架构](#架构)
- [快速开始（Docker）](#快速开始docker)
- [本地开发](#本地开发)
- [配置](#配置)
- [认证与网关](#认证与网关)
- [API 概览](#api-概览)
- [阶段 1：设备目录与能力模型](#阶段-1设备目录与能力模型)
- [阶段 2：文档智能](#阶段-2文档智能)
- [阶段 3：只读诊断 Agent](#阶段-3只读诊断-agent)
- [测试](#测试)
- [部署注意事项](#部署注意事项)
- [仓库结构](#仓库结构)

## 架构

```text
                 ┌──────────────────────────────┐
  HTTP 客户端 ──▶│ control-api (FastAPI)        │──▶ OpenAI 兼容的
  /api/v1        │  device · testing · document │    LLM / 视觉 /
  /admin  ──────▶│  diagnosis · 管理审核台      │    Embedding 接口
                 └───────┬──────────────┬───────┘
                         │              │ 投递任务         ┌──────────────────┐
                         ▼              ▼                  │ 只读网关         │
                ┌──────────────┐  ┌──────────┐             │ POST /read-point │
                │ PostgreSQL 16│  │ Redis 7  │             │ （可选）         │
                │  + pgvector  │  └────┬─────┘             └────────▲─────────┘
                └──────▲───────┘       │                            │
                       │               ▼                            │
                       │     ┌───────────────────┐    诊断工具 ─────┘
                       └─────│ document-worker   │    调用
                             │ (Celery)          │
                             └───────────────────┘
          control-api 与 document-worker 共享 `document-files` 卷
```

技术栈：Python 3.12、FastAPI、SQLAlchemy 2（async，psycopg 3）、Alembic、
Pydantic Settings、Celery + Redis、PostgreSQL 16 + pgvector、Jinja2（管理审核台）、
pytest + pytest-asyncio、Ruff。

## 快速开始（Docker）

环境要求：Docker 及 Compose v2。

```bash
cp .env.example .env
# 编辑 .env：设置 DEVICE_DATABASE_PASSWORD 和 DEVICE_ADMIN_PASSWORD（身份和网关的
# 默认值配合本地 Keycloak 可以直接使用），
# 以及需要用到的 DEVICE_DOCUMENT_* / DEVICE_DIAGNOSIS_* 模型服务配置

docker compose up --build -d
docker compose exec control-api alembic upgrade head
```

启动后检查：

- 健康检查：<http://localhost:8000/health> → `{"status": "ok"}`
- 交互式 API 文档（Swagger UI）：<http://localhost:8000/docs>
- 管理审核台：<http://localhost:8000/admin/documents>

Compose 会启动 `db`（pgvector/pgvector:pg16）、`redis`、`control-api`、
`document-worker`、网关 `gateway`（Caddy）和 `oauth2-proxy`，启用 `local-idp`
profile 时还会启动 `keycloak`。对宿主机只开放网关（`DEVICE_API_PORT`，默认 8000）、
数据库（`DEVICE_DATABASE_PORT`，默认 5433）和本地 Keycloak（8180），且都只绑定在
`127.0.0.1`；`control-api` 本身只能经由网关访问。详见[认证与网关](#认证与网关)。

未设置 `DEVICE_DATABASE_PASSWORD` 时 Compose 会拒绝启动。密码会被拼进
`DEVICE_DATABASE_URL`，所以如果其中含有 URL 保留字符，需要先做 URL 编码。

## 本地开发

在宿主机上运行 API，连接 Compose 启动的数据库：

```bash
docker compose up -d db

cd services/control_api
python3.12 -m venv .venv
source .venv/bin/activate            # Windows: .\.venv\Scripts\Activate.ps1
pip install -e ".[dev]"

export DEVICE_DATABASE_URL="postgresql+psycopg://device:<password>@localhost:5433/device_ai_agent"
alembic upgrade head
uvicorn app.main:app --reload
```

应用只从 `DEVICE_*` 环境变量读取配置，不会自动加载 `.env` 文件。
`services/control_api/.env.example` 列出了宿主机进程需要导出的变量（根目录的
`.env.example` 是给 Compose 用的）。

如果要在宿主机上运行文档 worker，还需要一个 `DEVICE_REDIS_URL` 能访问到的 Redis
（Compose 里的 Redis 没有暴露到宿主机）：

```bash
celery -A app.modules.document.celery_app:celery_app worker --loglevel=INFO
```

## 配置

所有配置项都使用 `DEVICE_` 前缀（见
[`app/core/config.py`](services/control_api/app/core/config.py)）。

| 分组 | 变量 | 说明 |
| --- | --- | --- |
| 数据库 | `DEVICE_DATABASE_URL`（宿主机）或 `DEVICE_DATABASE_NAME/USER/PASSWORD/PORT`（Compose） | Compose 会自行拼接 URL |
| Redis | `DEVICE_REDIS_URL` | Celery broker |
| 管理审核台 | `DEVICE_ADMIN_USERNAME`、`DEVICE_ADMIN_PASSWORD` | 未设置密码时审核台不可用 |
| 身份（API） | `DEVICE_OIDC_ISSUER`、`DEVICE_OIDC_AUDIENCE`、`DEVICE_OIDC_JWKS_URL`、`DEVICE_OIDC_ACTOR_CLAIMS`、`DEVICE_OIDC_LEEWAY_SECONDS` | 未设置 issuer 和 audience 时 `/api/v1` 返回 503 |
| 网关 | `DEVICE_OIDC_LOGIN_URL`、`DEVICE_OIDC_TOKEN_URL`、`DEVICE_GATEWAY_CLIENT_SECRET`、`DEVICE_GATEWAY_COOKIE_SECRET`、`DEVICE_GATEWAY_COOKIE_SECURE`、`DEVICE_GATEWAY_PUBLIC_URL`、`DEVICE_GATEWAY_ALLOWED_CIDRS`、`DEVICE_GATEWAY_SITE_ADDRESS` | 仅 Compose 使用 |
| 文档存储 | `DEVICE_DOCUMENT_STORAGE_ROOT`、`DEVICE_DOCUMENT_MAX_FILE_BYTES`、`DEVICE_DOCUMENT_MAX_PDF_PAGES`、`DEVICE_DOCUMENT_MIN_TEXT_CHARS_PER_PAGE`、`DEVICE_DOCUMENT_TASK_VISIBILITY_TIMEOUT_SECONDS` | 文本量低于阈值的页面会走 OCR |
| 抽取 LLM | `DEVICE_DOCUMENT_LLM_BASE_URL/API_KEY/MODEL/TIMEOUT_SECONDS/MAX_OUTPUT_TOKENS`、`DEVICE_DOCUMENT_EXTRACTION_GROUP_MAX_TOKENS` | OpenAI 兼容接口 |
| 视觉（OCR） | `DEVICE_DOCUMENT_VISION_BASE_URL/API_KEY/MODEL/TIMEOUT_SECONDS` | OpenAI 兼容接口 |
| Embedding | `DEVICE_DOCUMENT_EMBEDDING_BASE_URL/API_KEY/MODEL/TIMEOUT_SECONDS` | 根目录 `.env.example` 默认指向本地 Ollama 的 `bge-m3` |
| 诊断 LLM | `DEVICE_DIAGNOSIS_LLM_BASE_URL/API_KEY/MODEL/TIMEOUT_SECONDS` | 必须支持工具调用 |
| 只读网关 | `DEVICE_DIAGNOSIS_READ_GATEWAY_URL/TOKEN/TIMEOUT_SECONDS` | 可选；没有网关时留空 |

每个 AI 服务（抽取、视觉、Embedding、诊断）都有独立的地址、密钥、模型和超时，
可以分别指向不同厂商或本地服务。

## 认证与网关

三个部分各管一件事：

- **身份提供方**（OIDC：Keycloak、飞书、Azure AD 等）负责"你是谁"，人和程序都在这里登记。
- **网关**（`gateway`，即 Caddy，加上 `oauth2-proxy`）是唯一对外的入口。它拒绝
  `DEVICE_GATEWAY_ALLOWED_CIDRS` 以外的来源，用会话 cookie 让浏览器登录，接受程序
  带来的身份提供方 token，并把校验过的 token 转给 API。只有 `/health` 不需要登录。
- **control-api** 不对外暴露端口。它会再校验一次转发来的 JWT（用身份提供方的 JWKS
  验签，检查 `iss`、`aud`、`exp`，只接受非对称算法），即使有请求绕过网关也无法冒充
  身份；并把调用方（依次取 `email`、`preferred_username`、`azp`、`client_id`、
  `sub`）记为候选审核操作的操作人。

没有有效登录时，`/api/*` 返回 `401`，其他页面跳转到身份提供方登录。`/admin`
审核台在网关登录之后仍然要输入它自己的密码。

### 本地身份提供方

`.env.example` 里设置了 `COMPOSE_PROFILES=local-idp`，会在 <http://localhost:8180>
启动 Keycloak（管理后台 `admin` / `admin`），导入
[`deploy/keycloak/device-realm.json`](deploy/keycloak/device-realm.json) 中的 realm：

| 客户端 / 用户 | 用途 |
| --- | --- |
| `device-control-api` | 网关的浏览器登录；它的 id 就是 API 的 audience |
| `device-cli` / `local-dev-cli-secret` | 示例程序客户端（client credentials） |
| `reviewer` / `reviewer` | 示例用户 |

token 的 issuer 固定是 `http://localhost:8180/realms/device`；容器通过
`http://keycloak:8080` 拉取公钥和换取 token，所以各个端点要分别配置。

### 接入正式的身份提供方

1. 为网关登记一个机密（confidential）OIDC 客户端，回调地址为
   `<DEVICE_GATEWAY_PUBLIC_URL>/oauth2/callback`。客户端 id 填到
   `DEVICE_OIDC_AUDIENCE`，密钥填到 `DEVICE_GATEWAY_CLIENT_SECRET`。
2. 每个程序单独登记一个客户端（client credentials），并加 audience mapper，让它的
   token 的 `aud` 包含 `DEVICE_OIDC_AUDIENCE`，同时带上 `email` claim。
3. 从身份提供方的 `/.well-known/openid-configuration` 中，把 `issuer`、
   `authorization_endpoint`、`token_endpoint`、`jwks_uri` 分别填到
   `DEVICE_OIDC_ISSUER`、`DEVICE_OIDC_LOGIN_URL`、`DEVICE_OIDC_TOKEN_URL`、
   `DEVICE_OIDC_JWKS_URL`。
4. 去掉 `COMPOSE_PROFILES=local-idp`，把 `DEVICE_GATEWAY_COOKIE_SECRET` 设为随机值
   （16、24 或 32 字节），设置 `DEVICE_GATEWAY_COOKIE_SECURE=true`，并把
   `DEVICE_GATEWAY_ALLOWED_CIDRS` 收窄到负载均衡或办公网段。
5. 如果前面有负载均衡终止 HTTPS，保持 `DEVICE_GATEWAY_SITE_ADDRESS=:80`；没有的话
   把它设成域名，Caddy 会自动申请和续期证书。

## API 概览

所有 JSON 接口都在 `/api/v1` 下。完整的请求/响应结构见 `/docs` 自动生成的
OpenAPI 文档。领域错误返回 `{"code": "...", "message": "..."}` 及相应的 HTTP
状态码。

| 类别 | 接口 |
| --- | --- |
| 目录 | `POST /device-categories`、`POST/GET /device-models`、`POST/GET /capabilities`、`POST/GET /device-categories/{id}/capabilities[/{capability_id}]` |
| Profile | `POST /device-models/{id}/profiles`、`GET /profiles/{id}`、`POST /profiles/{id}/protocols`、`POST /protocols/{id}/points`、`GET /profiles/{id}/points`、`POST /profiles/{id}/release` |
| 能力 | `POST /profiles/{id}/capabilities/{capability_id}`、`POST /profile-capabilities/{id}/point-bindings`、`GET /profiles/{id}/capabilities` |
| 命令 | `POST /capabilities/{id}/command-templates`、`GET /profiles/{id}/commands` |
| 测试 | `POST /devices`、`GET /devices/{id}/context`、`POST /devices/{id}/connections`、`POST /connections/{id}/validate-config`、`GET /devices/{id}/test-options` |
| 文档 | `POST /documents`、`GET /documents/{id}`、`POST /documents/{id}/extraction-runs`、`GET /extraction-runs/{id}`、`POST /extraction-runs/{id}/retry`、`GET /extraction-runs/{id}/candidate` |
| 候选 | `PATCH /extraction-candidates/{id}`、`POST /extraction-candidates/{id}/validate`、`.../approve`、`.../reject`、`.../import` |
| 检索 | `POST /document-search` |
| 诊断 | `POST /diagnosis-runs`、`POST /diagnosis-runs/{id}/execute`、`GET /diagnosis-runs/{id}` |

`GET /health`（不在 `/api/v1` 下）是存活探针。

## 阶段 1：设备目录与能力模型

- **设备目录**：设备类别、型号和版本化 Profile。Profile 从 `draft` 变为
  `released`，发布后的配置不可修改。
- **协议与点位**：Profile 包含协议（模拟器、Modbus TCP/RTU、BACnet/IP、MQTT、
  OPC UA）及其点位。`address_text` 不要求唯一，一个点位可以服务多个能力。
- **能力模型**：全局的、与协议无关的 `BasicCapability` 目录，与设备类别是多对多
  关系。`ProfileCapability` 为某个 Profile 实现一个基础能力，并通过
  `CapabilityPointBinding` 的角色绑定到点位。
- **命令生成**：能力命令模板按角色引用点位（`select=one` / `select=all`），
  从不引用具体的点位 ID。生成的命令编码带命名空间：
  `<capability_code>.<template_code>`。
- **测试元数据**：设备实例、连接配置和基于能力的测试选项。`/validate-config`
  只做 JSON Schema 校验，不会建立任何网络连接。

## 阶段 2：文档智能

阶段 2 把 PDF/DOCX 说明书（包括扫描版 PDF）转换为可追溯、可人工审核的设备
Profile 候选：

1. **上传**：文件先进入隔离区，经过大小、类型、结构和风险校验，再写入不可变的
   源文件存储。
2. **解析**：确定性的 PDF/DOCX 解析加上扫描页 OCR，生成稳定的证据引用（文本块和
   页码）。
3. **抽取**：worker 对内容分组后交给抽取模型生成 Profile 候选；有来源的字段
   （如型号信息、能力和点位绑定）会附带证据引用。
4. **审核**：候选必须通过 Schema/领域校验，并由人工明确批准。
5. **导入**：批准后的导入是事务性且幂等的，只会创建 DRAFT 状态的 Profile。
   AI 处理永远不会发布 Profile。

RAG 检索（`/document-search`）基于 pgvector，必须显式指定文档或设备型号范围，
并按 Embedding 服务、模型和维度过滤。

### 管理审核台

设置 `DEVICE_ADMIN_USERNAME` 和足够强的 `DEVICE_ADMIN_PASSWORD`，然后打开
<http://localhost:8000/admin/documents>。这个服务端渲染的审核台覆盖完整的审核流程：

- 筛选、分页查看文档审核队列，包括抽取失败的文档；
- 对照结构化的候选字段、AI 原始输出和引用的证据；
- 从字段跳转到对应的源文本块和 PDF 页面；
- 编辑白名单内的字段，带版本冲突检查和重新校验；
- 批准、驳回、重新打开，以及导入（只会导入为 DRAFT Profile）；
- 查看不可变的审核人/操作/版本审计时间线。

安全机制：

- 浏览器访问会重定向到 `/admin/login`，登录后签发 HttpOnly、`SameSite=Strict`
  的签名会话 Cookie，有效期 8 小时。修改管理员密码会使已有会话失效。
- 所有管理表单（包括登录和登出）都使用签名 CSRF Token。
- 开发工具仍可显式使用 HTTP Basic 认证，但浏览器的 HTML 访问必须使用表单会话，
  因此浏览器缓存的 Basic 凭据无法绕过登出。
- 管理页面响应会带上 `Cache-Control: no-store` 等安全响应头。

审核台请只在 localhost、VPN 或可信的内网反向代理后使用，不要直接暴露到公网。

## 阶段 3：只读诊断 Agent

阶段 3 增加了一个有边界的“模型 → 工具 → 观测”循环，并持久化完整审计记录。
Agent 可以：

- 查看设备上下文和已发布 Profile 的点位；
- 在设备型号范围内检索说明书证据；
- 通过只读网关读取一个指定点位；
- 用已发布的 Profile 解码之前读到的原始寄存器值。

Agent 无法使用写操作、任意地址、SQL、文件或不受限的 HTTP。

`DEVICE_DIAGNOSIS_LLM_*` 需配置为支持工具调用的 OpenAI 兼容模型。只有在确实有
只读网关时才设置 `DEVICE_DIAGNOSIS_READ_GATEWAY_URL`。网关协议是
`POST /read-point`；Control API 会先根据已发布的 Profile 解析出点位和地址，再调用
网关。支持的观测结果有 `success`、`timeout`、`connection_refused` 和
`modbus_exception`。

典型流程：

```text
POST /api/v1/diagnosis-runs              → 创建诊断任务（device_id、symptom、max_steps）
POST /api/v1/diagnosis-runs/{id}/execute → 执行 Agent 循环
GET  /api/v1/diagnosis-runs/{id}         → 查看步骤和最终报告
```

每一次模型动作、校验后的参数、工具观测、证据引用和最终报告都保存在
`diagnosis_run` / `diagnosis_step` 中。硬性步数上限、每个任务的 PostgreSQL
advisory lock 和逐步提交，可以防止重复执行，并在进程故障后保留最近完成的步骤。
最终报告必须引用至少一个已持久化的证据步骤。

### 评测

三套离线评测覆盖依赖模型的部分：诊断 Agent 端到端、档案候选抽取、`search_manual` 检索。
数据集在 [`services/control_api/evals/`](services/control_api/evals/README.zh-CN.md)，
每次运行写一份 JSON 报告，指标未达门槛时以非零退出码结束。在 `services/control_api` 下：

```bash
python scripts/run_diagnosis_evals.py --trials 3
python scripts/run_document_evals.py
```

诊断评测报告通过率、pass^k、分类准确率、危险操作率和策略违规率、证据引用、冗余调用、
Token 和 p95 延迟，并按标签拆分；抽取评测报告点位精确率/召回率/F1、字段与证据准确率和陷阱误抽；
检索评测按语言和查询类型报告 Hit@1、Recall@5 和 MRR。诊断数据集固定了系统 Prompt 的
SHA-256，修改 Prompt 需要显式重新审定基线。`Evals` 工作流可按需运行，详见
[评测说明](services/control_api/evals/README.zh-CN.md)。

## 测试

测试需要一个名称以 `_test` 结尾的 PostgreSQL 数据库；pytest 会拒绝清空其他任何
数据库。测试使用确定性的假实现，不会调用真实的模型服务。

用 Compose 数据库做一次性准备：

```bash
docker compose up -d db
docker compose exec db createdb -U device device_ai_agent_test
```

然后在 `services/control_api` 目录下运行（bash）：

```bash
export DEVICE_DATABASE_URL="postgresql+psycopg://device:<password>@localhost:5433/device_ai_agent_test"
alembic upgrade head

ruff check .
pytest -v
alembic current
alembic check
```

PowerShell 写法：

```powershell
$env:DEVICE_DATABASE_PASSWORD = "replace-with-a-local-secret"
$env:DEVICE_DATABASE_URL = "postgresql+psycopg://device:$($env:DEVICE_DATABASE_PASSWORD)@localhost:5433/device_ai_agent_test"
cd services/control_api
alembic upgrade head
pytest -v
alembic current
alembic check
```

之后再运行 `createdb` 会提示数据库已存在，这是正常的。

## 部署注意事项

完整的生产部署步骤（负载均衡、防火墙、升级、备份）见 [DEPLOYMENT.zh-CN.md](DEPLOYMENT.zh-CN.md)。

当前的 Compose 配置面向本地和内网使用。暴露到其他环境之前：

- 为 `DEVICE_DATABASE_PASSWORD` 和 `DEVICE_ADMIN_PASSWORD` 设置强且唯一的值，
  模型服务的 API Key 不要提交到版本库；
- 每次发布时，先运行 `alembic upgrade head`，再启动新的 API 和 worker 容器；
- 按[接入正式的身份提供方](#接入正式的身份提供方)配置，并在负载均衡上终止 TLS，或交给 Caddy；
- `document-files` 卷和 PostgreSQL 数据要一起持久化和备份：源文档存在卷里，
  元数据存在数据库里。

## 仓库结构

```text
.
├── docker-compose.yml          # db、redis、control-api、document-worker
├── .env.example                # Compose 配置
└── services/control_api/
    ├── app/
    │   ├── core/               # 配置、领域错误
    │   ├── db/                 # SQLAlchemy base 和异步 session
    │   └── modules/
    │       ├── device/         # 目录、Profile、能力、命令
    │       ├── testing/        # 设备实例、连接、测试选项
    │       ├── document/       # 导入、抽取、审核台、RAG
    │       └── diagnosis/      # 只读 Agent、工具、网关、评测
    ├── alembic/                # 数据库迁移
    ├── evals/                  # 版本化评测数据集和门槛
    ├── scripts/                # run_diagnosis_evals.py、run_document_evals.py
    ├── tests/
    ├── Dockerfile
    └── pyproject.toml
```

## 许可证

见 [LICENSE](LICENSE)。
