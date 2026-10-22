"""F8: the facade calls a host too old only when it refused a newer field as unknown.

Before this, any 422 from a host for an attach carrying ``role`` or ``parent`` became 501
"Update this session's Forge host", masking real refusals such as a parent outside the
project (reproduced through Thor@11afe41d → m6@6c31bfe1).
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import FastAPI
from fastapi.responses import JSONResponse

from tests.test_niuu.test_rest_volundr import _client, _headers, _instance

PREFIX = "/api/v1/forge"
LOCAL = _instance(
    "local", base_url="embedded://local", is_default=True, config={"transport": "embedded"}
)
SESSION = str(uuid4())
OLD_HOST_REJECTS_ROLE = {
    "detail": [
        {
            "type": "extra_forbidden",
            "loc": ["body", "role"],
            "msg": "Extra inputs are not permitted",
            "input": "worker",
        }
    ]
}
DOMAIN_REFUSAL = {"detail": "Referenced local session is not a project member"}
FIELD_ERROR = {
    "detail": [{"type": "missing", "loc": ["body", "expected_revision"], "msg": "Field required"}]
}


def _node(put_status: int, put_body: dict) -> FastAPI:
    app = FastAPI()

    @app.get(f"{PREFIX}/sessions/{{session_id}}")
    async def session(session_id: str):
        return {"id": session_id, "name": "s", "status": "running"}

    @app.get(f"{PREFIX}/sessions/{{session_id}}/project")
    async def current(session_id: str):
        return {"session_id": session_id, "revision": 0, "coordination": None}

    @app.put(f"{PREFIX}/sessions/{{session_id}}/project")
    async def assign(session_id: str):
        return JSONResponse(put_body, status_code=put_status)

    return app


def _put(node: FastAPI, **extra):
    client = _client([LOCAL], embedded_forge_app=node)
    body = {"project_id": None, "expected_revision": 0, **extra}
    return client.put(f"{PREFIX}/sessions/{SESSION}/project", json=body, headers=_headers())


def test_an_old_host_that_refuses_role_as_unknown_is_still_a_501():
    response = _put(_node(422, OLD_HOST_REJECTS_ROLE), role="worker")

    assert response.status_code == 501
    assert "Update this session's Forge host" in response.json()["detail"]


def test_a_domain_refusal_passes_through_as_the_hosts_own_422():
    response = _put(_node(422, DOMAIN_REFUSAL), role="side_quest")

    assert response.status_code == 422
    assert "not a project member" in response.json()["detail"]


def test_another_validation_error_is_not_called_an_old_host():
    response = _put(_node(422, FIELD_ERROR), role="worker")

    assert response.status_code == 422


def test_without_newer_fields_a_422_is_never_a_501():
    response = _put(_node(422, OLD_HOST_REJECTS_ROLE))

    assert response.status_code == 422


def test_a_successful_attach_still_carries_the_instance():
    response = _put(_node(200, {"session_id": SESSION, "revision": 1}), role="worker")

    assert response.status_code == 200
    assert response.json()["instance_id"] == "local"
