# Device AI Agent: Developer Guide

English | [简体中文](DEVELOPMENT.zh-CN.md)

Project structure, configuration, API map, and development workflow. For the
project overview, see the [README](README.md).

Everything runs in one service, the **Control API** (FastAPI + PostgreSQL),
plus a Celery worker for document processing. It covers three phases:

| Phase | Module | What it does |
| --- | --- | --- |
| 1 | `device/`, `testing/` | Device catalog, versioned profiles, capability model, command generation, device instances and connection metadata |
| 2 | `document/` | PDF/DOCX manual ingestion, OCR, AI extraction into reviewable profile candidates, admin review console, pgvector RAG |
| 3 | `diagnosis/` | Bounded, audited, read-only diagnosis agent with evaluation harness |

## Contents

- [Architecture](#architecture)
- [Quick start (Docker)](#quick-start-docker)
- [Local development](#local-development)
- [Configuration](#configuration)
- [Authentication and gateway](#authentication-and-gateway)
- [API overview](#api-overview)
- [Phase 1: device catalog and capabilities](#phase-1-device-catalog-and-capabilities)
- [Phase 2: document intelligence](#phase-2-document-intelligence)
- [Phase 3: read-only diagnosis agent](#phase-3-read-only-diagnosis-agent)
- [Testing](#testing)
- [Deployment notes](#deployment-notes)
- [Repository layout](#repository-layout)

## Architecture

```text
                 ┌──────────────────────────────┐
  HTTP clients ─▶│ control-api (FastAPI)        │──▶ OpenAI-compatible
  /api/v1        │  device · testing · document │    LLM / vision /
  /admin  ──────▶│  diagnosis · admin console   │    embedding endpoints
                 └───────┬──────────────┬───────┘
                         │              │ enqueue          ┌──────────────────┐
                         ▼              ▼                  │ read-only gateway│
                ┌──────────────┐  ┌──────────┐             │ POST /read-point │
                │ PostgreSQL 16│  │ Redis 7  │             │ (optional)       │
                │  + pgvector  │  └────┬─────┘             └────────▲─────────┘
                └──────▲───────┘       │                            │
                       │               ▼                            │
                       │     ┌───────────────────┐    diagnosis ────┘
                       └─────│ document-worker   │    tool calls
                             │ (Celery)          │
                             └───────────────────┘
          control-api and document-worker share the `document-files` volume
```

Tech stack: Python 3.12, FastAPI, SQLAlchemy 2 (async) with psycopg 3,
Alembic, Pydantic Settings, Celery + Redis, PostgreSQL 16 with pgvector,
Jinja2 (admin console), pytest + pytest-asyncio, Ruff.

## Quick start (Docker)

Requirements: Docker with Compose v2.

```bash
cp .env.example .env
# edit .env: set DEVICE_DATABASE_PASSWORD and DEVICE_ADMIN_PASSWORD (the
# identity and gateway defaults work as-is with the local Keycloak),
# plus the DEVICE_DOCUMENT_* / DEVICE_DIAGNOSIS_* provider settings you need

docker compose up --build -d
docker compose exec control-api alembic upgrade head
```

Then check:

- API health: <http://localhost:8000/health> → `{"status": "ok"}`
- Interactive API docs (Swagger UI): <http://localhost:8000/docs>
- Admin review console: <http://localhost:8000/admin/documents>

Compose starts `db` (pgvector/pgvector:pg16), `redis`, `control-api`,
`document-worker`, the `gateway` (Caddy) and `oauth2-proxy`, plus `keycloak`
with the `local-idp` profile. Only the gateway (`DEVICE_API_PORT`, default
8000), the database (`DEVICE_DATABASE_PORT`, default 5433) and the local
Keycloak (8180) are published, all on `127.0.0.1`; `control-api` itself is
reachable only through the gateway. See
[Authentication and gateway](#authentication-and-gateway).

Compose refuses to start without `DEVICE_DATABASE_PASSWORD`. Use a
URL-encoded password if it contains reserved URL characters, because it is
embedded in `DEVICE_DATABASE_URL`.

## Local development

Run the API on the host against the Compose database:

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

Settings are read from `DEVICE_*` environment variables only; the app does not
load a `.env` file by itself. `services/control_api/.env.example` lists the
host-process values to export (the root `.env.example` is for Compose).

To run the document worker on the host, you also need a Redis reachable at
`DEVICE_REDIS_URL` (Compose's Redis is not exposed to the host):

```bash
celery -A app.modules.document.celery_app:celery_app worker --loglevel=INFO
```

## Configuration

All settings use the `DEVICE_` prefix (see
[`app/core/config.py`](services/control_api/app/core/config.py)).

| Group | Variables | Notes |
| --- | --- | --- |
| Database | `DEVICE_DATABASE_URL` (host) or `DEVICE_DATABASE_NAME/USER/PASSWORD/PORT` (Compose) | Compose builds the URL itself |
| Redis | `DEVICE_REDIS_URL` | Celery broker |
| Admin console | `DEVICE_ADMIN_USERNAME`, `DEVICE_ADMIN_PASSWORD` | Console is unusable until a password is set |
| Identity (API) | `DEVICE_OIDC_ISSUER`, `DEVICE_OIDC_AUDIENCE`, `DEVICE_OIDC_JWKS_URL`, `DEVICE_OIDC_ACTOR_CLAIMS`, `DEVICE_OIDC_LEEWAY_SECONDS` | `/api/v1` answers 503 until issuer and audience are set |
| Gateway | `DEVICE_OIDC_LOGIN_URL`, `DEVICE_OIDC_TOKEN_URL`, `DEVICE_GATEWAY_CLIENT_SECRET`, `DEVICE_GATEWAY_COOKIE_SECRET`, `DEVICE_GATEWAY_COOKIE_SECURE`, `DEVICE_GATEWAY_PUBLIC_URL`, `DEVICE_GATEWAY_ALLOWED_CIDRS`, `DEVICE_GATEWAY_SITE_ADDRESS` | Compose only |
| Document storage | `DEVICE_DOCUMENT_STORAGE_ROOT`, `DEVICE_DOCUMENT_MAX_FILE_BYTES`, `DEVICE_DOCUMENT_MAX_PDF_PAGES`, `DEVICE_DOCUMENT_MIN_TEXT_CHARS_PER_PAGE`, `DEVICE_DOCUMENT_TASK_VISIBILITY_TIMEOUT_SECONDS` | Pages below the text threshold are sent to OCR |
| Extraction LLM | `DEVICE_DOCUMENT_LLM_BASE_URL/API_KEY/MODEL/TIMEOUT_SECONDS/MAX_OUTPUT_TOKENS`, `DEVICE_DOCUMENT_EXTRACTION_GROUP_MAX_TOKENS` | OpenAI-compatible |
| Vision (OCR) | `DEVICE_DOCUMENT_VISION_BASE_URL/API_KEY/MODEL/TIMEOUT_SECONDS` | OpenAI-compatible |
| Embeddings | `DEVICE_DOCUMENT_EMBEDDING_BASE_URL/API_KEY/MODEL/TIMEOUT_SECONDS` | The root `.env.example` points at a local Ollama `bge-m3` |
| Diagnosis LLM | `DEVICE_DIAGNOSIS_LLM_BASE_URL/API_KEY/MODEL/TIMEOUT_SECONDS` | Must support tool calling |
| Read gateway | `DEVICE_DIAGNOSIS_READ_GATEWAY_URL/TOKEN/TIMEOUT_SECONDS` | Optional; leave empty when no gateway exists |

Each AI provider (extraction, vision, embedding, diagnosis) has its own
endpoint, key, model, and timeout, so they can point at different vendors or
local servers.

## Authentication and gateway

Three parts, each with one job:

- **Identity provider** (OIDC: Keycloak, Feishu, Azure AD, ...) decides who
  you are. People and programs are both registered there.
- **Gateway** (`gateway`, Caddy, plus `oauth2-proxy`) is the only published
  entry point. It rejects clients outside `DEVICE_GATEWAY_ALLOWED_CIDRS`,
  signs browsers in with a session cookie, accepts provider-issued bearer
  tokens from programs, and forwards the verified token to the API. Only
  `/health` is open.
- **control-api** is not published. It verifies the forwarded JWT again
  (signature against the provider's JWKS, `iss`, `aud`, `exp`; asymmetric
  algorithms only) so a request that reaches it some other way cannot
  impersonate anyone, and records the caller (`email`, then
  `preferred_username`, `azp`, `client_id`, `sub`) as the actor on candidate
  review actions.

`/api/*` answers `401` without a valid login; other pages redirect to the
provider's sign-in. The `/admin` console still asks for its own password after
the gateway login.

### Local identity provider

`.env.example` sets `COMPOSE_PROFILES=local-idp`, which starts Keycloak on
<http://localhost:8180> (admin console `admin` / `admin`) with the realm in
[`deploy/keycloak/device-realm.json`](deploy/keycloak/device-realm.json):

| Client / user | Use |
| --- | --- |
| `device-control-api` | The gateway's browser sign-in; its id is the API audience |
| `device-cli` / `local-dev-cli-secret` | Example machine client (client credentials) |
| `reviewer` / `reviewer` | Example person |

Tokens always carry the issuer `http://localhost:8180/realms/device`; the
containers fetch keys and redeem codes through `http://keycloak:8080`, which is
why the endpoints are configured one by one.

### Using a real identity provider

1. Register a confidential OIDC client for the gateway with redirect URI
   `<DEVICE_GATEWAY_PUBLIC_URL>/oauth2/callback`. Its client id is
   `DEVICE_OIDC_AUDIENCE`; its secret is `DEVICE_GATEWAY_CLIENT_SECRET`.
2. Register each program as its own client (client credentials) and add an
   audience mapper so its tokens include `DEVICE_OIDC_AUDIENCE` in `aud` and an
   `email` claim.
3. Copy `issuer`, `authorization_endpoint`, `token_endpoint` and `jwks_uri`
   from the provider's `/.well-known/openid-configuration` into
   `DEVICE_OIDC_ISSUER`, `DEVICE_OIDC_LOGIN_URL`, `DEVICE_OIDC_TOKEN_URL` and
   `DEVICE_OIDC_JWKS_URL`.
4. Remove `COMPOSE_PROFILES=local-idp`, set a random
   `DEVICE_GATEWAY_COOKIE_SECRET` (16, 24 or 32 bytes),
   `DEVICE_GATEWAY_COOKIE_SECURE=true`, and narrow
   `DEVICE_GATEWAY_ALLOWED_CIDRS` to the load balancer or office ranges.
5. Behind a load balancer that terminates HTTPS, keep
   `DEVICE_GATEWAY_SITE_ADDRESS=:80`. Without one, set it to the domain name
   and Caddy obtains and renews the certificate itself.

## API overview

All JSON endpoints are under `/api/v1`. Full request/response schemas are in
the generated OpenAPI docs at `/docs`. Domain errors return
`{"code": "...", "message": "..."}` with an appropriate HTTP status.

| Area | Endpoints |
| --- | --- |
| Catalog | `POST /device-categories`, `POST/GET /device-models`, `POST/GET /capabilities`, `POST/GET /device-categories/{id}/capabilities[/{capability_id}]` |
| Profiles | `POST /device-models/{id}/profiles`, `GET /profiles/{id}`, `POST /profiles/{id}/protocols`, `POST /protocols/{id}/points`, `GET /profiles/{id}/points`, `POST /profiles/{id}/release` |
| Capabilities | `POST /profiles/{id}/capabilities/{capability_id}`, `POST /profile-capabilities/{id}/point-bindings`, `GET /profiles/{id}/capabilities` |
| Commands | `POST /capabilities/{id}/command-templates`, `GET /profiles/{id}/commands` |
| Testing | `POST /devices`, `GET /devices/{id}/context`, `POST /devices/{id}/connections`, `POST /connections/{id}/validate-config`, `GET /devices/{id}/test-options` |
| Documents | `POST /documents`, `GET /documents/{id}`, `POST /documents/{id}/extraction-runs`, `GET /extraction-runs/{id}`, `POST /extraction-runs/{id}/retry`, `GET /extraction-runs/{id}/candidate` |
| Candidates | `PATCH /extraction-candidates/{id}`, `POST /extraction-candidates/{id}/validate`, `.../approve`, `.../reject`, `.../import` |
| Search | `POST /document-search` |
| Diagnosis | `POST /diagnosis-runs`, `POST /diagnosis-runs/{id}/execute`, `GET /diagnosis-runs/{id}` |

`GET /health` (outside `/api/v1`) is the liveness probe.

## Phase 1: device catalog and capabilities

- **Device catalog**: device categories, models, and versioned profiles.
  Profiles move from `draft` to `released`; released configuration is
  immutable.
- **Protocols and points**: a profile carries protocols (simulator, Modbus
  TCP/RTU, BACnet/IP, MQTT, OPC UA) and their points. `address_text` is not
  unique, and one point may serve several capabilities.
- **Capability model**: a global, protocol-independent `BasicCapability`
  catalog, linked many-to-many with device categories. A `ProfileCapability`
  implements one basic capability for one profile and is bound to points
  through `CapabilityPointBinding` roles.
- **Command generation**: capability command templates reference point roles
  (`select=one` / `select=all`), never concrete point IDs. Generated command
  codes are namespaced as `<capability_code>.<template_code>`.
- **Testing metadata**: device instances, connection settings, and
  capability-driven test options. `/validate-config` validates the JSON Schema
  only and never opens sockets.

## Phase 2: document intelligence

Phase 2 turns PDF/DOCX manuals, including scanned PDFs, into traceable and
human-reviewable device profile candidates:

1. **Upload**: files pass through quarantine, size/type/structure/risk
   validation, and immutable source storage.
2. **Parse**: deterministic PDF/DOCX parsing plus scanned-page OCR produce
   stable evidence references (block and page).
3. **Extract**: the worker groups content and asks the extraction model for a
   candidate profile; sourced fields (such as model identity, capabilities,
   and point bindings) carry evidence references.
4. **Review**: candidates require schema/domain validation and explicit human
   approval.
5. **Import**: approved imports are transactional and idempotent and create
   only DRAFT profiles. AI processing never releases a profile.

RAG search (`/document-search`) uses pgvector, requires an explicit document
or device-model scope, and filters by embedding provider, model, and
dimension.

### Admin review console

Set `DEVICE_ADMIN_USERNAME` and a strong `DEVICE_ADMIN_PASSWORD`, then open
<http://localhost:8000/admin/documents>. The server-rendered console covers
the full review gate:

- filter and page the document review queue, including extraction failures;
- compare structured candidate fields with AI originals and cited evidence;
- jump from a field to its source block and PDF page;
- edit allowlisted fields with revision conflict checks and revalidation;
- approve, reject, reopen, and import only to a DRAFT profile;
- inspect the immutable reviewer/action/revision audit timeline.

Security model:

- Browser visits redirect to `/admin/login`, which issues an HttpOnly,
  `SameSite=Strict` signed session cookie with an eight-hour lifetime.
  Changing the admin password invalidates existing sessions.
- All admin forms, including login and logout, use signed CSRF tokens.
- Explicit HTTP Basic requests still work for development tools, but HTML
  browser visits require the form session, so cached Basic credentials cannot
  bypass logout.
- Admin responses send `Cache-Control: no-store` and other hardening headers.

Keep the console on localhost, a VPN, or behind a trusted internal reverse
proxy. It is not intended to be published directly to the internet.

## Phase 3: read-only diagnosis agent

Phase 3 adds a bounded model → tool → observation loop with a persistent
audit trail. The agent can:

- inspect device context and released-profile points;
- search manual evidence within the device-model scope;
- request one named point through a read-only gateway;
- decode prior raw words with the published profile.

Write tools, arbitrary addresses, SQL, files, and unrestricted HTTP are not
available to the agent.

Configure `DEVICE_DIAGNOSIS_LLM_*` for an OpenAI-compatible tool-calling
model. Set `DEVICE_DIAGNOSIS_READ_GATEWAY_URL` only when a read-only gateway
is available. The gateway contract is `POST /read-point`; the Control API
resolves the point and address from the released profile before calling it.
Supported observations are `success`, `timeout`, `connection_refused`, and
`modbus_exception`.

Typical flow:

```text
POST /api/v1/diagnosis-runs              → create a run (device_id, symptom, max_steps)
POST /api/v1/diagnosis-runs/{id}/execute → run the agent loop
GET  /api/v1/diagnosis-runs/{id}         → read steps and the final report
```

Every model action, validated argument set, tool observation, evidence
reference, and terminal report is stored in `diagnosis_run` /
`diagnosis_step`. A hard step limit, a per-run PostgreSQL advisory lock, and
per-step commits prevent duplicate execution and keep the latest completed
step after a process failure. Terminal reports must cite at least one
persisted evidence step.

### Evaluation

Versioned evaluation cases live in
[`services/control_api/evals/diagnosis/cases.json`](services/control_api/evals/diagnosis/cases.json).
Run repeated live-model evaluation from `services/control_api`:

```bash
python scripts/run_diagnosis_evals.py --trials 3
```

The report includes classification accuracy, handoff/escalation rate,
dangerous-action rate, total tokens, average latency, and average step count.
The dataset pins the system prompt SHA-256, so prompt changes require an
explicit baseline review.

## Testing

Tests need a PostgreSQL database whose name ends in `_test`; pytest refuses to
truncate any other database. Tests use deterministic fakes and make no live
provider calls.

One-time setup with the Compose database:

```bash
docker compose up -d db
docker compose exec db createdb -U device device_ai_agent_test
```

Then, from `services/control_api` (bash):

```bash
export DEVICE_DATABASE_URL="postgresql+psycopg://device:<password>@localhost:5433/device_ai_agent_test"
alembic upgrade head

ruff check .
pytest -v
alembic current
alembic check
```

PowerShell equivalent:

```powershell
$env:DEVICE_DATABASE_PASSWORD = "replace-with-a-local-secret"
$env:DEVICE_DATABASE_URL = "postgresql+psycopg://device:$($env:DEVICE_DATABASE_PASSWORD)@localhost:5433/device_ai_agent_test"
cd services/control_api
alembic upgrade head
pytest -v
alembic current
alembic check
```

`createdb` reports that the database already exists on later runs; that is
expected.

## Deployment notes

For a step-by-step production setup (load balancer, firewall, upgrades,
backups) see [DEPLOYMENT.md](DEPLOYMENT.md).

The Compose file is set up for local and internal use. Before exposing the
service anywhere else:

- set strong, unique values for `DEVICE_DATABASE_PASSWORD` and
  `DEVICE_ADMIN_PASSWORD`, and keep provider API keys out of version control;
- run `alembic upgrade head` on every release before starting new API or
  worker containers;
- follow [Using a real identity provider](#using-a-real-identity-provider)
  and terminate TLS at the load balancer or let Caddy do it;
- persist the `document-files` volume and the PostgreSQL data together; source
  documents are stored on the volume, metadata in the database.

## Repository layout

```text
.
├── docker-compose.yml          # db, redis, control-api, document-worker
├── .env.example                # Compose settings
└── services/control_api/
    ├── app/
    │   ├── core/               # settings, domain errors
    │   ├── db/                 # SQLAlchemy base and async session
    │   └── modules/
    │       ├── device/         # catalog, profiles, capabilities, commands
    │       ├── testing/        # device instances, connections, test options
    │       ├── document/       # ingestion, extraction, review console, RAG
    │       └── diagnosis/      # read-only agent, tools, gateway, evals
    ├── alembic/                # database migrations
    ├── evals/diagnosis/        # versioned diagnosis eval cases
    ├── scripts/                # run_diagnosis_evals.py
    ├── tests/
    ├── Dockerfile
    └── pyproject.toml
```

## License

See [LICENSE](LICENSE).
