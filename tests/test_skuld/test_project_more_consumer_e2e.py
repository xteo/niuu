"""R17: real agent stdio MCP → real broker → actual CP auth/router/store.

Run with CP_PROJECT_MORE_FIXTURE_COMMAND set to the JSON argv for frontend's
views/__tests__/project-more-consumer.fixture.ts. No model, credentials, or live
services are needed. The sole test transport mapping preserves the fixed CP
request origin and forwards it to that fixture's ephemeral loopback listener.
"""

from __future__ import annotations

import base64
import json
import os
import re
import selectors
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from skuld.forge_mcp.integration import forge_mcp_command
from tests.test_skuld.test_forge_mcp_e2e import _clean_env, _free_port, _rpc

pytestmark = pytest.mark.integration

# Only this test launcher rewrites the pinned transport. There is no production
# endpoint override, test-mode environment switch, or credential in argv.
BROKER_TEST_LAUNCHER = """
import os, runpy
import httpx
original = httpx.AsyncHTTPTransport.handle_async_request
async def map_cp(self, request):
    assert request.url.scheme == 'http' and request.url.host == '10.89.0.21'
    assert request.url.port == 9600 and request.url.path == '/v1/cp/forge/project-more'
    assert request.method == 'GET'
    assert set(request.url.params) <= {'section', 'offset', 'sha'}
    request.url = request.url.copy_with(host='127.0.0.1', port=int(os.environ['CP_FIXTURE_PORT']))
    return await original(self, request)
httpx.AsyncHTTPTransport.handle_async_request = map_cp
runpy.run_module('skuld', run_name='__main__')
"""


def _fixture_ready(process: subprocess.Popen, timeout: float = 30.0) -> dict:
    assert process.stdout is not None
    with selectors.DefaultSelector() as selector:
        selector.register(process.stdout, selectors.EVENT_READ)
        assert selector.select(timeout), "CP fixture did not report readiness"
        line = process.stdout.readline()
    assert line, "CP fixture exited before readiness (see fixture log)"
    return json.loads(line)


def test_seeded_agent_reads_omitted_question_through_real_mcp_and_cp(tmp_path: Path) -> None:
    configured = os.environ.get("CP_PROJECT_MORE_FIXTURE_COMMAND")
    if not configured:
        pytest.skip("requires the frontend project-more-consumer.fixture.ts command")
    command = json.loads(configured)
    assert isinstance(command, list) and all(isinstance(arg, str) for arg in command)
    fixture_log = (tmp_path / "cp-fixture.log").open("w")
    fixture = subprocess.Popen(
        command, stdout=subprocess.PIPE, stderr=fixture_log, env=_clean_env(), cwd=tmp_path
    )
    broker = None
    stdio = None
    broker_log = None
    try:
        ready = _fixture_ready(fixture)
        assert ready["omitted_text"] not in ready["seed"]
        advertised = re.findall(r"project_more section=([a-z_]+)", ready["seed"])
        assert "questions" in advertised
        section = advertised[advertised.index("questions")]
        claims = {"token_use": "forge_session", "workload_session_id": ready["session_id"]}
        payload = base64.urlsafe_b64encode(json.dumps(claims, separators=(",", ":")).encode())
        token = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            + payload.decode().rstrip("=")
            + ".Zml4dHVyZS1zaWduYXR1cmU"
        )
        port = _free_port()
        runtime = tmp_path / "runtime"
        workspace = tmp_path / "workspace"
        workspace.mkdir()
        env = _clean_env()
        env.update(
            {
                "SKULD__SESSION__ID": ready["session_id"],
                "SKULD__SESSION__NAME": "project-more-consumer-e2e",
                "SKULD__SESSION__WORKSPACE_DIR": str(workspace),
                "SKULD__HOST": "127.0.0.1",
                "SKULD__PORT": str(port),
                "SKULD__VOLUNDR_API_URL": "",
                "SKULD__EVENT_LOG_ENABLED": "false",
                "SKULD__TRANSPORT_ADAPTER": "skuld.transports.subprocess.SubprocessTransport",
                "SKULD__FORGE_MCP__RUNTIME_DIR": str(runtime),
                "SKULD__FORGE_MCP__PROJECT_MORE_ENABLED": "true",
                "SKULD__FORGE_MCP__TOKEN": token,
                "CP_FIXTURE_PORT": str(ready["port"]),
            }
        )
        broker_log = (tmp_path / "broker.log").open("w")
        broker = subprocess.Popen(
            [sys.executable, "-c", BROKER_TEST_LAUNCHER],
            cwd=workspace,
            env=env,
            stdout=broker_log,
            stderr=subprocess.STDOUT,
        )
        token_file = runtime / "forge-mcp.token"
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            assert broker.poll() is None, "broker exited (see broker log)"
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1):
                    if token_file.exists():
                        break
            except OSError:
                pass
            time.sleep(0.1)
        else:
            pytest.fail("broker did not become healthy")
        stdio = subprocess.Popen(
            [
                *forge_mcp_command(),
                "--broker-url",
                f"http://127.0.0.1:{port}",
                "--token-file",
                str(token_file),
                "--timeout",
                "10",
            ],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_clean_env(),
        )
        initialized = _rpc(
            stdio,
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "initialize",
                "params": {"protocolVersion": "2025-06-18", "capabilities": {}},
            },
        )
        assert initialized["result"]["serverInfo"]["name"] == "forge"
        listed = _rpc(stdio, {"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
        assert "project_more" in {tool["name"] for tool in listed["result"]["tools"]}
        pages = []
        cursor = {"section": section}
        for request_id in range(3, 23):
            response = _rpc(
                stdio,
                {
                    "jsonrpc": "2.0",
                    "id": request_id,
                    "method": "tools/call",
                    "params": {"name": "project_more", "arguments": cursor},
                },
            )["result"]
            assert response["isError"] is False, response
            page = json.loads(response["content"][0]["text"])
            assert page == response["structuredContent"]
            assert page["project_id"] == ready["project_id"]
            assert page["purpose"] == "side_quest"
            assert token not in json.dumps(response)
            pages.append(page["text"])
            if page["next_offset"] is None:
                break
            cursor = {"section": section, "offset": page["next_offset"], "sha": page["sha"]}
        else:
            pytest.fail("CP pagination did not terminate")
        assert ready["omitted_text"] in "".join(pages)
        assert len(pages) > 1
    finally:
        for process in (stdio, broker, fixture):
            if process is not None and process.poll() is None:
                process.terminate()
                process.wait(timeout=15)
        fixture_log.close()
        if broker_log is not None:
            broker_log.close()
