"""Real committed-document reader plus authenticated project API, no live services."""

import asyncio
import hashlib
import json
import subprocess
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest
from fastapi import FastAPI
from pydantic import ValidationError

from volundr.adapters.inbound.rest_projects import create_projects_router
from volundr.adapters.outbound.project_documents import GitProjectDocumentReader
from volundr.adapters.outbound.project_workspace import GitProjectWorkspace
from volundr.domain.models import Principal
from volundr.domain.project_ports import ProjectConflictError, ProjectDocumentNotFoundError
from volundr.domain.projects import ForgeProject, ProjectDocumentDescriptor


def commit(root):
    subprocess.run(["git", "-C", str(root), "add", "."], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Document QA",
            "-c",
            "user.email=qa@example.test",
            "commit",
            "-qm",
            "Save project records",
        ],
        check=True,
    )


@pytest.fixture
def published(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    project = ForgeProject(
        slug="records",
        name="Records",
        repo_url="https://example.test/project",
        workspace_path=str(root),
    )
    (root / "PROJECT.md").write_text("Project instructions, not a workflow engine.\n")
    (root / "notes.md").write_bytes("# Project notes\r\nExact café text.\n".encode())
    manifest = {
        "id": str(project.id),
        "documents": [{"id": "notes", "title": "Project notes", "path": "notes.md"}],
    }
    (root / "project.json").write_text(json.dumps(manifest))
    commit(root)
    return root, project, manifest


async def test_committed_snapshot_is_exact_and_dirty_files_never_override_it(published):
    root, project, _ = published
    workspace = GitProjectWorkspace(allowed_prefixes=[str(root.parent)])
    index = await workspace.documents(project)
    original = (root / "notes.md").read_bytes()
    (root / "notes.md").write_text("Unsaved edits must not be published")
    (root / "project.json").write_text("Uncommitted invalid manifest")
    doc = await workspace.read_document(project, "notes", index.revision)
    assert doc.revision == index.revision
    assert doc.content.encode() == original
    assert doc.byte_size == len(original)
    assert doc.sha256 == hashlib.sha256(original).hexdigest()
    assert doc.document.title == "Project notes"
    assert "author" not in doc.model_dump() and "status" not in doc.model_dump()
    assert (root / "notes.md").read_text() == "Unsaved edits must not be published"
    assert (
        subprocess.check_output(["git", "-C", str(root), "diff", "--cached", "--name-only"]) == b""
    )


async def test_revision_conflict_is_explicit_and_unpublished_id_is_not_a_path(published):
    root, project, _ = published
    workspace = GitProjectWorkspace()
    index = await workspace.documents(project)
    (root / "notes.md").write_text("Saved new notes")
    commit(root)
    with pytest.raises(ProjectConflictError, match="changed"):
        await workspace.read_document(project, "notes", index.revision)
    assert (await workspace.read_document(project, "notes")).content == "Saved new notes"
    for name in ["../notes.md", "notes.md", "missing", ".git/config"]:
        with pytest.raises(ProjectDocumentNotFoundError):
            await workspace.read_document(project, name)


@pytest.mark.parametrize(
    "path",
    [
        "/notes.md",
        "../notes.md",
        "a/../notes.md",
        "a//notes.md",
        "./notes.md",
        ".git/secret.md",
        "a/.GIT/secret.md",
        "a\\notes.md",
        ":(glob)*.md",
        "~/notes.md",
        "notes.md\0",
        "notes.md\n",
        "private.key",
        "a/./notes.md",
    ],
)
def test_descriptor_rejects_paths_with_special_meaning(path):
    with pytest.raises(ValidationError):
        ProjectDocumentDescriptor(id="notes", title="Notes", path=path)


@pytest.mark.parametrize(
    "change",
    [
        {"documents": None},
        {"documents": "notes.md"},
        {"documents": [{}]},
        {"documents": [{"id": "notes", "title": "\n", "path": "notes.md"}]},
        {"documents": [{"id": "notes", "title": "Notes", "path": "notes.md", "author": "human"}]},
        {"id": "other-project"},
    ],
)
async def test_invalid_committed_manifest_is_not_silently_ignored(published, change):
    root, project, manifest = published
    manifest.update(change)
    (root / "project.json").write_text(json.dumps(manifest))
    commit(root)
    with pytest.raises(ValueError):
        await GitProjectWorkspace().documents(project)


async def test_count_duplicate_paths_ids_and_manifest_budget(published):
    root, project, manifest = published
    manifest["documents"] *= 2
    (root / "project.json").write_text(json.dumps(manifest))
    commit(root)
    with pytest.raises(ValueError, match="count"):
        await GitProjectWorkspace(document_count=1).documents(project)
    with pytest.raises(ValueError, match="IDs"):
        await GitProjectWorkspace().documents(project)
    manifest["documents"][1] = {**manifest["documents"][1], "id": "another"}
    (root / "project.json").write_text(json.dumps(manifest))
    commit(root)
    with pytest.raises(ValueError, match="paths"):
        await GitProjectWorkspace().documents(project)
    with pytest.raises(ValueError, match="budget"):
        await GitProjectWorkspace(context_bytes=100).documents(project)


async def test_missing_unconfigured_manifest_or_documents_is_empty_not_a_workflow_default(
    published,
):
    root, project, manifest = published
    (root / "project.json").unlink()
    commit(root)
    assert (await GitProjectWorkspace().documents(project)).documents == []
    (root / "project.json").write_text(json.dumps({"id": manifest["id"]}))
    commit(root)
    assert (await GitProjectWorkspace().documents(project)).documents == []


@pytest.mark.parametrize("content", [b"\xff", b"text\0other", b"x" * 101])
async def test_binary_and_oversize_documents_are_errors_not_truncated_success(published, content):
    root, project, _ = published
    (root / "notes.md").write_bytes(content)
    commit(root)
    with pytest.raises(ValueError):
        await GitProjectWorkspace(document_bytes=100).read_document(project, "notes")


async def test_committed_symlinks_and_missing_files_are_rejected(published, tmp_path):
    root, project, _ = published
    secret = tmp_path / "private.md"
    secret.write_text("Do not publish")
    (root / "notes.md").unlink()
    (root / "notes.md").symlink_to(secret)
    commit(root)
    with pytest.raises(ValueError, match="regular"):
        await GitProjectWorkspace().read_document(project, "notes")
    (root / "notes.md").unlink()
    commit(root)
    with pytest.raises(ValueError, match="not committed"):
        await GitProjectWorkspace().read_document(project, "notes")
    (root / "project.json").unlink()
    (root / "project.json").symlink_to(secret)
    commit(root)
    with pytest.raises(ValueError, match="regular"):
        await GitProjectWorkspace().documents(project)


async def test_checkout_roots_are_still_enforced(published, tmp_path):
    _, project, _ = published
    workspace = GitProjectWorkspace(allowed_prefixes=[str(tmp_path / "elsewhere")])
    with pytest.raises(ValueError, match="outside"):
        await workspace.documents(project)
    with pytest.raises(ValueError, match="outside"):
        await workspace.read_document(project, "notes")


async def test_empty_text_is_a_valid_exact_document(published):
    root, project, _ = published
    (root / "notes.md").write_bytes(b"")
    commit(root)
    doc = await GitProjectWorkspace().read_document(project, "notes")
    assert doc.content == "" and doc.byte_size == 0
    assert doc.sha256 == hashlib.sha256(b"").hexdigest()


async def test_api_enforces_project_access_and_exact_revision(published, rig):
    _, project, _ = published
    service, _, _, _, _ = rig
    service.workspace = GitProjectWorkspace()
    await service.register(project, None)
    principal = None

    async def identity(_):
        return principal

    app = FastAPI()
    app.include_router(create_projects_router(service, identity))
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as client:
        prefix = f"/projects/{project.id}/documents"
        index = await client.get(prefix)
        assert index.status_code == 200 and index.json()["documents"][0]["id"] == "notes"
        response = await client.get(
            prefix + "/notes", params={"revision": index.json()["revision"]}
        )
        assert response.status_code == 200 and response.json()["content"].startswith(
            "# Project notes"
        )
        assert (await client.get(prefix + "/notes", params={"revision": "x"})).status_code == 422
        assert (
            await client.get(prefix + "/notes", params={"revision": "0" * 40})
        ).status_code == 409
        assert (await client.get(prefix + "/unknown")).status_code == 404
        principal = Principal("other", "other@example.test", "other-tenant", [])
        assert (await client.get(prefix)).status_code == 404
        assert (await client.get(prefix + "/notes")).status_code == 404


async def test_git_timeout_cancels_owned_process_and_does_not_return_empty_success(
    tmp_path, monkeypatch
):
    async def stalled_read(_):
        await asyncio.sleep(60)

    proc = SimpleNamespace(
        stdout=SimpleNamespace(read=stalled_read),
        returncode=None,
        kill=Mock(),
        communicate=AsyncMock(return_value=(b"", b"")),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    reader = GitProjectDocumentReader(
        manifest_bytes=1024, document_bytes=1024, document_count=2, git_timeout=0.001
    )
    with pytest.raises(ValueError, match="timed out"):
        await reader._git(tmp_path, 1024, "rev-parse", "HEAD")
    proc.kill.assert_called_once()
    proc.communicate.assert_awaited_once()


@pytest.mark.parametrize("raw", [b"not JSON", b"[]", b"\xff"])
async def test_invalid_manifest_encoding_or_container_fails_loudly(published, raw):
    root, project, _ = published
    (root / "project.json").write_bytes(raw)
    commit(root)
    with pytest.raises(ValueError, match="JSON"):
        await GitProjectWorkspace().documents(project)


async def test_git_output_limit_kills_only_owned_subprocess(tmp_path, monkeypatch):
    proc = SimpleNamespace(
        stdout=SimpleNamespace(read=AsyncMock(return_value=b"too long")),
        returncode=None,
        kill=Mock(),
        communicate=AsyncMock(return_value=(b"", b"")),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    reader = GitProjectDocumentReader(
        manifest_bytes=1024, document_bytes=1024, document_count=2, git_timeout=1
    )
    with pytest.raises(ValueError, match="budget"):
        await reader._git(tmp_path, 1, "rev-parse", "HEAD")
    proc.kill.assert_called_once()
    proc.communicate.assert_awaited_once()


async def test_cancelled_git_read_preserves_cancellation_and_reaps_process(tmp_path, monkeypatch):
    entered = asyncio.Event()

    async def waiting(_):
        entered.set()
        await asyncio.sleep(60)

    proc = SimpleNamespace(
        stdout=SimpleNamespace(read=waiting),
        returncode=None,
        kill=Mock(),
        communicate=AsyncMock(return_value=(b"", b"")),
    )
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=proc))
    reader = GitProjectDocumentReader(
        manifest_bytes=1024, document_bytes=1024, document_count=2, git_timeout=60
    )
    task = asyncio.create_task(reader._git(tmp_path, 1024, "rev-parse", "HEAD"))
    await entered.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    proc.kill.assert_called_once()
    proc.communicate.assert_awaited_once()
