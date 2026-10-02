"""REST routes of the Guild push gateway.

* ``GET  /push/status``  what the gateway has done since it started
* ``POST /push/test``    push once to the caller's own registered tokens

Both exist only on a node that runs the gateway (``push.gateway.enabled``). The
test push goes to the caller's tokens and nobody else's, so it needs no admin role.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, ConfigDict, Field

from volundr.adapters.inbound.auth import extract_principal
from volundr.domain.models import Principal
from volundr.domain.push import PushTokenKind
from volundr.domain.push_ports import GuildFeedError
from volundr.domain.services.push_gateway import PushGateway

#: Bounds of a test alert's text; a real alert's come from the notification.
MAX_TEST_TITLE_CHARS = 120
MAX_TEST_BODY_CHARS = 280


class PushTestRequest(BaseModel):
    """Which of the caller's tokens to push to, and the test alert's text."""

    model_config = ConfigDict(extra="forbid")

    kind: PushTokenKind = PushTokenKind.ALERT
    title: str = Field(default="Forge test push", min_length=1, max_length=MAX_TEST_TITLE_CHARS)
    body: str = Field(
        default="Push from Forge reaches this device.", max_length=MAX_TEST_BODY_CHARS
    )


class PushTestDevice(BaseModel):
    """APNs' answer for one token. Only the token's last characters are shown."""

    kind: str
    environment: str
    token_suffix: str
    accepted: bool
    detail: str
    apns_id: str


class PushTestResponse(BaseModel):
    kind: str
    sent: int
    accepted: int
    devices: list[PushTestDevice]


def create_push_router(gateway: PushGateway, *, prefix: str = "/api/v1/forge") -> APIRouter:
    """Create the push gateway's status and test routes."""
    router = APIRouter(prefix=prefix, tags=["Push"])

    @router.get("/push/status")
    async def push_status(_principal: Principal = Depends(extract_principal)) -> dict[str, Any]:
        """Counters since start, the nodes being read and the last error."""
        return gateway.status()

    @router.post("/push/test", response_model=PushTestResponse)
    async def send_test_push(
        data: PushTestRequest, principal: Principal = Depends(extract_principal)
    ) -> PushTestResponse:
        """Push once to each of the caller's registered tokens of ``kind``.

        ``alert`` shows the given title and body and opens the app's activity list;
        ``live_activity`` and ``live_activity_start`` carry the caller's current
        notifications; ``widget`` reloads the widgets. Throttles do not apply.
        """
        try:
            results = await gateway.send_test(
                principal.user_id, data.kind, title=data.title, body=data.body
            )
        except GuildFeedError as exc:
            raise HTTPException(status.HTTP_502_BAD_GATEWAY, str(exc)) from exc
        if not results:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND,
                f"You have no registered {data.kind.value} token on this Forge",
            )
        return PushTestResponse(
            kind=data.kind.value,
            sent=len(results),
            accepted=sum(1 for result in results if result.accepted),
            devices=[
                PushTestDevice(
                    kind=result.kind,
                    environment=result.environment,
                    token_suffix=result.token_suffix,
                    accepted=result.accepted,
                    detail=result.detail,
                    apns_id=result.apns_id,
                )
                for result in results
            ],
        )

    return router
