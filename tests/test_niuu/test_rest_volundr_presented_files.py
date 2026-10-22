"""F2 through the Guild facade (what RootServer serves on /api/v1/forge): the broker's
durable upload is a PUT that must reach the embedded Forge with its session credential,
and the durable copy then serves through the same facade after the session stops."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from identity.adapters.identity import AllowAllIdentityAdapter
from niuu.adapters.inbound.rest_volundr import create_volundr_router
from niuu.domain.presented_files import PRESENTED_FILE_NAME_HEADER
from tests.support.forge_session import OWNER, TENANT, bearer, build_forge_app
from tests.test_niuu.test_rest_volundr import StubInstanceService, _instance
from volundr.adapters.outbound.local_presented_files import LocalPresentedFileStore
from volundr.domain.models import SessionStatus

FID = "pf_" + "a" * 32
BASE = "http://thor.tail737f2a.ts.net:8380"
OWNER_HEADERS = {"x-auth-user-id": OWNER, "x-auth-tenant": TENANT}


@pytest.fixture
async def guild(tmp_path):
    store = LocalPresentedFileStore(root=str(tmp_path))
    forge = build_forge_app(presented_file_store=store)
    local = _instance(
        "local",
        base_url="embedded://local-forge",
        tenant_id=TENANT,
        is_default=True,
        config={"transport": "embedded"},
    )
    app = FastAPI()
    app.state.identity = AllowAllIdentityAdapter(user_repository=AsyncMock())
    app.include_router(
        create_volundr_router(StubInstanceService([local]), embedded_forge_app=forge.app)
    )
    session = await forge.session("self")
    credential = await forge.start(session)
    return TestClient(app), forge, session, bearer(credential.token), store


def _url(session_id, fid=FID) -> str:
    return f"/api/v1/forge/sessions/{session_id}/files/presented/{fid}"


async def test_broker_upload_through_the_facade_then_served_after_stop(guild) -> None:
    client, forge, session, auth, store = guild
    put = client.put(
        _url(session.id),
        content=b"hello durable",
        headers={**auth, "content-type": "text/plain", PRESENTED_FILE_NAME_HEADER: "notes.txt"},
    )
    assert put.status_code == 200, put.text
    body = put.json()
    assert body["url"] == f"{BASE}{_url(session.id)}"
    assert body["name"] == "notes.txt" and body["mime"] == "text/plain"
    record, _ = await store.get(session.id, FID)
    assert record.size == len(b"hello durable")

    await forge.set_status(session.id, SessionStatus.STOPPED)
    # The owner reads it (the phone on the tailnet, as dev-user in mini mode); no broker is running.
    got = client.get(_url(session.id), headers=OWNER_HEADERS)
    assert got.status_code == 200
    assert got.content == b"hello durable"
    assert got.headers["content-type"].startswith("text/plain")


async def test_forge_refusals_pass_through_unchanged(guild) -> None:
    client, forge, session, auth, _ = guild
    assert client.put(_url(session.id), content=b"one", headers=auth).status_code == 200
    assert client.put(_url(session.id), content=b"two", headers=auth).status_code == 409
    peer = await forge.session("peer")
    assert client.put(_url(peer.id), content=b"x", headers=auth).status_code == 403
