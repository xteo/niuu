"""``ForgeClient`` over the broker's Forge HTTP client.

The broker already holds an authenticated ``httpx.AsyncClient`` for Volundr
(``EventLogMixin._get_http_client``). MCP-proxied calls reuse it, with one
difference: when a scoped session token is configured (``forge_mcp.token``,
``SKULD__FORGE_MCP__TOKEN``) it replaces the broker's own ``Authorization`` for
these calls only, so the model's reach is bounded by that token's scopes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import quote

import httpx

from niuu.forge_mcp.models import ForgeApiError
from niuu.forge_mcp.ports import ForgeClient

FORGE_API_PREFIX = "/api/v1/forge"
_ERROR_DETAIL_CHARS = 500


def _detail(response: httpx.Response) -> str:
    try:
        body = response.json()
    except ValueError:
        return response.text[:_ERROR_DETAIL_CHARS]
    if isinstance(body, dict) and body.get("detail") is not None:
        return str(body["detail"])[:_ERROR_DETAIL_CHARS]
    return response.text[:_ERROR_DETAIL_CHARS]


def _instance_params(instance_id: str | None) -> dict[str, str]:
    return {"instance_id": instance_id} if instance_id else {}


class HttpForgeClient(ForgeClient):
    """Forge REST through the broker's HTTP client."""

    def __init__(
        self,
        *,
        http_client: Callable[[], Awaitable[httpx.AsyncClient]],
        token: str,
        timeout_s: float,
    ) -> None:
        self._http_client = http_client
        self._headers = {"Authorization": f"Bearer {token}"} if token else {}
        self._timeout_s = timeout_s

    async def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> Any:
        client = await self._http_client()
        try:
            response = await client.request(
                method,
                f"{FORGE_API_PREFIX}{path}",
                params=params or None,
                json=json,
                headers=self._headers or None,
                timeout=self._timeout_s,
            )
        except httpx.RequestError as exc:
            raise ForgeApiError(f"{type(exc).__name__}: {exc}") from exc
        if response.status_code >= 400:
            raise ForgeApiError(_detail(response), status=response.status_code)
        if not response.content:
            return {}
        try:
            return response.json()
        except ValueError as exc:
            raise ForgeApiError(
                f"non-JSON response from {path}", status=response.status_code
            ) from exc

    async def _object(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        body = await self._request(method, path, **kwargs)
        if not isinstance(body, dict):
            raise ForgeApiError(f"unexpected response shape from {path}")
        return body

    async def list_notifications(self, params: dict[str, str]) -> dict[str, Any]:
        return await self._object("GET", "/notifications", params=params)

    async def list_sessions(self, params: dict[str, str]) -> list[dict[str, Any]]:
        body = await self._request("GET", "/sessions", params=params)
        if not isinstance(body, list):
            raise ForgeApiError("unexpected response shape from /sessions")
        return body

    async def get_session(self, session_id: str, *, instance_id: str | None) -> dict[str, Any]:
        return await self._object(
            "GET", f"/sessions/{quote(session_id)}", params=_instance_params(instance_id)
        )

    async def get_conversation(
        self, session_id: str, *, turns: int, instance_id: str | None
    ) -> dict[str, Any]:
        params = {"detail": "shallow", "limit": str(turns), **_instance_params(instance_id)}
        return await self._object(
            "GET", f"/sessions/{quote(session_id)}/conversation", params=params
        )

    async def send_message(
        self, session_id: str, *, content: str, request_id: str, instance_id: str | None
    ) -> dict[str, Any]:
        return await self._object(
            "POST",
            f"/sessions/{quote(session_id)}/messages",
            params=_instance_params(instance_id),
            json={"content": content, "request_id": request_id},
        )

    async def get_message_delivery(
        self, session_id: str, *, request_id: str, instance_id: str | None
    ) -> dict[str, Any]:
        return await self._object(
            "GET",
            f"/sessions/{quote(session_id)}/message-deliveries/{quote(request_id)}",
            params=_instance_params(instance_id),
        )

    async def create_session(self, body: dict[str, Any]) -> dict[str, Any]:
        return await self._object("POST", "/sessions", json=body)

    async def start_session(self, session_id: str, *, instance_id: str | None) -> dict[str, Any]:
        return await self._object(
            "POST", f"/sessions/{quote(session_id)}/start", params=_instance_params(instance_id)
        )

    async def stop_session(self, session_id: str, *, instance_id: str | None) -> dict[str, Any]:
        return await self._object(
            "POST", f"/sessions/{quote(session_id)}/stop", params=_instance_params(instance_id)
        )
