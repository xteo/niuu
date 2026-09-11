"""Bounded project context and atomic handoff files in an existing Git checkout."""

import asyncio
import hashlib
import json
import os
import tempfile
from pathlib import Path

from volundr.domain.project_ports import ProjectWorkspace
from volundr.domain.projects import ForgeProject, ProjectReceipt


class GitProjectWorkspace(ProjectWorkspace):
    def __init__(self, allowed_prefixes=(), context_bytes=8192, git_timeout=15.0, **_kwargs):
        self._prefixes = tuple(Path(p).resolve() for p in allowed_prefixes)
        self._context_bytes = context_bytes
        self._git_timeout = git_timeout

    def _root(self, project: ForgeProject) -> Path:
        if not project.workspace_path:
            raise ValueError("Project has no local checkout on this host")
        root = Path(project.workspace_path).resolve()
        if not root.is_dir() or not (root / ".git").exists():
            raise ValueError("Project workspace must be an existing Git checkout")
        if self._prefixes and not any(root.is_relative_to(prefix) for prefix in self._prefixes):
            raise ValueError("Project checkout is outside the allowed workspace roots")
        return root

    async def context(self, project: ForgeProject) -> tuple[str, str]:
        root = self._root(project)
        files = ("PROJECT.md", "context/CURRENT.md")
        chunks = []
        for name in files:
            path = (root / name).resolve()
            if not path.is_relative_to(root):
                raise ValueError("Project context cannot escape its checkout")
            if path.is_file():
                if path.stat().st_size > self._context_bytes:
                    raise ValueError(
                        f"{name} exceeds the project context budget; shorten the checkpoint"
                    )
                chunks.append(
                    f"## {name}\n{await asyncio.to_thread(path.read_text, encoding='utf-8')}"
                )
        context = "\n\n".join(chunks)
        if len(context.encode()) > self._context_bytes:
            raise ValueError("Project context exceeds its budget; shorten the checkpoint")
        proc = await asyncio.create_subprocess_exec(
            "git",
            "-C",
            str(root),
            "rev-parse",
            "HEAD",
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), self._git_timeout)
        except TimeoutError:
            proc.kill()
            await proc.communicate()
            raise ValueError("Project Git revision lookup timed out") from None
        if proc.returncode:
            raise ValueError("Project checkout has no readable Git revision")
        # Include the content hash: coordinators may have a checkpoint staged
        # but not committed yet. Never label dirty content as the Git commit alone.
        digest = hashlib.sha256(context.encode()).hexdigest()
        return context, f"{out.decode().strip()}@sha256:{digest}"

    async def archive_receipt(self, project: ForgeProject, receipt: ProjectReceipt) -> None:
        root = self._root(project)

        def write():
            directory = (root / "history" / "handoffs").resolve()
            if not directory.is_relative_to(root):
                raise ValueError("Project history cannot escape its checkout")
            directory.mkdir(parents=True, exist_ok=True)
            target = directory / f"{receipt.id}.json"
            content = json.dumps(receipt.model_dump(mode="json"), indent=2) + "\n"
            descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".handoff-")
            try:
                with os.fdopen(descriptor, "w") as stream:
                    stream.write(content)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, target)
            finally:
                if os.path.exists(temporary):
                    os.unlink(temporary)

        await asyncio.to_thread(write)
