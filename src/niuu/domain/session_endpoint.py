"""Browser-facing session endpoint normalization."""

from __future__ import annotations

import ipaddress
from urllib.parse import quote, urlsplit, urlunsplit

OPENSHELL_SERVICE_HOST_SUFFIX = ".openshell.localhost"


def is_loopback_or_wildcard(host: str | None) -> bool:
    """Whether a host is only reachable on this machine (or is a bind-all address).

    ``localhost``, the whole ``127.0.0.0/8`` range, ``::1`` and the bind-all forms
    ``0.0.0.0`` / ``::``: none of them is an address a client elsewhere can open.
    """
    if not host:
        return False
    name = host.strip("[]").lower()
    if name == "localhost":
        return True
    try:
        address = ipaddress.ip_address(name)
    except ValueError:
        return False
    return address.is_loopback or address.is_unspecified


def public_session_endpoint(
    endpoint: str | None,
    *,
    session_id: str = "",
    public_host: str | None = None,
) -> str | None:
    """Normalize an internal session endpoint for a browser-facing response."""
    if not endpoint:
        return endpoint

    try:
        parsed = urlsplit(endpoint)
    except ValueError:
        return endpoint

    if session_id and parsed.hostname and parsed.hostname.endswith(OPENSHELL_SERVICE_HOST_SUFFIX):
        return f"/s/{quote(session_id, safe='')}/session"

    # Every loopback or bind-all form, not only 127.0.0.1 (LEX-1847): a session's endpoint
    # is stored eagerly from the server's own origin, which a 0.0.0.0 bind turns into
    # localhost, and a client using the start response before a relist got a dead route.
    if not is_loopback_or_wildcard(parsed.hostname):
        return endpoint

    normalized_host = str(public_host or "127.0.0.1").strip() or "127.0.0.1"
    browser_host = "localhost" if is_loopback_or_wildcard(normalized_host) else normalized_host
    netloc = browser_host
    if parsed.port is not None:
        netloc = f"{browser_host}:{parsed.port}"

    return urlunsplit((parsed.scheme, netloc, parsed.path, parsed.query, parsed.fragment))
