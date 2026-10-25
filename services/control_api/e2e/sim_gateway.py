"""Modbus TCP simulators plus the read-only HTTP gateway the diagnosis agent calls.

Faults come from real protocol behaviour, not canned tool results:
a closed port refuses the connection, an unmapped register raises Modbus
exception 2, a silent socket times out, and register contents are whatever
the scenario wrote (wrong word order, out-of-range values, ...).

Run:  python -m e2e.sim_gateway e2e/scenarios.json --gateway-port 8090
"""

import argparse
import asyncio
import json
import struct
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import FastAPI
from pymodbus.client import AsyncModbusTcpClient
from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusServerContext,
    ModbusSparseDataBlock,
)
from pymodbus.exceptions import ModbusException
from pymodbus.server import StartAsyncTcpServer

_WORDS = {"float32": 2, "uint32": 2, "int32": 2, "unix_ts": 2}
_FORMATS = {"float32": "f", "uint32": "I", "int32": "i", "uint16": "H", "int16": "h"}


def encode_value(value: float, data_type: str, word_order: str = "big") -> list[int]:
    if data_type == "bool":
        return [1 if value else 0]
    payload = struct.pack(">" + _FORMATS[data_type], value)
    words = [int.from_bytes(payload[i : i + 2], "big") for i in range(0, len(payload), 2)]
    return words if word_order == "big" else list(reversed(words))


def register_offset(address_text: str) -> tuple[str, int]:
    text = str(address_text).strip()
    number = int(text)
    if len(text) == 5 and text[0] in "34":
        table = "holding" if text[0] == "4" else "input"
        return table, number - (40001 if table == "holding" else 30001)
    return "holding", number


def _sparse_block(values: dict[int, int]) -> ModbusSparseDataBlock:
    """Only configured registers exist; anything else is an illegal address."""
    return ModbusSparseDataBlock(dict(values))


async def _silent_server(port: int) -> asyncio.base_events.Server:
    async def handle(reader, writer):
        await reader.read(1024)
        await asyncio.sleep(3600)

    return await asyncio.start_server(handle, "127.0.0.1", port)


async def start_simulators(devices: list[dict[str, Any]]) -> None:
    for device in devices:
        mode = device.get("mode", "online")
        port = device["port"]
        if mode == "offline":
            continue
        if mode == "silent":
            await _silent_server(port)
            continue
        holding: dict[int, int] = {}
        for register in device.get("registers", []):
            _, offset = register_offset(register["address"])
            words = encode_value(
                register["value"],
                register["data_type"],
                register.get("stored_word_order", "big"),
            )
            for i, word in enumerate(words):
                holding[offset + i] = word
        context = ModbusServerContext(
            devices={
                device.get("unit_id", 1): ModbusDeviceContext(hr=_sparse_block(holding))
            },
            single=False,
        )
        asyncio.create_task(
            StartAsyncTcpServer(context=context, address=("127.0.0.1", port))
        )
    await asyncio.sleep(0.5)


def build_gateway(timeout_seconds: float) -> FastAPI:
    app = FastAPI()
    app.state.requests = []

    @app.post("/read-point")
    async def read_point(request: dict[str, Any]) -> dict[str, Any]:
        app.state.requests.append(request)
        config = request.get("connection_config") or {}
        table, offset = register_offset(request["address_text"])
        count = _WORDS.get(request["data_type"], 1)
        client = AsyncModbusTcpClient(
            config.get("host", "127.0.0.1"),
            port=int(config["port"]),
            timeout=timeout_seconds,
            retries=0,
        )
        try:
            if not await client.connect():
                return {"status": "connection_refused"}
            reader = (
                client.read_holding_registers
                if table == "holding"
                else client.read_input_registers
            )
            response = await reader(
                offset, count=count, device_id=int(config.get("unit_id", 1))
            )
        except TimeoutError:
            return {"status": "timeout"}
        except ModbusException as exc:
            if "timeout" in str(exc).lower() or "no response" in str(exc).lower():
                return {"status": "timeout"}
            return {"status": "gateway_error", "error_code": type(exc).__name__}
        finally:
            client.close()
        if response.isError():
            code = getattr(response, "exception_code", None)
            names = {1: "illegal_function", 2: "illegal_data_address", 3: "illegal_data_value"}
            return {
                "status": "modbus_exception",
                "exception_code": code,
                "exception_name": names.get(code, "unknown"),
            }
        return {"status": "success", "raw_words": list(response.registers)}

    return app


async def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("scenarios", type=Path)
    parser.add_argument("--gateway-port", type=int, default=8090)
    parser.add_argument("--timeout", type=float, default=2.0)
    args = parser.parse_args()
    spec = json.loads(args.scenarios.read_text(encoding="utf-8"))
    await start_simulators(spec["devices"])
    config = uvicorn.Config(
        build_gateway(args.timeout), host="127.0.0.1", port=args.gateway_port, log_level="warning"
    )
    await uvicorn.Server(config).serve()


if __name__ == "__main__":
    asyncio.run(_main())
