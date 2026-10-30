"""Guild facade: push device registration and the push routes reach the push node."""

from __future__ import annotations

from typing import Any

import pytest
import respx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from httpx import Response

from niuu.domain.services.forge_session_policy import policy_refusal
from niuu.domain.services.token_scope import (
    FORGE_NOTIFY_SCOPE,
    FORGE_SESSION_LIFECYCLE_SCOPE,
    FORGE_SESSION_MESSAGE_SCOPE,
    FORGE_SESSION_READ_SCOPE,
)
from tests.test_niuu.test_rest_volundr import _client, _headers, _instance

KNOWN_FORGE_SESSION_SCOPES = (
    FORGE_NOTIFY_SCOPE,
    FORGE_SESSION_LIFECYCLE_SCOPE,
    FORGE_SESSION_MESSAGE_SCOPE,
    FORGE_SESSION_READ_SCOPE,
)

URL = "/api/v1/forge/devices"
LOCAL = _instance(
    "local", base_url="embedded://local", is_default=True, config={"transport": "embedded"}
)
REMOTE = _instance("remote", base_url="http://remote")
BODY = {
    "platform": "ios",
    "token": "0a1b2c3d",
    "kind": "live_activity",
    "environment": "sandbox",
    "app_bundle_id": "com.xteo.lexi.chat",
    "activity_id": "ACT-1",
}


def _embedded(calls: list[dict[str, Any]], *, push: bool = True) -> FastAPI:
    """A Forge node's device and push routes, recording what the facade forwarded."""
    app = FastAPI()

    def record(request: Request, **extra: Any) -> None:
        calls.append(
            {
                "method": request.method,
                "path": request.url.path,
                "query": dict(request.query_params),
                "user": request.headers.get("x-auth-user-id"),
                "tenant": request.headers.get("x-auth-tenant"),
                "authorization": request.headers.get("authorization"),
                **extra,
            }
        )

    @app.post("/api/v1/forge/devices", status_code=201)
    async def register(request: Request, body: dict):
        record(request, body=body)
        if not push:
            return JSONResponse({"detail": "Push is not enabled on this Forge"}, status_code=503)
        return {"id": "d1", **body}

    @app.get("/api/v1/forge/devices")
    async def listing(request: Request):
        record(request)
        return [{"id": "d1", "token": "0a1b2c3d", "kind": "alert"}]

    @app.delete("/api/v1/forge/devices/{token:path}", status_code=204)
    async def unregister(request: Request, token: str):
        record(request, token=token)
        return None

    @app.get("/api/v1/forge/push/status")
    async def status(request: Request):
        record(request)
        return {"running": True, "alerts_sent": 3}

    @app.post("/api/v1/forge/push/test")
    async def test_push(request: Request, body: dict):
        record(request, body=body)
        if body.get("kind") == "widget":
            return JSONResponse({"detail": "You have no registered widget token"}, status_code=404)
        return {"kind": body.get("kind", "alert"), "sent": 1, "accepted": 1, "devices": []}

    return app


def test_register_is_forwarded_to_the_default_node_with_the_callers_identity():
    calls: list[dict[str, Any]] = []
    client = _client([LOCAL, REMOTE], embedded_forge_app=_embedded(calls))

    response = client.post(URL, json=BODY, headers=_headers())

    assert response.status_code == 201
    assert response.json() == {
        "id": "d1",
        **BODY,
        "instance_id": "local",
        "instance_name": "Instance local",
        "instance_slug": "local",
    }
    assert calls == [
        {
            "method": "POST",
            "path": URL,
            "query": {},
            "user": "user-a",
            "tenant": "tenant-a",
            "authorization": "Bearer test-token",
            "body": BODY,
        }
    ]


def test_list_is_forwarded_and_tagged_with_the_node():
    calls: list[dict[str, Any]] = []
    client = _client([LOCAL], embedded_forge_app=_embedded(calls))

    response = client.get(URL, headers=_headers())

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "d1",
            "token": "0a1b2c3d",
            "kind": "alert",
            "instance_id": "local",
            "instance_name": "Instance local",
            "instance_slug": "local",
        }
    ]
    assert calls[0]["method"] == "GET" and calls[0]["user"] == "user-a"


def test_unregister_forwards_the_token_and_its_kind():
    calls: list[dict[str, Any]] = []
    client = _client([LOCAL], embedded_forge_app=_embedded(calls))

    response = client.delete(f"{URL}/0a1b2c3d", params={"kind": "widget"}, headers=_headers())

    assert response.status_code == 204 and response.content == b""
    assert calls[0]["method"] == "DELETE"
    assert calls[0]["path"] == f"{URL}/0a1b2c3d"
    assert calls[0]["token"] == "0a1b2c3d"
    assert calls[0]["query"] == {"kind": "widget"}


def test_a_token_with_reserved_characters_stays_one_path_segment():
    calls: list[dict[str, Any]] = []
    client = _client([LOCAL], embedded_forge_app=_embedded(calls))

    client.delete(f"{URL}/endpoint%3Fa%3Db", headers=_headers())

    assert calls[0]["token"] == "endpoint?a=b"
    assert calls[0]["query"] == {}


@respx.mock
def test_instance_id_selects_another_node_and_is_not_forwarded():
    route = respx.post("http://remote/api/v1/forge/devices").mock(
        return_value=Response(201, json={"id": "d9", "token": "0a1b2c3d"})
    )
    calls: list[dict[str, Any]] = []
    client = _client([LOCAL, REMOTE], embedded_forge_app=_embedded(calls))

    response = client.post(URL, params={"instance_id": "remote"}, json=BODY, headers=_headers())

    assert response.status_code == 201
    assert response.json()["instance_id"] == "remote"
    assert calls == []
    request = route.calls.last.request
    assert request.url.query == b""
    assert request.headers["x-auth-user-id"] == "user-a"


def test_a_node_without_push_answers_503_through_the_facade():
    calls: list[dict[str, Any]] = []
    client = _client([LOCAL], embedded_forge_app=_embedded(calls, push=False))

    response = client.post(URL, json=BODY, headers=_headers())

    assert response.status_code == 503
    assert "Push is not enabled on this Forge" in response.json()["detail"]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("POST", URL),
        ("GET", URL),
        ("DELETE", f"{URL}/0a1b2c3d"),
        ("GET", "/api/v1/forge/push/status"),
        ("POST", "/api/v1/forge/push/test"),
    ],
)
def test_a_session_credential_may_not_use_the_device_or_push_routes(method, path):
    # An agent's own session token never registers a device or triggers a push:
    # the facade's edge guard (and Forge) only admit the allow-listed routes.
    assert policy_refusal(method, path, tuple(KNOWN_FORGE_SESSION_SCOPES)) is not None


def test_push_status_and_test_push_reach_the_push_node():
    calls: list[dict[str, Any]] = []
    client = _client([LOCAL, REMOTE], embedded_forge_app=_embedded(calls))

    status = client.get("/api/v1/forge/push/status", headers=_headers())
    sent = client.post("/api/v1/forge/push/test", json={"kind": "alert"}, headers=_headers())
    default = client.post("/api/v1/forge/push/test", headers=_headers())
    missing = client.post("/api/v1/forge/push/test", json={"kind": "widget"}, headers=_headers())

    assert status.json()["alerts_sent"] == 3 and status.json()["instance_id"] == "local"
    assert sent.status_code == 200 and sent.json()["accepted"] == 1
    assert default.status_code == 200
    assert missing.status_code == 404
    assert [call["path"] for call in calls] == [
        "/api/v1/forge/push/status",
        "/api/v1/forge/push/test",
        "/api/v1/forge/push/test",
        "/api/v1/forge/push/test",
    ]
    assert calls[1]["body"] == {"kind": "alert"} and calls[2]["body"] == {}
    assert {call["user"] for call in calls} == {"user-a"}
