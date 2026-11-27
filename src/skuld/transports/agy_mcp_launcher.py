"""Start one of a Skuld session's MCP servers for the Antigravity CLI (``agy``).

``agy`` reads MCP servers from one config per home, shared by every session in
that home, but each session's servers carry their own arguments (the Forge MCP
names its broker and token file) and their own credentials. The shared config
therefore lists this script once per server name, and the broker that launched
``agy`` puts the real descriptions in ``SKULD_AGY_MCP_SERVERS``; this script
starts the one named on its command line:

* a stdio server: this script replaces itself with the server's command;
* a streamable-HTTP server: this script stays and bridges agy's stdio to the
  server's URL. Each request reads the auth header afresh from the session's
  credential file or variable, so a rotated token is picked up and no token is
  ever written to the shared config.

Standard-library only: it is copied into the ``agy`` home and run with the
system Python. An ``agy`` started without a broker has no such variable, and
the server then fails to start with the reason on stderr.

The bridge relays every message agy sends and every reply the server returns
to it, including server messages streamed on a request's response. It does not
open the optional standalone GET stream, so a server message sent outside any
request is not relayed.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import urllib.error
import urllib.request
from datetime import datetime, timezone  # noqa: UP017

# typing, not collections.abc, and timezone.utc: this runs on the system Python (3.9 on macOS).
from typing import Any, BinaryIO, Callable, Iterable, Iterator  # noqa: UP035

SERVERS_ENV = "SKULD_AGY_MCP_SERVERS"
SESSION_HEADER = "Mcp-Session-Id"
PROTOCOL_HEADER = "MCP-Protocol-Version"
_ACCEPT = "application/json, text/event-stream"
# JSON-RPC "internal error": the bridge could not get the server's answer.
_BRIDGE_ERROR_CODE = -32603


def resolve(name: str, environ: dict[str, str]) -> dict[str, Any]:
    """Server ``name``'s description from the session's variable."""
    raw = environ.get(SERVERS_ENV, "")
    if not raw:
        raise SystemExit(
            f"skuld agy mcp: {SERVERS_ENV} is not set; {name!r} runs only in a Forge session"
        )
    servers = json.loads(raw)
    spec = servers.get(name)
    if not isinstance(spec, dict):
        raise SystemExit(f"skuld agy mcp: this session has no MCP server named {name!r}")
    return spec


def stdio_command(
    spec: dict[str, Any], environ: dict[str, str]
) -> tuple[list[str], dict[str, str]]:
    """The argv and environment for a stdio server."""
    argv = [str(spec["command"]), *[str(arg) for arg in spec.get("args") or []]]
    env = dict(environ)
    env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
    return argv, env


def _token(spec: dict[str, Any], environ: dict[str, str]) -> str:
    if spec.get("credential_env"):
        token = environ.get(str(spec["credential_env"]), "")
    else:
        with open(str(spec["credential_file"]), encoding="utf-8") as handle:
            token = handle.read().strip()
        if spec.get("credential_format") == "oauth":
            document = json.loads(token)
            token = document["access_token"]
            expires = document.get("expires_at")
            if expires:
                expiry = datetime.fromisoformat(expires)
                now = datetime.now(timezone.utc)  # noqa: UP017
                if expiry.tzinfo is None or expiry <= now:
                    raise ValueError("the MCP credential expired; reconnect the integration")
    if not isinstance(token, str) or not token or any(c in token for c in "\r\n"):
        raise ValueError("the MCP credential is missing or malformed")
    return token


def auth_headers(spec: dict[str, Any], environ: dict[str, str]) -> dict[str, str]:
    """The static headers plus the current auth header, read now (tokens rotate)."""
    headers = {str(k): str(v) for k, v in (spec.get("headers") or {}).items()}
    if spec.get("credential_file") or spec.get("credential_env"):
        header = str(spec.get("auth_header") or "Authorization")
        prefix = str(spec.get("auth_prefix") if "auth_prefix" in spec else "Bearer ")
        headers[header] = prefix + _token(spec, environ)
        return headers
    bearer_env = str(spec.get("bearer_token_env_var") or "")
    if bearer_env and not any(k.lower() == "authorization" for k in headers):
        token = environ.get(bearer_env, "")
        if not token or any(c in token for c in "\r\n"):
            raise ValueError(f"{bearer_env} is not set in this session")
        headers["Authorization"] = f"Bearer {token}"
    return headers


def sse_messages(lines: Iterable[bytes]) -> Iterator[str]:
    """The data of each server-sent event, as the server sent it."""
    data: list[str] = []
    for raw in lines:
        line = raw.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
            data = []
            continue
        if line.startswith(":"):
            continue
        field, _, value = line.partition(":")
        if field == "data":
            data.append(value[1:] if value.startswith(" ") else value)
    if data:
        yield "\n".join(data)


def _default_opener(request: urllib.request.Request, timeout: float | None) -> Any:
    return urllib.request.urlopen(request, timeout=timeout)  # noqa: S310 - the session's own URL


class HttpBridge:
    """Relays newline-delimited JSON-RPC from agy to a streamable-HTTP MCP server."""

    def __init__(
        self,
        spec: dict[str, Any],
        environ: dict[str, str],
        out: BinaryIO,
        *,
        opener: Callable[..., Any] = _default_opener,
    ) -> None:
        self._spec = spec
        self._environ = environ
        self._out = out
        self._opener = opener
        self._timeout = spec.get("tool_timeout_sec")
        self._session_id = ""
        self._protocol = ""
        self._write_lock = threading.Lock()
        self._state_lock = threading.Lock()

    def write(self, text: str) -> None:
        with self._write_lock:
            self._out.write(text.encode("utf-8") + b"\n")
            self._out.flush()

    def _headers(self) -> dict[str, str]:
        headers = auth_headers(self._spec, self._environ)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = _ACCEPT
        with self._state_lock:
            if self._session_id:
                headers[SESSION_HEADER] = self._session_id
            if self._protocol:
                headers[PROTOCOL_HEADER] = self._protocol
        return headers

    def _relay(self, text: str, *, initialize: bool) -> None:
        if initialize:
            try:
                result = json.loads(text).get("result") or {}
            except (ValueError, AttributeError):
                result = {}
            version = result.get("protocolVersion") if isinstance(result, dict) else None
            if isinstance(version, str) and version:
                with self._state_lock:
                    self._protocol = version
        self.write(text)

    def post(self, line: str) -> None:
        """Send one message from agy; write every message of the answer back to agy."""
        try:
            message = json.loads(line)
        except ValueError:
            print(
                f"skuld agy mcp: agy sent a line that is not JSON: {line[:200]!r}", file=sys.stderr
            )
            return
        request_id = message.get("id") if isinstance(message, dict) else None
        initialize = isinstance(message, dict) and message.get("method") == "initialize"
        try:
            request = urllib.request.Request(
                str(self._spec["url"]), data=line.encode("utf-8"), headers=self._headers()
            )
            with self._opener(request, self._timeout) as response:
                session = response.headers.get(SESSION_HEADER)
                if session:
                    with self._state_lock:
                        self._session_id = session
                kind = (response.headers.get("Content-Type") or "").split(";")[0].strip()
                if kind == "text/event-stream":
                    for data in sse_messages(response):
                        self._relay(data, initialize=initialize)
                    return
                body = response.read().decode("utf-8").strip()
            if not body:
                return
            parsed = json.loads(body)
            for item in parsed if isinstance(parsed, list) else [parsed]:
                self._relay(json.dumps(item), initialize=initialize)
        except urllib.error.HTTPError as exc:
            self._fail(request_id, f"{self._spec['url']} answered HTTP {exc.code} {exc.reason}")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            self._fail(request_id, f"could not reach {self._spec['url']}: {exc}")

    def _fail(self, request_id: Any, reason: str) -> None:
        print(f"skuld agy mcp: {reason}", file=sys.stderr)
        if request_id is None:
            return  # a notification has no one to answer
        self.write(
            json.dumps(
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "error": {"code": _BRIDGE_ERROR_CODE, "message": f"Skuld MCP bridge: {reason}"},
                }
            )
        )

    def run(self, lines: Iterable[bytes]) -> None:
        """Relay until agy closes stdin; requests run concurrently, like agy sends them."""
        workers: list[threading.Thread] = []
        for raw in lines:
            line = raw.decode("utf-8").strip()
            if not line:
                continue
            worker = threading.Thread(target=self.post, args=(line,), daemon=True)
            worker.start()
            workers.append(worker)
            workers = [w for w in workers if w.is_alive()]
        for worker in workers:
            worker.join()


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: agy-mcp <server-name>")
    environ = dict(os.environ)
    spec = resolve(args[0], environ)
    if spec.get("url"):
        HttpBridge(spec, environ, sys.stdout.buffer).run(sys.stdin.buffer)
        return
    command, env = stdio_command(spec, environ)
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    main()
