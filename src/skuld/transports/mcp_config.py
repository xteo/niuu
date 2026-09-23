"""Helpers for adapting session MCP configs to specific CLI transports.

A session's MCP servers arrive as a list of plain dicts (launch spec, Forge
contributors, Skuld's built-in ``forge`` server). Each engine wants its own
shape:

* Claude Code ``--mcp-config`` JSON and the Claude Agent SDK ``mcp_servers``
  dict: ``{"type": "stdio"|"http"|"sse", "command"/"args"/"env" | "url"/"headers"}``.
  Claude expands ``${VAR}`` in ``headers``, so a ``bearer_token_env_var`` becomes
  an ``Authorization: Bearer ${VAR}`` header and the secret never enters argv.
* Codex ``-c key=value`` overrides (0.154): ``mcp_servers.<name>.url``,
  ``bearer_token_env_var`` and ``http_headers`` for streamable HTTP, or
  ``command``/``args``/``env``/``cwd`` for stdio. Values are TOML.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

# Streamable HTTP is the current MCP network transport; SSE is legacy. A URL
# server that does not declare its transport is treated as streamable HTTP by
# every translator so the engines agree.
_DEFAULT_URL_TRANSPORT = "http"
_URL_TRANSPORT_ALIASES = {
    "http": "http",
    "streamable_http": "http",
    "streamable-http": "http",
    "streamablehttp": "http",
    "sse": "sse",
}
_TIMEOUT_KEYS = ("startup_timeout_sec", "tool_timeout_sec")
_AUTHORIZATION_HEADER = "Authorization"


def _string_map(raw: object) -> dict[str, str]:
    if not isinstance(raw, dict):
        return {}
    return {str(key): str(value) for key, value in raw.items() if str(key)}


def normalize_mcp_servers(raw_servers: object) -> list[dict[str, Any]]:
    """Return a sanitized list of MCP server config dicts."""
    if not isinstance(raw_servers, list):
        return []

    servers: list[dict[str, Any]] = []
    for raw in raw_servers:
        if not isinstance(raw, dict):
            continue
        name = str(raw.get("name") or "").strip()
        if not name:
            continue
        entry: dict[str, Any] = {"name": name}
        if raw.get("type"):
            entry["type"] = str(raw["type"])
        if raw.get("command"):
            entry["command"] = str(raw["command"])
        if raw.get("url"):
            entry["url"] = str(raw["url"])
        if isinstance(raw.get("args"), list):
            entry["args"] = [str(arg) for arg in raw["args"]]
        if isinstance(raw.get("env"), dict):
            entry["env"] = _string_map(raw["env"])
        headers = _string_map(raw.get("headers"))
        if headers:
            entry["headers"] = headers
        bearer_env = str(raw.get("bearer_token_env_var") or "").strip()
        if bearer_env:
            entry["bearer_token_env_var"] = bearer_env
        if raw.get("description"):
            entry["description"] = str(raw["description"])
        if raw.get("cwd"):
            entry["cwd"] = str(raw["cwd"])
        for timeout_key in _TIMEOUT_KEYS:
            timeout = raw.get(timeout_key)
            if isinstance(timeout, (int, float)) and not isinstance(timeout, bool) and timeout > 0:
                entry[timeout_key] = float(timeout)
        servers.append(entry)
    return servers


def with_default_mcp_servers(
    configured: list[dict[str, Any]], defaults: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """Append built-in servers unless a configured server already uses the same name.

    A launch-spec entry named like a built-in (e.g. ``forge``) therefore overrides it.
    """
    taken = {str(server.get("name") or "").strip() for server in configured}
    added = [server for server in defaults if str(server.get("name") or "").strip() not in taken]
    return [*configured, *added]


def url_transport(server: dict[str, Any]) -> str:
    """Return ``http`` or ``sse`` for a URL server (undeclared means streamable HTTP)."""
    declared = str(server.get("type") or "").strip().lower()
    return _URL_TRANSPORT_ALIASES.get(declared, _DEFAULT_URL_TRANSPORT)


def _claude_headers(server: dict[str, Any]) -> dict[str, str]:
    """Headers for Claude URL servers; the bearer env var is left for Claude to expand."""
    headers = dict(server.get("headers") or {})
    bearer_env = server.get("bearer_token_env_var")
    has_authorization = any(key.lower() == "authorization" for key in headers)
    if bearer_env and not has_authorization:
        headers[_AUTHORIZATION_HEADER] = f"Bearer ${{{bearer_env}}}"
    return headers


def _claude_server_entry(server: dict[str, Any]) -> dict[str, Any]:
    """One server in the shape Claude Code and the Agent SDK accept."""
    if server.get("url"):
        entry: dict[str, Any] = {"type": url_transport(server), "url": server["url"]}
        headers = _claude_headers(server)
        if headers:
            entry["headers"] = headers
        return entry

    entry = {"command": server.get("command") or ""}
    if server.get("type") == "stdio":
        entry["type"] = "stdio"
    if server.get("args"):
        entry["args"] = list(server["args"])
    if server.get("env"):
        entry["env"] = dict(server["env"])
    return entry


def build_claude_mcp_payload(raw_servers: object) -> dict[str, Any] | None:
    """Return the ``{"mcpServers": {...}}`` document Claude's ``--mcp-config`` loads."""
    servers = normalize_mcp_servers(raw_servers)
    if not servers:
        return None
    return {"mcpServers": {server["name"]: _claude_server_entry(server) for server in servers}}


def build_claude_mcp_config(raw_servers: object) -> str | None:
    """Serialize MCP servers into Claude's ``--mcp-config`` JSON format."""
    payload = build_claude_mcp_payload(raw_servers)
    if payload is None:
        return None
    return json.dumps(payload)


def build_sdk_mcp_servers(raw_servers: object) -> dict[str, dict[str, Any]]:
    """Translate MCP servers into ``ClaudeAgentOptions.mcp_servers`` format."""
    payload = build_claude_mcp_payload(raw_servers)
    if payload is None:
        return {}
    return payload["mcpServers"]


def _toml_string(value: str) -> str:
    """A TOML basic string (JSON's escaping is close, but TOML forbids raw DEL and surrogates)."""
    escaped: list[str] = []
    for char in value:
        code = ord(char)
        if char == '"':
            escaped.append('\\"')
        elif char == "\\":
            escaped.append("\\\\")
        elif char == "\n":
            escaped.append("\\n")
        elif char == "\t":
            escaped.append("\\t")
        elif char == "\r":
            escaped.append("\\r")
        elif code < 0x20 or code == 0x7F:
            escaped.append(f"\\u{code:04x}")
        else:
            escaped.append(char)
    return '"' + "".join(escaped) + '"'


def toml_value(value: object) -> str:
    """Serialize a config value for Codex ``-c key=<toml>`` (strings, numbers, lists, tables)."""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"TOML cannot represent non-finite number {value!r}")
        return repr(value)
    if isinstance(value, str):
        return _toml_string(value)
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(toml_value(item) for item in value) + "]"
    if isinstance(value, dict):
        pairs = ", ".join(f"{_toml_string(str(k))} = {toml_value(v)}" for k, v in value.items())
        return "{ " + pairs + " }" if pairs else "{}"
    raise TypeError(f"Unsupported TOML value type: {type(value).__name__}")


def build_codex_mcp_overrides(raw_servers: object) -> list[tuple[str, str]]:
    """Translate MCP servers into ``codex -c key=value`` override pairs."""
    servers = normalize_mcp_servers(raw_servers)
    overrides: list[tuple[str, str]] = []
    for server in servers:
        base = f"mcp_servers.{server['name']}"
        if server.get("url"):
            overrides.append((f"{base}.url", toml_value(server["url"])))
            if server.get("bearer_token_env_var"):
                overrides.append(
                    (f"{base}.bearer_token_env_var", toml_value(server["bearer_token_env_var"]))
                )
            for header, header_value in dict(server.get("headers") or {}).items():
                overrides.append((f"{base}.http_headers.{header}", toml_value(header_value)))
        else:
            overrides.append((f"{base}.command", toml_value(server.get("command") or "")))
            overrides.append((f"{base}.args", toml_value(list(server.get("args") or []))))
            for env_key, env_value in dict(server.get("env") or {}).items():
                overrides.append((f"{base}.env.{env_key}", toml_value(env_value)))
            if server.get("cwd"):
                overrides.append((f"{base}.cwd", toml_value(server["cwd"])))
        for timeout_key in _TIMEOUT_KEYS:
            if timeout_key in server:
                overrides.append((f"{base}.{timeout_key}", toml_value(server[timeout_key])))
    return overrides


def mcp_server_names(raw_servers: object) -> list[str]:
    """Names of the servers a translator would emit (for environment reporting)."""
    return [server["name"] for server in normalize_mcp_servers(raw_servers)]


def _name_value_pairs(values: dict[str, str]) -> list[dict[str, str]]:
    return [{"name": key, "value": value} for key, value in values.items()]


def build_acp_mcp_servers(
    raw_servers: object,
    *,
    env: Mapping[str, str],
    supports_http: bool,
    supports_sse: bool,
) -> list[dict[str, Any]]:
    """Translate MCP servers into ACP ``session/new`` ``mcpServers`` entries.

    ACP stdio servers are ``{name, command, args, env: [{name, value}]}``; HTTP/SSE
    servers are ``{type, name, url, headers: [{name, value}]}`` and are only valid
    when the agent advertises the matching ``mcpCapabilities``. ACP has no env-var
    expansion, so a ``bearer_token_env_var`` is resolved from ``env`` here (the
    value travels over the agent's stdin, never argv).
    """
    servers: list[dict[str, Any]] = []
    for server in normalize_mcp_servers(raw_servers):
        if not server.get("url"):
            servers.append(
                {
                    "name": server["name"],
                    "command": server.get("command") or "",
                    "args": list(server.get("args") or []),
                    "env": _name_value_pairs(dict(server.get("env") or {})),
                }
            )
            continue
        transport = url_transport(server)
        supported = supports_http if transport == "http" else supports_sse
        if not supported:
            raise ValueError(
                f"MCP server {server['name']!r} uses {transport}, which this ACP agent does "
                "not advertise in mcpCapabilities; use a stdio server or remove it"
            )
        headers = dict(server.get("headers") or {})
        bearer_env = server.get("bearer_token_env_var")
        if bearer_env and not any(key.lower() == "authorization" for key in headers):
            token = env.get(bearer_env, "")
            if not token:
                raise ValueError(
                    f"MCP server {server['name']!r} needs ${bearer_env} "
                    "(bearer_token_env_var), which is not set for this session"
                )
            headers[_AUTHORIZATION_HEADER] = f"Bearer {token}"
        servers.append(
            {
                "type": transport,
                "name": server["name"],
                "url": server["url"],
                "headers": _name_value_pairs(headers),
            }
        )
    return servers
