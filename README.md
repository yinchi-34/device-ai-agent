# Device AI Agent

A backend platform for managing industrial device definitions, capabilities,
and connectivity testing — the foundation for AI-assisted device diagnosis.

The current phase ships the **Control API**: a FastAPI service that models
device categories, models, versioned profiles, protocols, points, and a
reusable capability catalog, and that generates protocol commands from
capability definitions.

## Features

- **Device catalog** — device categories, models, and versioned profiles
  (draft → released, released configuration is immutable).
- **Capability model** — a global, protocol-independent `BasicCapability`
  catalog linked many-to-many with device categories; concrete
  `ProfileCapability` implementations bound semantically to device points.
- **Command generation** — capability command templates reference points by
  role (`select=one` / `select=all`); generated command codes are namespaced
  as `<capability_code>.<template_code>`.
- **Testing metadata** — device instances, connection settings with
  schema-only validation (`/validate-config` never opens sockets), and
  capability-driven test options.

## Tech stack

- Python 3.12, FastAPI, SQLAlchemy 2 (async), Alembic, Pydantic Settings
- PostgreSQL 16
- pytest + pytest-asyncio, Ruff

## Quick start

### With Docker

```bash
docker compose up --build -d
docker compose exec control-api alembic upgrade head
```


### Local development

Requirements: Python 3.12 and a PostgreSQL instance (or
`docker compose up db`).


## Development

```bash
# lint
ruff check services/control_api

# tests (requires a reachable PostgreSQL; see docker compose)
cd services/control_api && pytest -v

# check migration state
cd services/control_api && alembic current
```

## License

See [LICENSE](LICENSE).

## Phase 2 document intelligence

Phase 2 turns PDF/DOCX manuals, including scanned PDFs, into traceable and
human-reviewable device profile candidates:

- uploads pass through quarantine, size/type/structure/risk validation, and
  immutable source storage;
- deterministic PDF/DOCX parsing and scanned-page OCR produce stable evidence
  references;
- extraction, vision, and embedding providers have independent endpoints,
  credentials, models, and timeouts;
- candidates require schema/domain validation and explicit human approval;
- approved imports are transactional and idempotent and create only DRAFT
  profiles; AI processing never releases a profile;
- pgvector RAG requires an explicit document or device-model scope and filters
  by embedding provider, model, and dimension.

Docker Compose runs PostgreSQL 16 with pgvector, Redis 7, the API, and a Celery
document worker. The API and worker share the `document-files` volume. The
database is exposed on host port 5433 for local verification.

```powershell
$env:DEVICE_DATABASE_PASSWORD = "replace-with-a-local-secret"
docker compose up --build -d
docker compose exec control-api alembic upgrade head
$env:DEVICE_DATABASE_URL = "postgresql+psycopg://device:$($env:DEVICE_DATABASE_PASSWORD)@localhost:5433/device_ai_agent"
cd services/control_api
pytest -v
alembic current
alembic check
```

Use a URL-encoded database password if it contains reserved URL characters.
Copy `services/control_api/.env.example` before configuring providers. Docker
Compose binds the API and database ports to `127.0.0.1` by default. Tests use
deterministic fakes and do not make live provider calls.
