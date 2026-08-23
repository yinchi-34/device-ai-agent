from dataclasses import asdict, dataclass
from typing import Any, Protocol

import httpx


@dataclass(frozen=True)
class ReadPointRequest:
    device_id: int
    connection_id: int
    protocol: str
    point_code: str
    address_text: str | None
    address_spec: dict[str, Any]
    data_type: str
    access_mode: str
    connection_config: dict[str, Any]
    secret_ref: str | None


class ReadOnlyRegisterGateway(Protocol):
    async def read_point(self, request: ReadPointRequest) -> dict[str, Any]:
        raise NotImplementedError


class UnavailableReadGateway:
    async def read_point(self, request: ReadPointRequest) -> dict[str, Any]:
        del request
        return {
            "status": "tool_unavailable",
            "error_code": "read_gateway_not_configured",
        }


class HttpReadOnlyRegisterGateway:
    """Narrow adapter exposing only a released-profile point read."""

    def __init__(
        self,
        *,
        base_url: str,
        token: str = "",
        timeout_seconds: float = 10,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout_seconds = timeout_seconds
        self.transport = transport

    async def read_point(self, request: ReadPointRequest) -> dict[str, Any]:
        headers = {"Authorization": f"Bearer {self.token}"} if self.token else {}
        try:
            async with httpx.AsyncClient(
                base_url=self.base_url,
                headers=headers,
                timeout=self.timeout_seconds,
                transport=self.transport,
            ) as client:
                response = await client.post("/read-point", json=asdict(request))
        except httpx.TimeoutException:
            return {"status": "timeout"}
        except httpx.TransportError:
            return {"status": "gateway_error", "error_code": "gateway_connection_error"}
        if response.status_code >= 500:
            return {"status": "gateway_error", "error_code": "gateway_unavailable"}
        if response.is_error:
            return {"status": "gateway_error", "error_code": "gateway_request_rejected"}
        try:
            payload = response.json()
        except ValueError:
            return {
                "status": "gateway_error",
                "error_code": "gateway_malformed_response",
            }
        if not isinstance(payload, dict):
            return {
                "status": "gateway_error",
                "error_code": "gateway_malformed_response",
            }
        return payload
