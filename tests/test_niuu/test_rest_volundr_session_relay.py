"""LEX-1710: a session messages its parent or project on another Guild node.

The session's own node verifies its credential and relays the message without it,
attesting the sender in the body; the owning node checks the relation and delivers.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from uuid import uuid4

import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient
from httpx import Response

from identity.adapters.identity import AllowAllIdentityAdapter
from niuu.adapters.inbound.rest_volundr import create_volundr_router
from niuu.domain.guild_membership import GuildMembership
from tests.support.forge_session import TENANT, bearer, build_forge_app
from tests.test_niuu.test_rest_volundr import StubInstanceService, _instance
from volundr.domain.projects import SessionCoordination

REMOTE = "https://remote.example"
GUILD = "https://guild.example"
PROJECT = uuid4()
TARGET = "00000000-0000-4000-8000-0000000000aa"
DELIVERED = {"status": "delivered", "delivery": "delivered", "request_id": "r1"}


async def _guild(*, membership: GuildMembership | None = None, with_remote: bool = True):
    forge = build_forge_app()
    local = _instance(
        "local",
        base_url="embedded://local-forge",
        tenant_id=TENANT,
        is_default=True,
        config={"transport": "embedded"},
    )
    instances = [local]
    if with_remote:
        instances.append(_instance("remote", base_url=REMOTE, tenant_id=TENANT))
    app = FastAPI()
    app.state.identity = AllowAllIdentityAdapter(user_repository=AsyncMock())
    app.include_router(
        create_volundr_router(
            StubInstanceService(instances),
            embedded_forge_app=forge.app,
            guild_membership=membership,
        )
    )
    sender = await forge.session(
        "voice",
        workload_config={"forge_mcp": {"grants": ["message"]}},
        coordination=SessionCoordination(project_id=PROJECT, role="worker"),
    )
    credential = await forge.start(sender)
    return TestClient(app), sender, bearer(credential.token)


class TestRelayToAnotherNode:
    @respx.mock
    async def test_a_registry_node_gets_the_relay_without_the_session_credential(self):
        client, sender, auth = await _guild()
        respx.get(f"{REMOTE}/api/v1/forge/sessions/{TARGET}").mock(
            return_value=Response(200, json={"id": TARGET})
        )
        relayed = respx.post(f"{REMOTE}/api/v1/forge/sessions/{TARGET}/messages/relayed").mock(
            return_value=Response(200, json=DELIVERED)
        )

        response = client.post(
            f"/api/v1/forge/sessions/{TARGET}/messages",
            json={"content": "hotfix landed", "request_id": "r1"},
            headers=auth,
        )

        assert response.status_code == 200 and response.json()["delivery"] == "delivered"
        request = relayed.calls.last.request
        assert "authorization" not in request.headers
        body = json.loads(request.content)
        assert body["content"] == "hotfix landed" and body["request_id"] == "r1"
        assert body["sender"] == {
            "session_id": str(sender.id),
            "session_name": "voice",
            "instance_name": "Instance local",
            "project_id": str(PROJECT),
            "parent_session_id": None,
        }

    @respx.mock
    async def test_a_session_no_registry_node_knows_goes_to_the_hosts_guild(self):
        membership = GuildMembership(guild_url=GUILD, instance=None)
        client, _, auth = await _guild(membership=membership, with_remote=False)
        relayed = respx.post(f"{GUILD}/api/v1/forge/sessions/{TARGET}/messages/relayed").mock(
            return_value=Response(200, json=DELIVERED)
        )

        response = client.post(
            f"/api/v1/forge/sessions/{TARGET}/messages", json={"content": "hi"}, headers=auth
        )

        assert response.status_code == 200
        assert "authorization" not in relayed.calls.last.request.headers

    async def test_without_a_guild_the_answer_says_how_to_reach_other_nodes(self):
        client, _, auth = await _guild(with_remote=False)

        response = client.post(
            f"/api/v1/forge/sessions/{TARGET}/messages", json={"content": "hi"}, headers=auth
        )

        assert response.status_code == 404
        assert "set guild.url" in response.json()["detail"]

    @respx.mock
    async def test_the_owning_nodes_refusal_keeps_its_reason(self):
        client, _, auth = await _guild()
        respx.get(f"{REMOTE}/api/v1/forge/sessions/{TARGET}").mock(
            return_value=Response(200, json={"id": TARGET})
        )
        reason = "Session x is not in the sender's project (p) and is not its parent"
        respx.post(f"{REMOTE}/api/v1/forge/sessions/{TARGET}/messages/relayed").mock(
            return_value=Response(403, json={"detail": reason})
        )

        response = client.post(
            f"/api/v1/forge/sessions/{TARGET}/messages", json={"content": "hi"}, headers=auth
        )

        assert response.status_code == 403
        assert reason in response.json()["detail"]

    @respx.mock
    async def test_a_node_that_requires_a_login_is_refused_clearly(self):
        client, _, auth = await _guild()
        respx.get(f"{REMOTE}/api/v1/forge/sessions/{TARGET}").mock(
            return_value=Response(401, json={"detail": "Not authenticated"})
        )

        response = client.post(
            f"/api/v1/forge/sessions/{TARGET}/messages", json={"content": "hi"}, headers=auth
        )

        assert response.status_code == 403
        assert "requires a login" in response.json()["detail"]
        assert "node credential" in response.json()["detail"]

    async def test_a_session_credential_cannot_call_the_relay_route(self):
        client, _, auth = await _guild()

        response = client.post(
            f"/api/v1/forge/sessions/{TARGET}/messages/relayed",
            json={"content": "hi", "sender": {}},
            headers=auth,
        )

        assert response.status_code == 403
