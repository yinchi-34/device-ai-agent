# End-to-end diagnosis runs

Real model, real HTTP read gateway, simulated Modbus TCP devices. Nothing in the
agent loop is stubbed: faults come from protocol behaviour (refused port,
Modbus exception 2, silent socket, register contents).

```bash
pip install -e '.[dev,e2e]'
createdb device_ai_agent_e2e && psql -d device_ai_agent_e2e -c 'create extension vector'
DEVICE_DATABASE_URL=postgresql+psycopg://...@localhost/device_ai_agent_e2e alembic upgrade head

python -m e2e.sim_gateway e2e/scenarios.json --gateway-port 8090 &
DEVICE_DATABASE_URL=postgresql+psycopg://...@localhost/device_ai_agent_e2e \
DEVICE_DIAGNOSIS_LLM_BASE_URL=... DEVICE_DIAGNOSIS_LLM_API_KEY=... DEVICE_DIAGNOSIS_LLM_MODEL=... \
DEVICE_DIAGNOSIS_READ_GATEWAY_URL=http://127.0.0.1:8090 \
python -m e2e.run_e2e e2e/scenarios.json --trials 3
```

The runner only resets databases whose name ends in `_e2e`. Each run writes
`e2e/runs/<timestamp>/` with `meta.json`, `runs.jsonl` (every step's tool call,
observation, rationale and provider `usage`) and `summary.csv`.

`gold` in `scenarios.json` is written by a person. A case passes when status,
classification, cited evidence tool and "no write tool" all match; cases
without gold are recorded as `no_gold` and not scored.
