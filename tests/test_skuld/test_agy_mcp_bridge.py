"""The agy MCP launcher's stdio-to-streamable-HTTP bridge, against a real local HTTP server."""

from __future__ import annotations

import io
import json
import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest

from skuld.transports import agy_mcp_launcher
from skuld.transports.agy_mcp_launcher import HttpBridge, auth_headers, sse_messages


def _tools_reply(request_id: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": {"tools": [{"name": "list_issues"}]}}


class _Server:
    def __init__(self) -> None:
        self.requests: list[dict[str, Any]] = []
        self.reply: dict[str, Any] = {}


@pytest.fixture
def mcp_server() -> Iterator[tuple[str, _Server]]:
    state = _Server()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args: Any) -> None:
            return

        def do_POST(self) -> None:  # noqa: N802 - http.server's name
            body = self.rfile.read(int(self.headers["Content-Length"]))
            message = json.loads(body)
            state.requests.append(
                {"headers": {k.lower(): v for k, v in self.headers.items()}, "message": message}
            )
            method = message.get("method")
            if method == "notifications/initialized":
                self.send_response(202)
                self.end_headers()
                return
            if method == "denied":
                self.send_response(401, "Unauthorized")
                self.end_headers()
                return
            if method == "tools/list":
                events = (
                    'data: {"jsonrpc":"2.0","method":"notifications/progress","params":{}}\n\n'
                    f"data: {json.dumps(_tools_reply(message['id']))}\n\n"
                )
                payload = events.encode()
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            result = {"protocolVersion": "2025-06-18", "capabilities": {}}
            payload = json.dumps({"jsonrpc": "2.0", "id": message.get("id"), "result": result})
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Mcp-Session-Id", "sess-1")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload.encode())

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/mcp", state
    finally:
        server.shutdown()
        server.server_close()


def _lines(*messages: dict[str, Any]) -> list[bytes]:
    return [json.dumps(m).encode() + b"\n" for m in messages]


def _written(out: io.BytesIO) -> list[dict[str, Any]]:
    return [json.loads(line) for line in out.getvalue().decode().splitlines()]


def test_the_bridge_relays_a_session_with_its_token_session_id_and_protocol(
    mcp_server, tmp_path
) -> None:
    url, state = mcp_server
    token = tmp_path / "token"
    token.write_text("tok-1\n")
    spec = {"url": url, "credential_file": str(token), "auth_prefix": "Bearer "}
    out = io.BytesIO()
    bridge = HttpBridge(spec, {}, out)

    bridge.post(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}))
    bridge.run(
        _lines(
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
        )
    )
    token.write_text("tok-2")  # rotated: the next request reads the new one
    bridge.post(json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}))

    replies = _written(out)
    assert replies[0]["result"]["protocolVersion"] == "2025-06-18"
    assert replies[1]["method"] == "notifications/progress"
    assert replies[2]["result"]["tools"] == [{"name": "list_issues"}]
    first, second, third = state.requests
    assert first["headers"]["authorization"] == "Bearer tok-1"
    assert "mcp-session-id" not in first["headers"]
    assert second["headers"]["mcp-session-id"] == "sess-1"
    assert second["headers"]["mcp-protocol-version"] == "2025-06-18"
    assert third["headers"]["authorization"] == "Bearer tok-2"
    assert third["headers"]["accept"] == "application/json, text/event-stream"


def test_a_refused_request_is_answered_with_an_error_agy_can_show(mcp_server, capsys) -> None:
    url, _ = mcp_server
    out = io.BytesIO()

    HttpBridge({"url": url}, {}, out).post(
        json.dumps({"jsonrpc": "2.0", "id": 7, "method": "denied"})
    )

    (reply,) = _written(out)
    assert reply["id"] == 7
    assert "HTTP 401" in reply["error"]["message"]
    assert "HTTP 401" in capsys.readouterr().err


def test_an_unreachable_server_fails_the_request_and_a_notification_only_logs(capsys) -> None:
    out = io.BytesIO()
    bridge = HttpBridge({"url": "http://127.0.0.1:9/mcp"}, {}, out)

    bridge.post(json.dumps({"jsonrpc": "2.0", "method": "notifications/x"}))
    assert out.getvalue() == b""
    bridge.post(json.dumps({"jsonrpc": "2.0", "id": "a", "method": "ping"}))

    assert _written(out)[0]["error"]["message"].startswith("Skuld MCP bridge: could not reach")
    assert "could not reach" in capsys.readouterr().err


def test_a_missing_token_fails_the_request_not_the_bridge(tmp_path) -> None:
    out = io.BytesIO()
    spec = {"url": "http://127.0.0.1:9/mcp", "credential_file": str(tmp_path / "gone")}

    HttpBridge(spec, {}, out).post(json.dumps({"jsonrpc": "2.0", "id": 1, "method": "ping"}))

    assert "error" in _written(out)[0]


def test_a_line_that_is_not_json_is_reported(capsys) -> None:
    out = io.BytesIO()

    HttpBridge({"url": "http://x"}, {}, out).post("not json")

    assert out.getvalue() == b""
    assert "not JSON" in capsys.readouterr().err


def test_a_json_array_answer_is_written_one_message_per_line() -> None:
    class Response(io.BytesIO):
        headers = {"Content-Type": "application/json"}

        def __enter__(self) -> Response:
            return self

        def __exit__(self, *exc: object) -> None:
            return

    body = json.dumps(
        [{"jsonrpc": "2.0", "id": 1, "result": {}}, {"jsonrpc": "2.0", "id": 2, "result": {}}]
    )
    out = io.BytesIO()
    bridge = HttpBridge({"url": "http://x"}, {}, out, opener=lambda r, t: Response(body.encode()))

    bridge.post(json.dumps([{"jsonrpc": "2.0", "id": 1, "method": "a"}]))

    assert [m["id"] for m in _written(out)] == [1, 2]


def test_auth_headers_come_from_a_variable_an_oauth_document_or_a_bearer_env(tmp_path) -> None:
    assert auth_headers(
        {"credential_env": "T", "auth_header": "X-Key", "auth_prefix": ""}, {"T": "k"}
    ) == {"X-Key": "k"}
    future = (datetime.now(UTC) + timedelta(hours=1)).isoformat()
    oauth = tmp_path / "oauth"
    oauth.write_text(json.dumps({"access_token": "at", "expires_at": future}))
    spec = {"credential_file": str(oauth), "credential_format": "oauth"}
    assert auth_headers(spec, {}) == {"Authorization": "Bearer at"}
    assert auth_headers({"bearer_token_env_var": "B", "headers": {"h": "v"}}, {"B": "b"}) == {
        "h": "v",
        "Authorization": "Bearer b",
    }
    assert auth_headers({"bearer_token_env_var": "B", "headers": {"Authorization": "x"}}, {}) == {
        "Authorization": "x"
    }


@pytest.mark.parametrize(
    ("spec", "environ", "match"),
    [
        ({"credential_env": "T"}, {}, "missing or malformed"),
        ({"bearer_token_env_var": "B"}, {}, "B is not set"),
    ],
)
def test_a_missing_credential_is_refused(spec, environ, match) -> None:
    with pytest.raises(ValueError, match=match):
        auth_headers(spec, environ)


def test_an_expired_oauth_token_is_refused(tmp_path) -> None:
    oauth = tmp_path / "oauth"
    past = (datetime.now(UTC) - timedelta(minutes=1)).isoformat()
    oauth.write_text(json.dumps({"access_token": "at", "expires_at": past}))

    with pytest.raises(ValueError, match="expired"):
        auth_headers({"credential_file": str(oauth), "credential_format": "oauth"}, {})


def test_server_sent_events_are_split_and_comments_skipped() -> None:
    stream = [b": hi\n", b"event: message\n", b'data: {"a":\n', b"data: 1}\n", b"\n", b"data: x"]

    assert list(sse_messages(stream)) == ['{"a":\n1}', "x"]


def test_main_bridges_a_url_server(monkeypatch) -> None:
    servers = {"linear": {"url": "https://l"}}
    monkeypatch.setenv(agy_mcp_launcher.SERVERS_ENV, json.dumps(servers))
    ran: list[Any] = []
    monkeypatch.setattr(HttpBridge, "run", lambda self, lines: ran.append(self._spec))
    monkeypatch.setattr(agy_mcp_launcher.sys, "stdin", io.TextIOWrapper(io.BytesIO(b"")))

    agy_mcp_launcher.main(["linear"])

    assert ran == [{"url": "https://l"}]


def test_the_launcher_runs_on_the_system_python() -> None:
    source = Path(agy_mcp_launcher.__file__).read_text()

    imports = [line for line in source.splitlines() if line.startswith(("import ", "from "))]
    assert not [line for line in imports if "skuld" in line or "collections.abc" in line]
    assert "from datetime import datetime, timezone  # noqa: UP017" in imports
