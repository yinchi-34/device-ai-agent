"""Start the local workbench with .env settings and Windows async DB support."""

import asyncio
import sys
from pathlib import Path

import uvicorn
from dotenv import load_dotenv

SERVICE_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SERVICE_ROOT))


if __name__ == "__main__":
    load_dotenv(SERVICE_ROOT / ".env")
    config = uvicorn.Config("app.main:app", host="127.0.0.1", port=8000, loop="none")
    loop_factory = (
        asyncio.SelectorEventLoop if sys.platform == "win32" else asyncio.new_event_loop
    )
    with asyncio.Runner(loop_factory=loop_factory) as runner:
        runner.run(uvicorn.Server(config).serve())
