"""Shared Niuu auth dependency for co-hosted local and browser flows."""

from __future__ import annotations

from fastapi import HTTPException, Request, status
from starlette.requests import HTTPConnection

from niuu.domain.models import Principal
from niuu.domain.services.forge_session_token import (
    FORGE_SESSION_ROLES,
    SESSION_ID_CLAIM,
)
from niuu.domain.services.token_scope import FORGE_SESSION_TOKEN_USE
from niuu.forge_mcp.credentials import is_forge_session_claims, token_scopes, unverified_claims

#: The only API a ``forge_session`` credential may call through a Niuu host.
FORGE_API_PREFIX = "/api/v1/forge/"
_BEARER_PREFIX = "bearer "


def _split_roles(raw: str) -> list[str]:
    return [role.strip() for role in raw.split(",") if role.strip()]


def presented_session_claims(connection: HTTPConnection) -> dict | None:
    """Claims (unverified) of a ``forge_session`` token in the Authorization header
    or ``?token=``, or ``None`` when the caller presents none."""
    candidates: list[str] = []
    authorization = connection.headers.get("authorization", "").strip()
    if authorization.lower().startswith(_BEARER_PREFIX):
        candidates.append(authorization[len(_BEARER_PREFIX) :].strip())
    query_token = connection.query_params.get("token")
    if query_token:
        candidates.append(query_token)
    for token in candidates:
        claims = unverified_claims(token)
        if is_forge_session_claims(claims):
            return claims
    return None


def _session_principal(request: Request) -> Principal | None:
    """The principal of a Forge session credential, or ``None`` for other callers.

    This host does not verify the token: it only routes. Every Forge route of the
    Guild facade forwards the credential to the owning Forge node, which verifies
    it, and the facade refuses to forward it to any node but the local one. Outside
    the Forge API a session credential is refused outright.
    """
    claims = presented_session_claims(request)
    if claims is None:
        return None
    if not request.url.path.startswith(FORGE_API_PREFIX):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Forge session tokens may only call the Forge API (/api/v1/forge)",
        )
    return Principal(
        user_id=str(claims.get("sub") or ""),
        email=str(claims.get("email") or ""),
        tenant_id=str(claims.get("tenant_id") or "default"),
        roles=list(FORGE_SESSION_ROLES),
        token_use=FORGE_SESSION_TOKEN_USE,
        scopes=token_scopes(claims),
        bound_session_id=str(claims.get(SESSION_ID_CLAIM) or "") or None,
    )


async def extract_principal(request: Request) -> Principal:
    """Extract a principal from trusted headers or fall back to local dev defaults.

    The shared Niuu surfaces are commonly used from the in-app browser against
    a local shell. When no forwarded identity headers are present, we provide a
    stable local developer principal so browsing the shell in anonymous-dev mode
    stays safe by default. Explicit `x-auth-*` headers always take precedence
    and are used by the guild proof to validate tenancy behavior. Non-production
    browser flows may also supply the same values through `dev*` query params.

    A Forge session credential comes first: it names its own owner and scopes,
    and is confined to the Forge API (see :func:`_session_principal`).
    """
    session_principal = _session_principal(request)
    if session_principal is not None:
        return session_principal

    user_id = request.headers.get("x-auth-user-id", "").strip()
    if user_id:
        return Principal(
            user_id=user_id,
            email=request.headers.get("x-auth-email", ""),
            tenant_id=request.headers.get("x-auth-tenant", ""),
            roles=_split_roles(request.headers.get("x-auth-roles", "volundr:developer")),
        )

    dev_user_id = request.query_params.get("devUserId", "").strip()
    if dev_user_id:
        return Principal(
            user_id=dev_user_id,
            email=request.query_params.get("devEmail", "").strip(),
            tenant_id=request.query_params.get("devTenantId", "").strip(),
            roles=_split_roles(request.query_params.get("devRoles", "volundr:developer")),
        )

    return Principal(
        user_id="dev-user",
        email="",
        tenant_id="default",
        roles=["volundr:developer"],
    )
