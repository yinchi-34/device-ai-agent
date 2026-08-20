# Codex Instructions

## Branch behavior

The Codex execution environment may check out the repository on a harness-managed branch such as `work`.
Do not block implementation only because the local branch is not named `main`.
Work on the branch provided by the Codex environment unless the environment itself provides a safe way to switch.
The user's intent is that no separate feature branch is required.

## Execution

- Implement Phase 1 task-by-task in plan order.
- Use TDD: write a failing test, verify RED, implement minimally, verify GREEN.
- Commit after each completed task when commits are supported by the environment.
- Do not open a PR unless explicitly requested.
- Do not add Go Gateway, Agent, RAG, Celery, Redis, MinIO, vector DB, or real protocol I/O in Phase 1.

## Domain boundaries

- `device/`: device definition, profiles, protocols, points, capabilities, bindings, generated commands.
- `document/`: Phase 2 only.
- `testing/`: device instances, connection metadata, test selection.
- `diagnosis/`: Phase 3 only.

## Core invariants

- `BasicCapability` is global and protocol-independent.
- `DeviceCategory <-> BasicCapability` is many-to-many.
- `ProfileCapability` implements one BasicCapability for one Profile.
- `CapabilityPointBinding` is the semantic many-to-many link between ProfileCapability and DevicePoint.
- One DevicePoint may serve multiple capabilities.
- `address_text` is not unique.
- `CapabilityCommandTemplate` references roles, not concrete Point IDs.
- Role selection supports `select=one` and `select=all`.
- Generated command codes are namespaced as `<capability_code>.<template_code>`.
- Released Profile configuration is immutable.
- `/validate-config` validates JSON Schema only and must not open sockets.

## Verification

Before claiming Phase 1 complete, run fresh:

```bash
ruff check .
pytest -v
alembic current
```
