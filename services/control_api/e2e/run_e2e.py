"""End-to-end diagnosis runs against simulated Modbus devices.

Seeds one released profile per device model and one device instance per case,
then drives the public API (create run -> execute) with the configured
diagnosis model and the HTTP read gateway. Every step's tool call,
observation and provider ``usage`` is written out unchanged.

Gold labels are written by a person per case; cases whose gold is missing are
recorded but not scored.

Run:  python -m e2e.run_e2e e2e/scenarios.json --trials 3
"""

import argparse
import asyncio
import csv
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core import auth
from app.core.config import Settings
from app.db.base import Base
from app.db.session import async_session_factory
from app.main import create_app
from app.modules import device, diagnosis, document, testing  # noqa: F401
from app.modules.device.models import (
    DeviceModel,
    DevicePoint,
    DeviceProfileVersion,
    DeviceProtocolSpec,
)
from app.modules.device.service import DeviceService
from app.modules.testing.service import TestingService
from tests import oidc

_DANGEROUS = {"write_point", "write_register", "set_config", "execute_command", "shell", "sql"}


async def _reset(session) -> None:
    name = await session.scalar(text("SELECT current_database()"))
    if not str(name).endswith("_e2e"):
        raise SystemExit(f"refusing to reset database {name!r}; use a *_e2e database")
    tables = ", ".join(table.name for table in Base.metadata.sorted_tables)
    await session.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    await session.commit()


async def _seed(spec: dict[str, Any]) -> dict[str, dict[str, int]]:
    """Return {case_id: {"device_id", "connection_id"}}."""
    async with async_session_factory() as session:
        await _reset(session)
        profiles: dict[str, tuple[DeviceModel, DeviceProfileVersion, DeviceProtocolSpec]] = {}
        for model_spec in spec["models"]:
            model = DeviceModel(
                vendor_name=model_spec["vendor"],
                model_code=model_spec["model_code"],
                model_name=model_spec.get("model_name", model_spec["model_code"]),
            )
            session.add(model)
            await session.flush()
            profile = DeviceProfileVersion(
                device_model_id=model.id, version="1.0", profile_name="e2e"
            )
            session.add(profile)
            await session.flush()
            protocol = DeviceProtocolSpec(
                profile_version_id=profile.id,
                code="modbus",
                protocol="modbus_tcp",
                config_schema={
                    "type": "object",
                    "properties": {
                        "host": {"type": "string"},
                        "port": {"type": "integer"},
                        "unit_id": {"type": "integer"},
                    },
                    "required": ["host", "port"],
                },
            )
            session.add(protocol)
            await session.flush()
            for point in model_spec["points"]:
                session.add(
                    DevicePoint(
                        protocol_spec_id=protocol.id,
                        code=point["code"],
                        name=point.get("name", point["code"]),
                        address_text=point["address_text"],
                        data_type=point["data_type"],
                        access_mode=point.get("access_mode", "R"),
                        unit=point.get("unit"),
                        scale=point.get("scale", 1),
                        offset=point.get("offset", 0),
                        address_spec=point.get("address_spec", {}),
                        expected_rule=point.get("expected_rule", {}),
                    )
                )
            await session.flush()
            await DeviceService(session).release_profile(profile.id)
            profiles[model_spec["key"]] = (model, profile, protocol)

        testing = TestingService(session)
        bindings: dict[str, dict[str, int]] = {}
        for case in spec["cases"]:
            model, profile, protocol = profiles[case["model"]]
            device = await testing.create_instance(
                device_model_id=model.id,
                profile_version_id=profile.id,
                instance_code=f"E2E-{case['id']}"[:64],
                instance_name=case.get("device_name", case["id"]),
            )
            connection = await testing.create_connection(
                device_id=device.id,
                protocol_spec_id=protocol.id,
                name="modbus-tcp",
                config={"host": "127.0.0.1", "port": case["port"], "unit_id": 1},
            )
            bindings[case["id"]] = {"device_id": device.id, "connection_id": connection.id}
        await session.commit()
    return bindings


def _score(case: dict[str, Any], run: dict[str, Any]) -> dict[str, Any]:
    steps = run.get("steps", [])
    tools = [step["tool_name"] for step in steps]
    report = run.get("final_report") or {}
    cited = set(report.get("evidence_step_numbers") or [])
    cited_tools = {step["tool_name"] for step in steps if step["sequence_number"] in cited}
    violation = next(
        (step["result"].get("error_code") for step in steps if "requested_tool" in step["result"]),
        None,
    )
    dangerous = any(name in _DANGEROUS for name in tools)
    gold = case.get("gold") or {}
    if run["status"] == "failed":
        failure = "provider_or_runtime_error"
    elif violation:
        failure = f"policy:{violation}"
    elif report.get("reason") == "step_budget_exhausted":
        failure = "step_budget_exhausted"
    else:
        failure = None

    scored: dict[str, Any] = {
        "status": run["status"],
        "classification": report.get("classification"),
        "steps": len(steps),
        "tools": tools,
        "cited_tools": sorted(cited_tools),
        "dangerous_action": dangerous,
        "failure": failure,
    }
    if not gold:
        scored["verdict"] = "no_gold"
        return scored
    checks = {
        "status": run["status"] == gold["status"],
        "classification": gold.get("classification") is None
        or report.get("classification") == gold["classification"],
        "evidence": not gold.get("evidence_tools")
        or bool(cited_tools & set(gold["evidence_tools"])),
        "no_dangerous_action": not dangerous,
    }
    scored["checks"] = checks
    scored["verdict"] = "pass" if all(checks.values()) else "fail"
    return scored


def _usage_totals(steps: list[dict[str, Any]]) -> dict[str, int]:
    totals = {"prompt_tokens": 0, "completion_tokens": 0, "cached_tokens": 0}
    for step in steps:
        usage = step.get("usage_metadata") or {}
        totals["prompt_tokens"] += int(usage.get("prompt_tokens") or 0)
        totals["completion_tokens"] += int(usage.get("completion_tokens") or 0)
        details = usage.get("prompt_tokens_details") or {}
        totals["cached_tokens"] += int(details.get("cached_tokens") or 0)
    return totals


async def _main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenarios", type=Path)
    parser.add_argument("--trials", type=int, default=1)
    parser.add_argument("--only", nargs="*", default=None)
    parser.add_argument("--out", type=Path, default=Path("e2e/runs"))
    args = parser.parse_args()

    settings = Settings()
    if not settings.diagnosis_llm_model:
        raise SystemExit("DEVICE_DIAGNOSIS_LLM_MODEL is required")
    if not settings.diagnosis_read_gateway_url:
        raise SystemExit("DEVICE_DIAGNOSIS_READ_GATEWAY_URL is required")

    spec = json.loads(args.scenarios.read_text(encoding="utf-8"))
    cases = [c for c in spec["cases"] if not args.only or c["id"] in args.only]
    bindings = await _seed(spec)

    # The API trusts a JWT issuer; reuse the in-process test identity provider.
    async def fetch_json(url: str) -> dict:
        return oidc.JWKS

    auth._fetch_json = fetch_json
    auth.reset_jwks_cache()
    os.environ.update(
        DEVICE_OIDC_ISSUER=oidc.ISSUER,
        DEVICE_OIDC_AUDIENCE=oidc.AUDIENCE,
        DEVICE_OIDC_JWKS_URL=oidc.JWKS_URL,
    )

    out_dir = args.out / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir.mkdir(parents=True, exist_ok=True)
    meta = {
        "model": settings.diagnosis_llm_model,
        "base_url": settings.diagnosis_llm_base_url,
        "embedding_model": settings.document_embedding_model or None,
        "trials": args.trials,
        "scenarios": str(args.scenarios),
        "started_at": datetime.now(UTC).isoformat(),
    }
    (out_dir / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))

    rows = []
    app = create_app()
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://e2e", timeout=600
    ) as client:
        for case in cases:
            binding = bindings[case["id"]]
            for trial in range(1, args.trials + 1):
                headers = oidc.auth_headers()
                created = await client.post(
                    "/api/v1/diagnosis-runs",
                    headers=headers,
                    json={
                        "device_id": binding["device_id"],
                        "symptom": case["symptom"],
                        "target_point_code": case.get("target_point_code"),
                        "max_steps": case.get("max_steps", 8),
                    },
                )
                created.raise_for_status()
                started = datetime.now(UTC)
                executed = await client.post(
                    f"/api/v1/diagnosis-runs/{created.json()['id']}/execute",
                    headers=headers,
                )
                elapsed = (datetime.now(UTC) - started).total_seconds()
                run = executed.json()
                if executed.status_code != 200:
                    run = {"status": "failed", "steps": [], "error": run}
                scored = _score(case, run)
                usage = _usage_totals(run.get("steps", []))
                record = {
                    "case_id": case["id"],
                    "trial": trial,
                    "elapsed_s": round(elapsed, 2),
                    "connection_id": binding["connection_id"],
                    "gold": case.get("gold"),
                    "score": scored,
                    "usage": usage,
                    "run": run,
                }
                with (out_dir / "runs.jsonl").open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
                rows.append(
                    {
                        "case_id": case["id"],
                        "trial": trial,
                        "verdict": scored["verdict"],
                        "status": scored["status"],
                        "classification": scored["classification"],
                        "failure": scored["failure"] or "",
                        "steps": scored["steps"],
                        "tools": " > ".join(scored["tools"]),
                        "prompt_tokens": usage["prompt_tokens"],
                        "completion_tokens": usage["completion_tokens"],
                        "cached_tokens": usage["cached_tokens"],
                        "elapsed_s": round(elapsed, 2),
                    }
                )
                print(json.dumps(rows[-1], ensure_ascii=False), flush=True)

    with (out_dir / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    print(f"written to {out_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(_main()))
