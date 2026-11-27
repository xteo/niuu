"""LEX-2110 through Forge: ``POST /sessions/{id}/files/resolve`` reaches the session's broker,
its refusals arrive unchanged (never as a 502), and Forge and the Guild facade advertise
``resolve_path``."""

from __future__ import annotations

from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from identity.adapters.identity import AllowAllIdentityAdapter
from niuu.adapters.inbound.rest_volundr import create_volundr_router
from tests.test_niuu.test_rest_volundr import StubInstanceService, _instance
from tests.test_volundr.test_presented_files_durable import _forge_client
from volundr.adapters.outbound.local_presented_files import LocalPresentedFileStore
from volundr.domain.models import GitSource, Session, SessionStatus
from volundr.domain.presented_files import presented_file_path

FID = "pf_" + "b" * 32
RECORD = {
    "file_id": FID,
    "name": "research.md",
    "size": 12,
    "mime": "text/markdown",
    "path": "/home/agent/agent/health/research.md",
    "display_path": "~/agent/health/research.md",
    "host": "testhost",
    "source": "link",
}


async def _live_session(repository):
    session = Session(
        id=uuid4(),
        name="live",
        model="test",
        source=GitSource(repo="https://example.test/repo", branch="main"),
        status=SessionStatus.RUNNING,
        chat_endpoint="ws://broker.test:8080/session",
        code_endpoint="http://broker.test:8080/",
    )
    await repository.create(session)
    return session


@respx.mock
async def test_forge_forwards_the_path_and_answers_the_broker_record(tmp_path):
    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    route = respx.post("http://broker.test:8080/api/files/resolve").mock(
        return_value=httpx.Response(200, json=RECORD)
    )
    resp = client.post(
        f"/api/v1/forge/sessions/{session.id}/files/resolve",
        json={"path": "~/agent/health/research.md"},
    )
    assert resp.status_code == 200, resp.text
    assert resp.json() == RECORD
    assert route.calls.last.request.read() == b'{"path":"~/agent/health/research.md"}'


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (403, "/home/agent/.ssh/id_ed25519 is refused: link is a .ssh directory"),
        (403, "/etc/passwd is outside the session roots"),
        (404, "no such file on testhost: /home/agent/missing.md"),
        (413, "/home/agent/big.bin is 100 bytes, over max_presented_file_bytes=8"),
        (422, "cannot decode '~other/x': ~user paths are not supported"),
    ],
)
@respx.mock
async def test_forge_passes_broker_refusals_through(tmp_path, status, detail):
    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    respx.post("http://broker.test:8080/api/files/resolve").mock(
        return_value=httpx.Response(status, json={"detail": detail})
    )
    resp = client.post(f"/api/v1/forge/sessions/{session.id}/files/resolve", json={"path": "x"})
    assert resp.status_code == status
    assert resp.json()["detail"] == detail


@respx.mock
async def test_a_broker_failure_outside_the_contract_is_still_502(tmp_path):
    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    respx.post("http://broker.test:8080/api/files/resolve").mock(
        return_value=httpx.Response(500, text="boom")
    )
    resp = client.post(f"/api/v1/forge/sessions/{session.id}/files/resolve", json={"path": "x"})
    assert resp.status_code == 502


@respx.mock
async def test_a_changed_link_file_is_410_through_forge(tmp_path):
    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    respx.get(f"http://broker.test:8080/api/files/presented/{FID}").mock(
        return_value=httpx.Response(410, json={"detail": "file changed since the link"})
    )
    resp = client.get(presented_file_path(session.id, FID))
    assert resp.status_code == 410


def test_feature_flags_advertise_resolve_path(tmp_path):
    client, _ = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    client.app.state.storage = type("S", (), {"supports_home_volumes": False})()
    capabilities = client.get("/api/v1/forge/feature-flags").json()["capabilities"]
    assert capabilities["resolve_path"] is True
    assert capabilities["present_by_path"] is True


# ------------------------------------------------------------------ the Guild facade


def _facade(node: FastAPI) -> TestClient:
    local = _instance(
        "local",
        base_url="embedded://n",
        tenant_id="t1",
        is_default=True,
        config={"transport": "embedded"},
    )
    app = FastAPI()
    app.state.identity = AllowAllIdentityAdapter(user_repository=AsyncMock())
    app.include_router(create_volundr_router(StubInstanceService([local]), embedded_forge_app=node))
    return TestClient(app)


SID = "00000000-0000-4000-8000-000000000001"
HEADERS = {"x-auth-user-id": "u1", "x-auth-tenant": "t1"}


def _node(status: int, payload: dict) -> FastAPI:
    from fastapi.responses import JSONResponse

    node = FastAPI()

    @node.post("/api/v1/forge/sessions/{sid}/files/resolve")
    async def resolve(sid: str):
        return JSONResponse(payload, status_code=status)

    @node.get("/api/v1/forge/sessions/{sid}")
    async def session_row(sid: str):
        return {"id": sid, "name": "s", "owner_id": "u1", "tenant_id": "t1"}

    @node.get("/api/v1/forge/feature-flags")
    async def flags():
        return {"capabilities": {"resolve_path": True}}

    return node


def test_the_facade_forwards_resolve_and_its_refusals():
    ok = _facade(_node(200, RECORD)).post(
        f"/api/v1/forge/sessions/{SID}/files/resolve", json={"path": "~/a.md"}, headers=HEADERS
    )
    assert ok.status_code == 200, ok.text
    assert ok.json() == RECORD
    refused = _facade(_node(403, {"detail": "/etc/passwd is outside the session roots"})).post(
        f"/api/v1/forge/sessions/{SID}/files/resolve", json={"path": "/etc/passwd"}, headers=HEADERS
    )
    assert refused.status_code == 403
    assert refused.json()["detail"] == "/etc/passwd is outside the session roots"


def test_the_facade_mirrors_the_nodes_resolve_path_capability():
    caps = (
        _facade(_node(200, RECORD))
        .get("/api/v1/forge/feature-flags", headers=HEADERS)
        .json()["capabilities"]
    )
    assert caps["resolve_path"] is True
    assert caps["present_by_path"] is True


# ------------------------------------------------------------------ review round 1


@respx.mock
async def test_a_broker_without_the_route_is_501_not_a_missing_file(tmp_path):
    """r1 L3: an old broker's framework 404 must not read as "no such file"."""
    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    respx.post("http://broker.test:8080/api/files/resolve").mock(
        return_value=httpx.Response(404, json={"detail": "Not Found"})
    )
    resp = client.post(f"/api/v1/forge/sessions/{session.id}/files/resolve", json={"path": "x"})
    assert resp.status_code == 501
    assert resp.headers["x-forge-error-code"] == "resolve_path_unsupported"
    assert "capabilities.resolve_path" in resp.json()["detail"]


async def test_resolve_needs_the_session_to_exist(tmp_path):
    client, _ = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    resp = client.post(f"/api/v1/forge/sessions/{uuid4()}/files/resolve", json={"path": "x"})
    assert resp.status_code == 404


async def test_resolve_checks_access_to_the_session(tmp_path, monkeypatch):
    """r1 L1: Forge checks the caller may change the session before reading its host."""
    from volundr.domain.services.session import SessionAccessDeniedError

    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    seen: list[str] = []

    async def deny(self, session_, principal, action):
        seen.append(action)
        raise SessionAccessDeniedError(session_.id, "someone-else")

    from volundr.domain.services import SessionService

    monkeypatch.setattr(SessionService, "_check_access", deny)
    resp = client.post(f"/api/v1/forge/sessions/{session.id}/files/resolve", json={"path": "x"})
    assert resp.status_code == 403
    assert seen == ["update"]


@pytest.mark.parametrize(
    ("status", "detail"),
    [
        (403, "/home/agent/a.md is outside the session roots"),
        (403, "linked files open only for the session owner"),
        (413, "/home/agent/a.md is 100 bytes, over max_presented_file_bytes=8"),
    ],
)
@respx.mock
async def test_download_time_refusals_reach_the_caller(tmp_path, status, detail):
    """r1 P2c: refusals at download time are not turned into 502."""
    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    respx.get(f"http://broker.test:8080/api/files/presented/{FID}").mock(
        return_value=httpx.Response(status, json={"detail": detail})
    )
    resp = client.get(presented_file_path(session.id, FID))
    assert resp.status_code == status
    assert resp.json()["detail"] == detail


def test_the_facade_forwards_download_time_refusals():
    from fastapi.responses import JSONResponse

    node = _node(200, RECORD)

    @node.get("/api/v1/forge/sessions/{sid}/files/presented/{fid}")
    async def refused(sid: str, fid: str):
        return JSONResponse({"detail": "linked files open only for the session owner"}, 403)

    resp = _facade(node).get(f"/api/v1/forge/sessions/{SID}/files/presented/{FID}", headers=HEADERS)
    assert resp.status_code == 403
    assert resp.json()["detail"] == "linked files open only for the session owner"


def test_the_facade_forwards_the_501_from_an_old_broker():
    node = _node(501, {"detail": "this session's broker has no files/resolve"})
    resp = _facade(node).post(
        f"/api/v1/forge/sessions/{SID}/files/resolve", json={"path": "x"}, headers=HEADERS
    )
    assert resp.status_code == 501


# ------------------------------------------------------------------ review round 2


def _broker_gate(request: httpx.Request) -> httpx.Response:
    """The broker's own rule for a link id: owner only, judged by the role Forge stamps."""
    if request.headers.get("x-niuu-room-role") != "owner":
        return httpx.Response(403, json={"detail": "linked files open only for the session owner"})
    return httpx.Response(200, content=b"owner bytes", headers={"content-type": "text/plain"})


def _as_role(monkeypatch, role):
    from volundr.domain.services.session_participants import SessionParticipantService

    async def effective_room_role(self, session, principal):
        return role

    monkeypatch.setattr(SessionParticipantService, "effective_room_role", effective_room_role)


@respx.mock
async def test_r2_forge_stamps_the_callers_room_role_and_a_viewer_gets_403(tmp_path, monkeypatch):
    """r2 item 1: Forge dials the broker on loopback; without the stamped role the
    broker would take every caller for the owner."""
    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    route = respx.get(f"http://broker.test:8080/api/files/presented/{FID}").mock(
        side_effect=_broker_gate
    )
    _as_role(monkeypatch, "viewer")
    refused = client.get(presented_file_path(session.id, FID))
    assert refused.status_code == 403
    assert route.calls.last.request.headers["x-niuu-room-role"] == "viewer"
    _as_role(monkeypatch, "owner")
    served = client.get(presented_file_path(session.id, FID))
    assert served.status_code == 200
    assert served.content == b"owner bytes"


@respx.mock
async def test_r2_a_caller_with_no_room_role_never_reaches_the_broker(tmp_path, monkeypatch):
    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    route = respx.get(f"http://broker.test:8080/api/files/presented/{FID}").mock(
        side_effect=_broker_gate
    )
    _as_role(monkeypatch, None)
    assert client.get(presented_file_path(session.id, FID)).status_code == 403
    assert not route.called


@respx.mock
async def test_r2_a_client_cannot_forge_the_room_role_header(tmp_path, monkeypatch):
    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    route = respx.get(f"http://broker.test:8080/api/files/presented/{FID}").mock(
        side_effect=_broker_gate
    )
    _as_role(monkeypatch, "viewer")
    resp = client.get(presented_file_path(session.id, FID), headers={"x-niuu-room-role": "owner"})
    assert resp.status_code == 403
    assert route.calls.last.request.headers["x-niuu-room-role"] == "viewer"


@respx.mock
async def test_r2_resolve_stamps_the_room_role_too(tmp_path, monkeypatch):
    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    route = respx.post("http://broker.test:8080/api/files/resolve").mock(
        return_value=httpx.Response(200, json=RECORD)
    )
    _as_role(monkeypatch, "owner")
    resp = client.post(f"/api/v1/forge/sessions/{session.id}/files/resolve", json={"path": "x"})
    assert resp.status_code == 200
    assert route.calls.last.request.headers["x-niuu-room-role"] == "owner"


@respx.mock
async def test_r2_a_viewer_gets_403_through_the_guild_facade(tmp_path, monkeypatch):
    """The facade forwards to the session's node, which resolves the role and stamps it."""
    client, repository = _forge_client(LocalPresentedFileStore(root=str(tmp_path)))
    session = await _live_session(repository)
    respx.get(f"http://broker.test:8080/api/files/presented/{FID}").mock(side_effect=_broker_gate)
    facade = _facade(client.app)
    _as_role(monkeypatch, "viewer")
    refused = facade.get(
        f"/api/v1/forge/sessions/{session.id}/files/presented/{FID}", headers=HEADERS
    )
    assert refused.status_code == 403, refused.text
    assert refused.json()["detail"] == "linked files open only for the session owner"
    _as_role(monkeypatch, "owner")
    served = facade.get(
        f"/api/v1/forge/sessions/{session.id}/files/presented/{FID}", headers=HEADERS
    )
    assert served.status_code == 200, served.text
    assert served.content == b"owner bytes"


def test_r2_the_facade_passes_the_error_code_header():
    """r2 item 8: x-forge-error-code survives the facade for an old broker's 501."""
    from fastapi.responses import JSONResponse

    node = FastAPI()

    @node.post("/api/v1/forge/sessions/{sid}/files/resolve")
    async def old_broker(sid: str):
        return JSONResponse(
            {"detail": "this session's broker has no files/resolve"},
            status_code=501,
            headers={"x-forge-error-code": "resolve_path_unsupported"},
        )

    @node.get("/api/v1/forge/sessions/{sid}")
    async def session_row(sid: str):
        return {"id": sid, "name": "s", "owner_id": "u1", "tenant_id": "t1"}

    resp = _facade(node).post(
        f"/api/v1/forge/sessions/{SID}/files/resolve", json={"path": "x"}, headers=HEADERS
    )
    assert resp.status_code == 501
    assert resp.headers["x-forge-error-code"] == "resolve_path_unsupported"
