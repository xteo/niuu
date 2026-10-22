"""LEX-1406: durable host-local side-quest defaults through ordinary project updates."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from uuid import uuid4

import httpx
import pytest
import respx
from fastapi import FastAPI, Request

from niuu.domain.models import Principal
from tests.test_niuu.test_rest_volundr import _client, _headers, _instance
from tests.test_projects.conftest import MemoryProjects
from volundr.adapters.inbound.rest_projects import create_projects_router
from volundr.adapters.outbound.postgres_projects import PostgresProjectRepository
from volundr.adapters.outbound.project_workspace import GitProjectWorkspace
from volundr.domain.project_ports import ProjectConflictError
from volundr.domain.projects import ForgeProject
from volundr.domain.services.projects import ProjectService

HOST = "thor-exp"
LOCAL = "315df5b7-05b2-4258-ae5b-fd86fa44ca95"
PRINCIPAL = Principal(user_id="dev-user", tenant_id="default", email="dev@example.test", roles=[])


def project_api(tmp_path):
    workspace = GitProjectWorkspace(allowed_prefixes=[str(tmp_path)], local_mounts_enabled=True)
    service = ProjectService(MemoryProjects(), workspace, SimpleNamespace(), instance_id=HOST)
    app = FastAPI()

    async def principal(_request):
        return PRINCIPAL

    app.include_router(create_projects_router(service, principal), prefix="/api/v1/forge")
    return app, service


def defaults(path, **extra):
    return {"host": HOST, "local_path": str(path), **extra}


def test_serializer_preserves_missing_null_and_object():
    plain = ForgeProject(name="Legacy")
    assert "side_quest_defaults" not in plain.model_dump()
    assert "side_quest_defaults" not in json.loads(plain.document_json())
    for value in (None, defaults("/workspaces/project")):
        saved = ForgeProject(name="Configured", side_quest_defaults=value)
        assert saved.model_dump()["side_quest_defaults"] == value
        restored = ForgeProject.model_validate_json(saved.document_json())
        assert restored.model_dump()["side_quest_defaults"] == value
        assert "side_quest_defaults" in restored.model_fields_set


async def test_registration_retry_cannot_silently_ignore_explicit_defaults():
    original = ForgeProject(name="Legacy")
    pool = SimpleNamespace(fetchrow=AsyncMock(return_value=None), get_max_size=lambda: 2)
    repository = PostgresProjectRepository(pool)
    repository.get = AsyncMock(return_value=original)
    assert await repository.register(original) == original
    with pytest.raises(ProjectConflictError):
        await repository.register(original.model_copy(update={"side_quest_defaults": None}))
    cleared = original.model_copy(update={"side_quest_defaults": None})
    repository.get.return_value = cleared
    assert await repository.register(cleared) == cleared
    # Older clients omitting the new field retain existing idempotent registration.
    assert await repository.register(original) == cleared


async def test_real_patch_get_clear_and_unrelated_update_preserve_semantics(tmp_path):
    folder = tmp_path / "scratch"
    folder.mkdir()
    app, _ = project_api(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as api:
        created = await api.post("/api/v1/forge/projects", json={"name": "Plain project"})
        assert created.status_code == 201
        path = f"/api/v1/forge/projects/{created.json()['id']}"
        assert "side_quest_defaults" not in created.json()
        renamed = await api.patch(path, json={"revision": 1, "name": "Renamed"})
        assert renamed.status_code == 200
        assert "side_quest_defaults" not in renamed.json()
        saved = await api.patch(path, json={"revision": 2, "side_quest_defaults": defaults(folder)})
        assert saved.status_code == 200
        assert saved.json()["revision"] == 3
        assert saved.json()["repo_url"] == saved.json()["workspace_path"] == ""
        assert saved.json()["side_quest_defaults"] == defaults(folder)
        assert (await api.get(path)).json()["side_quest_defaults"] == defaults(folder)
        stale = await api.patch(path, json={"revision": 2, "side_quest_defaults": None})
        assert stale.status_code == 409
        assert (await api.get(path)).json()["side_quest_defaults"] == defaults(folder)
        cleared = await api.patch(path, json={"revision": 3, "side_quest_defaults": None})
        assert cleared.status_code == 200 and cleared.json()["revision"] == 4
        assert cleared.json()["side_quest_defaults"] is None
        updated = await api.patch(path, json={"revision": 4, "brief": "Keep clear"})
        assert updated.status_code == 200 and updated.json()["side_quest_defaults"] is None
        assert (await api.get(path)).json()["side_quest_defaults"] is None


async def test_concurrent_updates_compare_and_swap_without_losing_other_fields(tmp_path):
    _, service = project_api(tmp_path)
    project = await service.register(ForgeProject(name="Original", brief="Keep"), PRINCIPAL)
    writes = [
        {"side_quest_defaults": defaults(tmp_path)},
        {"side_quest_defaults": None},
    ]
    results = await asyncio.gather(
        *(service.update(project.id, change, 1, PRINCIPAL) for change in writes),
        return_exceptions=True,
    )
    assert sum(isinstance(row, ForgeProject) for row in results) == 1
    assert sum(isinstance(row, ProjectConflictError) for row in results) == 1
    stored = await service.get(project.id, PRINCIPAL)
    assert stored.name == "Original" and stored.brief == "Keep" and stored.revision == 2


@pytest.mark.parametrize("value", [None, {"host": HOST, "local_path": "/unused"}])
async def test_replicas_cannot_set_or_clear_defaults(tmp_path, value):
    _, service = project_api(tmp_path)
    project = await service.register(
        ForgeProject(name="Replica", home_instance_id="elsewhere"), PRINCIPAL
    )
    with pytest.raises(ProjectConflictError, match="home host"):
        await service.update(project.id, {"side_quest_defaults": value}, 1, PRINCIPAL)
    with pytest.raises(ProjectConflictError, match="home host"):
        await service.register(
            ForgeProject(name="Replica", home_instance_id="elsewhere", side_quest_defaults=value),
            PRINCIPAL,
        )


@pytest.mark.parametrize(
    "value",
    [
        {"host": "production", "local_path": "/unused"},
        {"host": HOST, "local_path": "relative"},
        {"host": HOST, "local_path": "/contains\ncontrol"},
        {"host": HOST, "local_path": "/contains\x7fcontrol"},
        {"host": HOST, "local_path": "/back\\slash"},
        {"host": HOST, "local_path": ""},
        {"host": HOST, "local_path": "/unused", "url": "https://elsewhere.test"},
    ],
)
async def test_api_rejects_invalid_defaults_for_create_and_patch(tmp_path, value):
    app, _ = project_api(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as api:
        project = (await api.post("/api/v1/forge/projects", json={"name": "Project"})).json()
        bad_create = await api.post(
            "/api/v1/forge/projects", json={"name": "Invalid", "side_quest_defaults": value}
        )
        bad_patch = await api.patch(
            f"/api/v1/forge/projects/{project['id']}",
            json={"revision": 1, "side_quest_defaults": value},
        )
    assert bad_create.status_code == bad_patch.status_code == 422


async def test_mount_policy_checks_existence_symlinks_and_disabled_mounts(tmp_path):
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    (allowed / "escape").symlink_to(outside, target_is_directory=True)
    adapter = GitProjectWorkspace(allowed_prefixes=[str(allowed)], local_mounts_enabled=True)
    assert await adapter.validate_local_path(str(allowed)) == str(allowed)
    for path in (outside, allowed / "escape", allowed / "missing", "/"):
        with pytest.raises(ValueError):
            await adapter.validate_local_path(str(path))
    with pytest.raises(ValueError, match="disabled"):
        await GitProjectWorkspace(allowed_prefixes=[str(allowed)]).validate_local_path(str(allowed))
    (allowed / "inside").symlink_to(allowed, target_is_directory=True)
    assert await adapter.validate_local_path(str(allowed / "inside")) == str(allowed)


@pytest.mark.parametrize(
    "invalid_target", ["too_long", "back\\slash", "line\nbreak", "del\x7fchar"]
)
async def test_api_revalidates_resolved_defaults_before_persisting(tmp_path, invalid_target):
    target = tmp_path / invalid_target
    if invalid_target == "too_long":
        while len(str(target)) <= 2048:
            target /= "nested" * 20
    target.mkdir(parents=True)
    alias = tmp_path / "valid-alias"
    alias.symlink_to(target, target_is_directory=True)
    value = defaults(alias)
    app, _ = project_api(tmp_path)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as api:
        project = (await api.post("/api/v1/forge/projects", json={"name": "Project"})).json()
        path = f"/api/v1/forge/projects/{project['id']}"
        bad_create = await api.post(
            "/api/v1/forge/projects", json={"name": "Invalid", "side_quest_defaults": value}
        )
        bad_patch = await api.patch(path, json={"revision": 1, "side_quest_defaults": value})
        assert bad_create.status_code == bad_patch.status_code == 422
        assert (await api.get(path)).json() == project
        assert (await api.get("/api/v1/forge/projects")).json() == [project]


async def test_ownership_still_enforced(tmp_path):
    _, service = project_api(tmp_path)
    project = await service.register(ForgeProject(name="Owned"), PRINCIPAL)
    stranger = replace(PRINCIPAL, user_id="stranger")
    with pytest.raises(LookupError):
        await service.update(project.id, {"side_quest_defaults": None}, 1, stranger)


def facade():
    calls = []
    embedded = FastAPI()

    @embedded.api_route("/api/v1/forge/projects", methods=["POST"])
    @embedded.api_route("/api/v1/forge/projects/{project_id}", methods=["PATCH"])
    async def update(request: Request, project_id: str = "new"):
        document = await request.json()
        calls.append(document)
        return {"id": project_id, **document}

    api = _client(
        [
            _instance("remote", base_url="http://remote.test", is_default=True),
            _instance(LOCAL, base_url="embedded://local-forge", config={"transport": "embedded"}),
        ],
        embedded_forge_app=embedded,
    )
    return api, calls


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@pytest.mark.parametrize("value", [None, {"host": HOST, "local_path": "/scratch"}])
@pytest.mark.parametrize(
    "selector",
    [
        ("query", "instance_id", "remote"),
        ("query", "instanceId", "remote"),
        ("body", "instance_id", "remote"),
        ("body", "instanceId", "remote"),
        ("query", "target_tags", "remote"),
        ("body", "target_match", "all"),
    ],
)
@respx.mock
def test_default_writes_refuse_remote_selectors_before_forwarding(method, value, selector):
    api, calls = facade()
    where, key, selected = selector
    body = {"revision": 1, "side_quest_defaults": value}
    params = {}
    (body if where == "body" else params)[key] = selected
    path = "/api/v1/forge/projects" + (f"/{uuid4()}" if method == "PATCH" else "")
    response = api.request(method, path, json=body, params=params, headers=_headers())
    assert response.status_code == 422
    assert calls == [] and len(respx.calls) == 0


@pytest.mark.parametrize("method", ["POST", "PATCH"])
@respx.mock
def test_default_write_uses_served_host_even_when_registry_default_is_remote(method):
    api, calls = facade()
    path = "/api/v1/forge/projects" + (f"/{uuid4()}" if method == "PATCH" else "")
    response = api.request(
        method, path, json={"revision": 1, "side_quest_defaults": None}, headers=_headers()
    )
    assert response.status_code in {200, 201}
    assert response.json()["instance_id"] == LOCAL
    assert calls == [{"revision": 1, "side_quest_defaults": None}]
    assert len(respx.calls) == 0


@respx.mock
def test_registry_shell_cannot_forward_default_write_without_served_forge():
    api = _client([_instance("remote", base_url="http://remote.test", is_default=True)])
    result = api.patch(
        f"/api/v1/forge/projects/{uuid4()}",
        json={"revision": 1, "side_quest_defaults": None},
        headers=_headers(),
    )
    assert result.status_code == 503 and len(respx.calls) == 0
