"""The agy command-hook bridge and MCP launcher, run as agy runs them."""

from __future__ import annotations

import io
import json
import socket
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from skuld.transports import agy_hook_bridge, agy_mcp_launcher


class _Broker(BaseHTTPRequestHandler):
    received: list[tuple[dict, str]] = []
    status = 200
    answer = {"decision": "allow"}

    def do_POST(self) -> None:  # noqa: N802 - http.server API
        body = json.loads(self.rfile.read(int(self.headers["content-length"])))
        type(self).received.append((body, self.headers["authorization"]))
        self.send_response(type(self).status)
        self.end_headers()
        self.wfile.write(json.dumps(type(self).answer).encode())

    def log_message(self, *args: object) -> None:
        return


def _closed_port_url() -> str:
    """A loopback URL nothing listens on (refused at once; port 9 may be dropped on CI)."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}/api/agy/hooks"


@pytest.fixture
def broker_url():
    _Broker.received = []
    _Broker.status = 200
    server = HTTPServer(("127.0.0.1", 0), _Broker)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_port}/api/agy/hooks"
    server.shutdown()
    server.server_close()


def _run(monkeypatch, capsys, event: str, payload: dict) -> str:
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    agy_hook_bridge.main([event])
    return capsys.readouterr().out


def test_a_hook_is_posted_with_the_secret_and_the_broker_answer_printed(
    broker_url, tmp_path, monkeypatch, capsys
) -> None:
    token = tmp_path / "token"
    token.write_text("s3cret\n")
    monkeypatch.setenv(agy_hook_bridge.HOOK_URL_ENV, broker_url)
    monkeypatch.setenv(agy_hook_bridge.BROKER_TOKEN_FILE_ENV, str(token))
    monkeypatch.setenv(agy_hook_bridge.HOOK_TIMEOUT_ENV, "5")

    out = _run(monkeypatch, capsys, "PreToolUse", {"toolCall": {"name": "run_command"}})

    assert json.loads(out) == {"decision": "allow"}
    assert _Broker.received == [
        ({"event": "PreToolUse", "payload": {"toolCall": {"name": "run_command"}}}, "Bearer s3cret")
    ]


def test_an_agy_not_started_by_a_broker_gets_agys_own_default(monkeypatch, capsys) -> None:
    monkeypatch.delenv(agy_hook_bridge.HOOK_URL_ENV, raising=False)

    assert json.loads(_run(monkeypatch, capsys, "PreToolUse", {})) == {"decision": "allow"}
    assert json.loads(_run(monkeypatch, capsys, "Stop", {})) == {"decision": "stop"}
    assert json.loads(_run(monkeypatch, capsys, "PostInvocation", {})) == {}


def test_a_refusing_broker_fails_the_hook_loudly(broker_url, tmp_path, monkeypatch, capsys) -> None:
    token = tmp_path / "token"
    token.write_text("s3cret")
    _Broker.status = 401
    monkeypatch.setenv(agy_hook_bridge.HOOK_URL_ENV, broker_url)
    monkeypatch.setenv(agy_hook_bridge.BROKER_TOKEN_FILE_ENV, str(token))

    with pytest.raises(SystemExit) as exit_info:
        _run(monkeypatch, capsys, "Stop", {})

    assert exit_info.value.code == agy_hook_bridge.EXIT_BROKER_UNREACHABLE
    assert "broker refused Stop: 401" in capsys.readouterr().err


def test_an_unreachable_broker_fails_the_hook_loudly(tmp_path, monkeypatch, capsys) -> None:
    token = tmp_path / "token"
    token.write_text("s3cret")
    monkeypatch.setenv(agy_hook_bridge.HOOK_URL_ENV, _closed_port_url())
    monkeypatch.setenv(agy_hook_bridge.BROKER_TOKEN_FILE_ENV, str(token))

    with pytest.raises(SystemExit) as exit_info:
        _run(monkeypatch, capsys, "Stop", {})

    assert exit_info.value.code == agy_hook_bridge.EXIT_BROKER_UNREACHABLE
    assert "broker unreachable for Stop" in capsys.readouterr().err


def test_a_broker_launch_without_the_token_file_fails(monkeypatch, capsys) -> None:
    monkeypatch.setenv(agy_hook_bridge.HOOK_URL_ENV, _closed_port_url())
    monkeypatch.delenv(agy_hook_bridge.BROKER_TOKEN_FILE_ENV, raising=False)

    with pytest.raises(SystemExit):
        _run(monkeypatch, capsys, "Stop", {})

    assert "FORGE_BROKER_TOKEN_FILE is not set" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["a", "b"]])
def test_the_bridge_needs_exactly_one_event(argv, capsys) -> None:
    with pytest.raises(SystemExit) as exit_info:
        agy_hook_bridge.main(argv)

    assert exit_info.value.code == agy_hook_bridge.EXIT_BAD_INPUT


def test_a_payload_that_is_not_json_is_refused(monkeypatch, capsys) -> None:
    monkeypatch.setattr("sys.stdin", io.StringIO("{not json"))

    with pytest.raises(SystemExit) as exit_info:
        agy_hook_bridge.main(["Stop"])

    assert exit_info.value.code == agy_hook_bridge.EXIT_BAD_INPUT


def test_the_launcher_resolves_the_sessions_own_server() -> None:
    servers = {"forge": {"command": "python3", "args": ["-m", "x", "--port", 1], "env": {"A": "b"}}}
    environ = {agy_mcp_launcher.SERVERS_ENV: json.dumps(servers), "PATH": "/bin"}

    argv, env = agy_mcp_launcher.stdio_command(agy_mcp_launcher.resolve("forge", environ), environ)

    assert argv == ["python3", "-m", "x", "--port", "1"]
    assert env["A"] == "b"
    assert env["PATH"] == "/bin"


def test_the_launcher_refuses_outside_a_forge_session() -> None:
    with pytest.raises(SystemExit, match="runs only in a Forge session"):
        agy_mcp_launcher.resolve("forge", {})


def test_the_launcher_refuses_a_server_the_session_does_not_have() -> None:
    with pytest.raises(SystemExit, match="no MCP server named 'mimir'"):
        agy_mcp_launcher.resolve("mimir", {agy_mcp_launcher.SERVERS_ENV: "{}"})


def test_the_launcher_execs_the_server(monkeypatch) -> None:
    servers = {"forge": {"command": "forge-mcp", "args": ["--x"]}}
    monkeypatch.setenv(agy_mcp_launcher.SERVERS_ENV, json.dumps(servers))
    calls = []
    monkeypatch.setattr(agy_mcp_launcher.os, "execvpe", lambda *a: calls.append(a))

    agy_mcp_launcher.main(["forge"])

    assert calls[0][0] == "forge-mcp"
    assert calls[0][1] == ["forge-mcp", "--x"]


def test_the_launcher_needs_one_server_name() -> None:
    with pytest.raises(SystemExit, match="usage"):
        agy_mcp_launcher.main([])
