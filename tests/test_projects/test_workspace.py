"""Real filesystem/Git checks for bounded context and recoverable handoff exports."""

import json
import subprocess
from uuid import uuid4

import pytest

from volundr.adapters.outbound.project_workspace import GitProjectWorkspace
from volundr.domain.projects import ForgeProject, ProjectReceipt, SessionReference


@pytest.fixture
def checkout(tmp_path):
    root = tmp_path / "project-lexi"
    root.mkdir()
    subprocess.run(["git", "init", "-q", str(root)], check=True)
    (root / "PROJECT.md").write_text("# Lexi\nPersistent coordination.\n")
    subprocess.run(["git", "-C", str(root), "add", "PROJECT.md"], check=True)
    subprocess.run(
        [
            "git",
            "-C",
            str(root),
            "-c",
            "user.name=Forge QA",
            "-c",
            "user.email=qa@example.test",
            "commit",
            "-qm",
            "Initial project",
        ],
        check=True,
    )
    project = ForgeProject(
        slug="lexi",
        name="Lexi",
        repo_url="https://github.com/xteo/project-lexi",
        workspace_path=str(root),
    )
    return root, project


async def test_dirty_checkpoint_has_distinct_revision_and_commit_is_unchanged(checkout):
    root, project = checkout
    workspace = GitProjectWorkspace(allowed_prefixes=[str(root.parent)])
    text, revision = await workspace.context(project)
    assert "Persistent coordination" in text
    (root / "PROJECT.md").write_text("Changed decision\n")
    _, changed = await workspace.context(project)
    assert changed != revision
    assert changed.split("@")[0] == revision.split("@")[0]


async def test_context_budget_and_symlink_escape(checkout, tmp_path):
    root, project = checkout
    workspace = GitProjectWorkspace(context_bytes=100)
    (root / "PROJECT.md").write_text("x" * 101)
    with pytest.raises(ValueError, match="budget"):
        await workspace.context(project)
    secret = tmp_path / "private.md"
    secret.write_text("must not be read")
    (root / "PROJECT.md").unlink()
    (root / "PROJECT.md").symlink_to(secret)
    with pytest.raises(ValueError, match="escape"):
        await workspace.context(project)


async def test_disallowed_root_and_missing_checkout(checkout, tmp_path):
    _, project = checkout
    with pytest.raises(ValueError, match="outside"):
        await GitProjectWorkspace(allowed_prefixes=[str(tmp_path / "other")]).context(project)
    with pytest.raises(ValueError, match="no local checkout"):
        await GitProjectWorkspace().context(project.model_copy(update={"workspace_path": ""}))


async def test_receipt_export_is_idempotent_and_does_not_stage_user_edits(checkout):
    root, project = checkout
    receipt = ProjectReceipt(
        project_id=project.id,
        sender=SessionReference(instance_id="thor", session_id=uuid4()),
        content="The worker finished; tests and review are still separate evidence.",
    )
    workspace = GitProjectWorkspace()
    (root / "unrelated.txt").write_text("User's ongoing work")
    await workspace.archive_receipt(project, receipt)
    await workspace.archive_receipt(project, receipt)
    files = list((root / "history" / "handoffs").iterdir())
    assert len(files) == 1 and json.loads(files[0].read_text())["id"] == str(receipt.id)
    staged = subprocess.check_output(["git", "-C", str(root), "diff", "--cached", "--name-only"])
    assert staged == b""
    assert (root / "unrelated.txt").read_text() == "User's ongoing work"


async def test_history_symlink_is_rejected_before_creating_external_directories(checkout, tmp_path):
    root, project = checkout
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "history").symlink_to(outside, target_is_directory=True)
    receipt = ProjectReceipt(
        project_id=project.id,
        sender=SessionReference(instance_id="thor", session_id=uuid4()),
        content="handoff",
    )
    with pytest.raises(ValueError, match="escape"):
        await GitProjectWorkspace().archive_receipt(project, receipt)
    assert not (outside / "handoffs").exists()
