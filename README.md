# Device AI Agent

Phase 1 provides a FastAPI control service for device definitions and testing
metadata.

## Control API

Run the development server from `services/control_api`:

```powershell
..\..\.venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

The health endpoint is available at `GET /health` and returns
`{"status": "ok"}`.
