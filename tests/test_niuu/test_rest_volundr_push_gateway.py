"""Guild facade with a push gateway: alert tokens fan out, gateway-only kinds do not.

Every node pushes alerts to the devices registered with it, so an alert registration
reaches every visible node. Live Activity, push-to-start and widget tokens serve only
the push gateway, which runs on one node (the default, or the node ``instance_id``
names), so they register there alone; a node that keeps alert tokens only would
refuse them and fail the whole fan-out.
"""

from __future__ import annotations

from typing import Any

import respx
from fastapi import FastAPI, Request
from httpx import Response

from tests.test_niuu.test_rest_volundr import _client, _headers, _instance

URL = "/api/v1/forge/devices"
LOCAL = _instance(
    "local", base_url="embedded://local", is_default=True, config={"transport": "embedded"}
)
REMOTE = _instance("remote", base_url="http://remote")
WIDGET = {"platform": "ios", "token": "tok-w", "kind": "widget", "environment": "sandbox"}
ALERT = {"platform": "ios", "token": "tok-a", "kind": "alert"}


def _gateway_node(calls: list[dict[str, Any]]) -> FastAPI:
    app = FastAPI()

    @app.post("/api/v1/forge/devices", status_code=201)
    async def register(body: dict):
        calls.append({"method": "POST", "body": body})
        return {"id": "g1", **body}

    @app.get("/api/v1/forge/devices")
    async def listing():
        return [
            {"id": "g1", "token": "tok-1", "kind": "alert"},
            {"id": "g2", "token": "tok-1", "kind": "widget"},
        ]

    @app.delete("/api/v1/forge/devices/{token}", status_code=204)
    async def unregister(request: Request, token: str):
        calls.append({"method": "DELETE", "token": token, "query": dict(request.query_params)})
        return None

    @app.get("/api/v1/forge/push/status")
    async def push_status():
        calls.append({"method": "GET", "path": "/push/status"})
        return {"enabled": True}

    @app.post("/api/v1/forge/push/test")
    async def push_test(body: dict):
        calls.append({"method": "POST", "path": "/push/test", "body": body})
        return {"kind": body.get("kind", "alert"), "sent": 1}

    return app


@respx.mock
def test_a_gateway_only_kind_registers_on_the_gateway_node_alone():
    calls: list[dict[str, Any]] = []
    remote = respx.post("http://remote/api/v1/forge/devices").mock(
        return_value=Response(422, json={"detail": "alert tokens only"})
    )
    client = _client([LOCAL, REMOTE], embedded_forge_app=_gateway_node(calls))

    response = client.post(URL, json=WIDGET, headers=_headers())

    assert response.status_code == 201
    assert response.json()["kind"] == "widget"
    assert calls == [{"method": "POST", "body": WIDGET}]
    assert not remote.called


@respx.mock
def test_an_alert_token_still_fans_out_to_every_node():
    calls: list[dict[str, Any]] = []
    remote = respx.post("http://remote/api/v1/forge/devices").mock(
        return_value=Response(201, json={"id": "r1", **ALERT})
    )
    client = _client([LOCAL, REMOTE], embedded_forge_app=_gateway_node(calls))

    response = client.post(URL, json=ALERT, headers=_headers())

    assert response.status_code == 201
    assert calls == [{"method": "POST", "body": ALERT}]
    assert remote.called


def test_the_list_keeps_one_row_per_token_and_kind():
    client = _client([LOCAL], embedded_forge_app=_gateway_node([]))

    rows = client.get(URL, headers=_headers()).json()

    assert sorted((row["token"], row["kind"]) for row in rows) == [
        ("tok-1", "alert"),
        ("tok-1", "widget"),
    ]


@respx.mock
def test_removing_one_gateway_kind_goes_to_the_gateway_node_with_its_kind():
    calls: list[dict[str, Any]] = []
    remote = respx.delete("http://remote/api/v1/forge/devices/tok-w").mock(
        return_value=Response(204)
    )
    client = _client([LOCAL, REMOTE], embedded_forge_app=_gateway_node(calls))

    response = client.delete(f"{URL}/tok-w", params={"kind": "widget"}, headers=_headers())

    assert response.status_code == 204
    assert calls == [{"method": "DELETE", "token": "tok-w", "query": {"kind": "widget"}}]
    assert not remote.called


@respx.mock
def test_removing_the_alert_kind_fans_out_with_its_kind():
    calls: list[dict[str, Any]] = []
    remote = respx.delete("http://remote/api/v1/forge/devices/tok-a").mock(
        return_value=Response(204)
    )
    client = _client([LOCAL, REMOTE], embedded_forge_app=_gateway_node(calls))

    response = client.delete(f"{URL}/tok-a", params={"kind": "alert"}, headers=_headers())

    assert response.status_code == 204
    assert calls == [{"method": "DELETE", "token": "tok-a", "query": {"kind": "alert"}}]
    assert dict(remote.calls.last.request.url.params) == {"kind": "alert"}


def test_push_status_and_test_reach_the_gateway_node():
    calls: list[dict[str, Any]] = []
    client = _client([LOCAL], embedded_forge_app=_gateway_node(calls))

    assert client.get("/api/v1/forge/push/status", headers=_headers()).json()["enabled"]
    sent = client.post("/api/v1/forge/push/test", json={"kind": "alert"}, headers=_headers())

    assert sent.json()["sent"] == 1
    assert [call.get("path") for call in calls] == ["/push/status", "/push/test"]
