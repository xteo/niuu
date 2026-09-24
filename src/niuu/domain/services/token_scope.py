"""Least-privilege scope enforcement for short-lived workload credentials.

Two kinds of scoped credential exist. Each is marked by its ``token_use``
claim and carries a ``scopes`` claim bounded, at issuance, to its own *family*
of scopes (:data:`SCOPES_BY_TOKEN_USE`):

- ``token_use == "valkyrie_build"`` — minted by the workload-identity exchange
  for a workload that needs to do one specific thing (commission a build,
  launch a workflow, publish its own topology). The value is historical:
  builds were the first use, but the marker means "this credential is scoped",
  not "this credential builds".
- ``token_use == "forge_session"`` — minted by Forge when it launches a
  session, bound to that one session, and held by its broker for the Forge MCP
  (notify, read sessions and, with an operator grant, message or run peers).

Enforcement is **stateless and fail-closed for scoped tokens only**:

- A token without a scoped ``token_use`` (humans, PATs, legacy workload
  tokens) passes through untouched — full backward compatibility.
- A scoped token is admitted at an entry point only when its ``scopes`` claim
  contains one of the required scopes *of its own family*; otherwise it is
  403'd.
- A check naming no scope of the token's family does not apply to a
  ``valkyrie_build`` token (it is only restricted at its own entry points, as
  before) but denies a ``forge_session`` token: session credentials are
  deny-by-default everywhere. Forge additionally verifies ``forge_session``
  tokens itself and admits them only on an allow-list of routes
  (``volundr.adapters.inbound.forge_session_auth``).

A scope is only real because code enforces it: :func:`require_scope` on a
route is what gives the string meaning. Adding an entry here without a
matching enforcement point produces a credential that reads as restricted but
protects nothing, which is why `test_token_scope.py` asserts the two stay in
step.

The JWT is decoded WITHOUT signature verification — the same posture as
``PATValidator``: Envoy validates the signature upstream, so this layer
only reads the already-trusted claims.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable

import jwt
from fastapi import HTTPException, Request, status

logger = logging.getLogger(__name__)

#: The claim value that marks a token as a scoped workload credential.
VALKYRIE_BUILD_TOKEN_USE = "valkyrie_build"
#: The claim value of the session-bound credential Forge mints at launch.
FORGE_SESSION_TOKEN_USE = "forge_session"
OPENSHELL_SESSION_TOKEN_USE = "openshell_session"
OPENSHELL_RESIDENT_TOKEN_USE = "openshell_resident"

#: Scope required to publish a topology fragment to the push inbox.
TOPOLOGY_PUSH_SCOPE = "observatory:topology:push"

#: Forge session credential scopes. ``notify`` and ``read`` are granted to every
#: session; ``message`` and ``lifecycle`` only through an operator grant.
FORGE_NOTIFY_SCOPE = "forge:notify"
FORGE_SESSION_READ_SCOPE = "forge:session:read"
FORGE_SESSION_MESSAGE_SCOPE = "forge:session:message"
FORGE_SESSION_LIFECYCLE_SCOPE = "forge:session:lifecycle"

#: Scopes a ``valkyrie_build`` exchange may grant.
VALKYRIE_BUILD_SCOPES: frozenset[str] = frozenset(
    {
        "forge:session:create",
        "ting:workflow:launch",
        TOPOLOGY_PUSH_SCOPE,
    }
)

#: Scopes a ``forge_session`` credential may carry.
FORGE_SESSION_SCOPES: frozenset[str] = frozenset(
    {
        FORGE_NOTIFY_SCOPE,
        FORGE_SESSION_READ_SCOPE,
        FORGE_SESSION_MESSAGE_SCOPE,
        FORGE_SESSION_LIFECYCLE_SCOPE,
    }
)

#: Each scoped ``token_use`` and the family of scopes it may carry.
SCOPES_BY_TOKEN_USE: dict[str, frozenset[str]] = {
    VALKYRIE_BUILD_TOKEN_USE: VALKYRIE_BUILD_SCOPES,
    FORGE_SESSION_TOKEN_USE: FORGE_SESSION_SCOPES,
}

#: Scoped token uses that are denied wherever no scope of their family applies.
_DENY_BY_DEFAULT_TOKEN_USES: frozenset[str] = frozenset({FORGE_SESSION_TOKEN_USE})

#: The scopes a short-lived workload credential may ever be granted. A caller
#: cannot self-grant anything outside this allowlist — unknown scopes are
#: dropped at issuance time, which is why this is deliberately a constant and
#: not configuration: whoever could edit the config could mint privilege.
#:
#: Every entry must have an enforcement point (see the module docstring).
KNOWN_WORKLOAD_SCOPES: frozenset[str] = VALKYRIE_BUILD_SCOPES | FORGE_SESSION_SCOPES


def _decode_claims(token: str) -> dict | None:
    """Decode a JWT's claims without verifying its signature.

    Returns ``None`` when the token is missing or malformed. Signature
    verification is delegated to Envoy upstream, so this layer only reads
    the already-trusted claims (same posture as ``PATValidator``).
    """
    if not token:
        return None
    try:
        return jwt.decode(
            token,
            options={"verify_signature": False, "verify_exp": False},
        )
    except jwt.InvalidTokenError:
        return None


def token_requires_scope_check(claims: dict) -> bool:
    """Return True when the token's claims mark it as a scoped credential."""
    return claims.get("token_use") in SCOPES_BY_TOKEN_USE


def claims_have_scope(claims: dict, scopes: Iterable[str]) -> bool:
    """Return True when a token with ``claims`` may use one of ``scopes``.

    - An unscoped token (no scoped ``token_use``) is always admitted.
    - A scoped token is admitted when its ``scopes`` claim holds one of the
      requested scopes that belong to its own family.
    - When none of the requested scopes belongs to its family, a
      ``valkyrie_build`` token is admitted (it is only restricted at its own
      entry points) and a ``forge_session`` token is denied.
    - A malformed ``scopes`` claim grants nothing.
    """
    token_use = claims.get("token_use")
    family = SCOPES_BY_TOKEN_USE.get(token_use) if isinstance(token_use, str) else None
    if family is None:
        return True

    applicable = [scope for scope in scopes if scope in family]
    if not applicable:
        return token_use not in _DENY_BY_DEFAULT_TOKEN_USES

    granted = claims.get("scopes", [])
    if not isinstance(granted, list):
        return False
    return any(scope in granted for scope in applicable)


def token_has_scope(token: str, scope: str) -> bool:
    """Return True when ``token`` is permitted to use ``scope``.

    Backward-compatible and fail-closed for scoped tokens only; see
    :func:`claims_have_scope`. A missing or malformed token passes through
    (humans / PATs / legacy workload tokens are unaffected).
    """
    claims = _decode_claims(token)
    if claims is None:
        return True
    return claims_have_scope(claims, (scope,))


def bound_workload_scopes(
    requested: list[str] | None,
    *,
    allowed: frozenset[str] = KNOWN_WORKLOAD_SCOPES,
) -> list[str]:
    """Intersect requested scopes with ``allowed`` (default: every known scope).

    Unknown scopes are dropped (and logged) so a caller can never
    self-grant a scope the platform does not recognise. Order and
    duplicates from the request are collapsed to a stable, de-duplicated
    list of known scopes. Issuers pass their own family
    (:data:`VALKYRIE_BUILD_SCOPES`, :data:`FORGE_SESSION_SCOPES`) so one kind of
    credential can never carry another kind's scopes.
    """
    if not requested:
        return []

    seen: set[str] = set()
    kept: list[str] = []
    dropped: list[str] = []
    for raw in requested:
        scope = str(raw).strip()
        if not scope or scope in seen:
            continue
        seen.add(scope)
        if scope in allowed:
            kept.append(scope)
            continue
        dropped.append(scope)

    if dropped:
        logger.warning(
            "Dropping unknown scopes from token request: %s",
            ", ".join(sorted(dropped)),
        )
    return kept


def _bearer_from_request(request: Request) -> str:
    """Extract the raw bearer token from the Authorization header, or ""."""
    auth = request.headers.get("authorization", "")
    if not auth.startswith("Bearer "):
        return ""
    return auth[7:]


def require_scope(scope: str, *alternatives: str) -> Callable[..., Awaitable[None]]:
    """FastAPI dependency factory enforcing a scope (any of several), fail-closed.

    The returned dependency reads the bearer token from the request's
    Authorization header, then raises HTTP 403 when the token is a scoped
    credential lacking every applicable scope (see :func:`claims_have_scope`).
    Unscoped tokens are admitted unchanged. Name one scope per credential
    family when a route serves both, e.g.
    ``require_scope("forge:session:create", FORGE_SESSION_LIFECYCLE_SCOPE)``.

    Usage::

        @router.post("/sessions", ...)
        async def create_session(
            request: Request,
            data: SessionCreate,
            _: None = Depends(require_scope("forge:session:create")),
        ) -> SessionResponse:
            ...
    """
    scopes = (scope, *alternatives)
    for name in scopes:
        if name not in KNOWN_WORKLOAD_SCOPES:
            raise ValueError(f"Unknown workload scope: {name}")
    wanted = " or ".join(scopes)

    async def _check(request: Request) -> None:
        claims = _decode_claims(_bearer_from_request(request))
        if claims is None or claims_have_scope(claims, scopes):
            return None
        logger.warning("Scoped token denied: missing scope %s", wanted)
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=f"Token is missing the required scope: {wanted}",
        )

    return _check


__all__ = [
    "FORGE_NOTIFY_SCOPE",
    "FORGE_SESSION_LIFECYCLE_SCOPE",
    "FORGE_SESSION_MESSAGE_SCOPE",
    "FORGE_SESSION_READ_SCOPE",
    "FORGE_SESSION_SCOPES",
    "FORGE_SESSION_TOKEN_USE",
    "KNOWN_WORKLOAD_SCOPES",
    "OPENSHELL_SESSION_TOKEN_USE",
    "OPENSHELL_RESIDENT_TOKEN_USE",
    "SCOPES_BY_TOKEN_USE",
    "TOPOLOGY_PUSH_SCOPE",
    "VALKYRIE_BUILD_SCOPES",
    "VALKYRIE_BUILD_TOKEN_USE",
    "bound_workload_scopes",
    "claims_have_scope",
    "require_scope",
    "token_has_scope",
    "token_requires_scope_check",
]
