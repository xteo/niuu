"""R17: a seeded agent can read overflow through its authenticated broker MCP."""

from __future__ import annotations

import json
from uuid import uuid4

import httpx
import pytest

from niuu.forge_mcp.models import ForgeMcpNotifyMode
from niuu.forge_mcp.tools import ForgeMcpToolbox
from skuld.forge_mcp.project_more import EXP_PROJECT_MORE_URL
from skuld.transports.session_env import session_process_env
from tests.test_skuld.test_forge_mcp_broker import (
    FULL_TOKEN,
    _broker,
    _call,
    _client,
)

PROJECT_ID = str(uuid4())
PAGE = {
    "project_id": PROJECT_ID,
    "purpose": "side_quest",
    "section": "questions",
    "as_of_seq": 14,
    "text": "x" * 7999 + "🦊",
    "sha": "abc123def456",
    "offset": 0,
    "next_offset": 8000,
    "total_chars": 8007,
}


@pytest.fixture
def cp(monkeypatch):
    requests = []
    options = []
    responses = [httpx.Response(200, json=PAGE)]

    def handler(request):
        requests.append(request)
        response = responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response

    def client(**kwargs):
        options.append(kwargs)
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr("skuld.forge_mcp.project_more.AsyncClient", client)
    return requests, options, responses


def enabled_broker(tmp_path, **options):
    return _broker(
        tmp_path,
        None,
        project_more_enabled=True,
        token=FULL_TOKEN,
        **options,
    )


async def test_catalog_and_call_absent_without_opt_in(tmp_path):
    broker = _broker(tmp_path, None)
    async with _client(broker) as client:
        tools = (await client.get("/api/forge-mcp/tools")).json()["tools"]
        result = await client.post("/api/forge-mcp/tools/project_more", json={"section": "goal"})
    assert "project_more" not in {tool["name"] for tool in tools}
    assert result.status_code == 404


async def test_only_enabled_broker_serves_project_more(tmp_path):
    broker = enabled_broker(tmp_path)
    toolbox = broker._forge_mcp_toolbox()
    spec = next(tool for tool in toolbox.list_tools() if tool["name"] == "project_more")
    assert set(spec["inputSchema"]["properties"]) == {"section", "offset", "sha"}
    assert spec["inputSchema"]["additionalProperties"] is False
    # Even an accidental enable flag must not expose it from generic HTTP MCP.
    feed = ForgeMcpToolbox(
        client=toolbox._client,
        host=toolbox._host,
        grants=(),
        limits=toolbox._limits,
        notify_mode=ForgeMcpNotifyMode.FEED,
        project_more_enabled=True,
    )
    assert "project_more" not in {tool["name"] for tool in feed.list_tools()}


async def test_complete_page_and_cursor_use_fixed_exp_route_and_broker_token(tmp_path, cp):
    requests, options, responses = cp
    broker = enabled_broker(tmp_path, output_max_chars=1024)
    first = await _call(broker, "project_more", {"section": "questions"})
    assert first["isError"] is False
    assert first["structuredContent"] == PAGE
    # R17: models which consume only text must also see the whole page.
    assert json.loads(first["content"][0]["text"]) == PAGE
    responses.append(
        httpx.Response(
            200,
            json={
                **PAGE,
                "text": "OMITTED",
                "offset": 8000,
                "next_offset": None,
            },
        )
    )
    second = await _call(
        broker,
        "project_more",
        {
            "section": "questions",
            "offset": 8000,
            "sha": PAGE["sha"],
        },
    )
    assert second["structuredContent"]["text"] == "OMITTED"
    assert len(requests) == 2
    for request in requests:
        assert str(request.url).split("?")[0] == EXP_PROJECT_MORE_URL
        assert request.method == "GET"
        assert request.headers["authorization"] == f"Bearer {FULL_TOKEN}"
        assert "cookie" not in request.headers
    assert dict(requests[1].url.params) == {
        "section": "questions",
        "offset": "8000",
        "sha": PAGE["sha"],
    }
    assert all(o["trust_env"] is False and o["follow_redirects"] is False for o in options)
    assert FULL_TOKEN not in json.dumps(first)


@pytest.mark.parametrize(
    "args",
    [
        {"section": "goal", "project_id": PROJECT_ID},
        {"section": "goal", "url": "https://production.invalid"},
        {"section": "goal", "token": "arbitrary"},
        {"section": "goal", "purpose": "device"},
        {"section": "goal", "offset": -1},
        {"section": "goal", "offset": True},
        {"section": "goal", "offset": "0"},
        {"section": "goal", "sha": "bad"},
        {"section": "unknown"},
    ],
)
async def test_agent_cannot_route_authenticate_or_bypass_pagination(tmp_path, cp, args):
    result = await _call(enabled_broker(tmp_path), "project_more", args)
    assert result["isError"] is True
    assert not cp[0]


@pytest.mark.parametrize("token", ["", "service-credential", "not.a.jwt"])
async def test_no_fallback_to_broker_or_device_credentials(tmp_path, cp, token):
    broker = _broker(tmp_path, None, project_more_enabled=True, token=token)
    result = await _call(broker, "project_more", {"section": "goal"})
    assert result["isError"] is True
    assert not cp[0]


async def test_another_sessions_credential_refused_before_network(tmp_path, cp):
    broker = enabled_broker(tmp_path)
    broker.session_id = uuid4()
    result = await _call(broker, "project_more", {"section": "goal"})
    assert result["isError"] is True
    assert not cp[0]


@pytest.mark.parametrize("status", [301, 302, 307, 308, 401, 403, 404, 409, 500])
async def test_refusals_no_redirect_or_retry_or_response_diagnostics(tmp_path, cp, status):
    requests, _, responses = cp
    responses[:] = [
        httpx.Response(
            status, headers={"location": "https://production.invalid"}, json={"detail": FULL_TOKEN}
        )
    ]
    result = await _call(enabled_broker(tmp_path), "project_more", {"section": "questions"})
    assert result["isError"] is True
    assert FULL_TOKEN not in json.dumps(result)
    assert len(requests) == 1
    if status == 403:
        assert "Private" in result["content"][0]["text"]
    if status == 409:
        assert "restart at offset=0 without sha" in result["content"][0]["text"]


async def test_network_exception_is_sanitized(tmp_path, cp):
    cp[2][:] = [httpx.ConnectError(FULL_TOKEN)]
    result = await _call(enabled_broker(tmp_path), "project_more", {"section": "questions"})
    assert result["isError"] is True
    assert FULL_TOKEN not in json.dumps(result)


@pytest.mark.parametrize(
    "change",
    [
        {"project_id": "not-a-project-id"},
        {"purpose": "voice"},
        {"section": "goal"},
        {"offset": 3},
        {"text": "x" * 8001},
        {"text": ""},
        {"next_offset": 8001},
        {"total_chars": 10},
        {"sha": "wrong"},
        {"as_of_seq": "1"},
    ],
)
async def test_invalid_cp_page_fails_closed(tmp_path, cp, change):
    cp[2][:] = [httpx.Response(200, json={**PAGE, **change})]
    result = await _call(enabled_broker(tmp_path), "project_more", {"section": "questions"})
    assert result["isError"] is True
    assert result.get("structuredContent") is None


async def test_changed_sha_not_silently_mixed_with_earlier_page(tmp_path, cp):
    result = await _call(
        enabled_broker(tmp_path),
        "project_more",
        {
            "section": "questions",
            "sha": "0123456789ab",
        },
    )
    assert result["isError"] is True


async def test_new_private_policy_rechecked_instead_of_cached_content(tmp_path, cp):
    broker = enabled_broker(tmp_path)
    first = await _call(broker, "project_more", {"section": "questions"})
    assert first["isError"] is False
    cp[2].append(httpx.Response(403, json={"reason": "private", "purpose": "side_quest"}))
    second = await _call(broker, "project_more", {"section": "questions"})
    assert second["isError"] is True
    assert len(cp[0]) == 2


def test_credential_absent_from_agent_environment_and_injected_mcp(tmp_path, monkeypatch):
    monkeypatch.setenv("SKULD__FORGE_MCP__TOKEN", FULL_TOKEN)
    broker = enabled_broker(tmp_path)
    assert "SKULD__FORGE_MCP__TOKEN" not in session_process_env()
    assert FULL_TOKEN not in json.dumps(broker._builtin_mcp_servers())
    assert FULL_TOKEN not in broker._session_tools().instructions
