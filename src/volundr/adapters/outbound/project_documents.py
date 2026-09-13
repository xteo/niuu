"""Read explicitly published text documents from one immutable Git commit.

Never read a requested workspace path or run smudge filters. The committed project
manifest selects documents; Git object mode, length and UTF-8 are validated before
returning content. No interpretation of project workflow or completion lives here.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import re
from pathlib import Path

from pydantic import ValidationError

from volundr.domain.project_ports import ProjectConflictError, ProjectDocumentNotFoundError
from volundr.domain.projects import (
    ForgeProject,
    ProjectDocument,
    ProjectDocumentDescriptor,
    ProjectDocumentIndex,
)


class GitProjectDocumentReader:
    def __init__(
        self, *, manifest_bytes: int, document_bytes: int, document_count: int, git_timeout: float
    ):
        self._manifest_bytes = manifest_bytes
        self._document_bytes = document_bytes
        self._document_count = document_count
        self._git_timeout = git_timeout

    async def _git(self, root: Path, limit: int, *arguments: str) -> bytes:
        proc = await asyncio.create_subprocess_exec(
            "git",
            "--no-replace-objects",
            "--literal-pathspecs",
            "-C",
            str(root),
            *arguments,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )

        async def consume() -> bytes:
            assert proc.stdout is not None
            result = bytearray()
            while chunk := await proc.stdout.read(limit + 1 - len(result)):
                result.extend(chunk)
                if len(result) > limit:
                    raise ValueError("Project document Git response exceeds its configured budget")
            await proc.wait()
            if proc.returncode:
                raise ValueError("Cannot read the committed project documents")
            return bytes(result)

        try:
            return await asyncio.wait_for(consume(), self._git_timeout)
        except BaseException as exc:
            if proc.returncode is None:
                proc.kill()
            await proc.communicate()
            if isinstance(exc, TimeoutError):
                raise ValueError("Project document Git lookup timed out") from None
            raise

    async def _blob(
        self, root: Path, revision: str, path: str, budget: int, *, optional: bool = False
    ) -> bytes | None:
        entry = await self._git(root, self._manifest_bytes, "ls-tree", "-z", revision, "--", path)
        if not entry:
            if optional:
                return None
            raise ValueError(f"Published project document is not committed: {path}")
        records = entry.split(b"\0")
        if len(records) != 2 or records[-1] != b"" or b"\t" not in records[0]:
            raise ValueError("Invalid project document Git entry")
        header, actual_path = records[0].split(b"\t", 1)
        fields = header.split(b" ")
        if (
            len(fields) != 3
            or fields[0] not in {b"100644", b"100755"}
            or fields[1] != b"blob"
            or actual_path != path.encode("utf-8")
            or not re.fullmatch(rb"(?:[0-9a-f]{40}|[0-9a-f]{64})", fields[2])
        ):
            raise ValueError("Published project documents must be regular committed files")
        oid = fields[2].decode("ascii")
        raw_size = await self._git(root, self._manifest_bytes, "cat-file", "-s", oid)
        if not raw_size.strip().isdigit():
            raise ValueError("Invalid project document size")
        size = int(raw_size)
        if size > budget:
            raise ValueError(f"Published project document exceeds its configured budget: {path}")
        data = await self._git(root, budget, "cat-file", "blob", oid)
        if len(data) != size:
            raise ValueError("Project document object length changed")
        return data

    async def _manifest(
        self, root: Path, project: ForgeProject, expected_revision: str | None
    ) -> tuple[str, list[ProjectDocumentDescriptor]]:
        raw_revision = await self._git(
            root, self._manifest_bytes, "rev-parse", "--verify", "HEAD^{commit}"
        )
        revision = raw_revision.decode("ascii").strip()
        if not re.fullmatch(r"(?:[0-9a-f]{40}|[0-9a-f]{64})", revision):
            raise ValueError("Invalid project document revision")
        if expected_revision is not None and expected_revision != revision:
            raise ProjectConflictError("Project documents changed; refresh the document list")
        raw = await self._blob(root, revision, "project.json", self._manifest_bytes, optional=True)
        if raw is None:
            return revision, []  # No committed publishing manifest is a normal optional absence.
        try:
            manifest = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise ValueError("Committed project.json must be a UTF-8 JSON object") from None
        if not isinstance(manifest, dict):
            raise ValueError("Committed project.json must be a JSON object")
        if "documents" not in manifest:
            return revision, []
        if manifest.get("id") != str(project.id):
            raise ValueError("Committed document manifest does not match this project's identity")
        items = manifest["documents"]
        if not isinstance(items, list) or len(items) > self._document_count:
            raise ValueError("Published project document count exceeds its configured budget")
        try:
            documents = [ProjectDocumentDescriptor.model_validate(item) for item in items]
        except ValidationError:
            # Do not echo arbitrary manifest values or paths into API diagnostics.
            raise ValueError("Invalid published project document descriptor") from None
        if len({item.id for item in documents}) != len(documents):
            raise ValueError("Published project document IDs must be unique")
        if len({item.path for item in documents}) != len(documents):
            raise ValueError("Published project document paths must be unique")
        return revision, documents

    async def index(self, root: Path, project: ForgeProject) -> ProjectDocumentIndex:
        revision, documents = await self._manifest(root, project, None)
        # Listing is cheap and does not claim every document was successfully read.
        return ProjectDocumentIndex(project_id=project.id, revision=revision, documents=documents)

    async def read(
        self, root: Path, project: ForgeProject, document_id: str, expected_revision: str | None
    ) -> ProjectDocument:
        revision, documents = await self._manifest(root, project, expected_revision)
        descriptor = next((item for item in documents if item.id == document_id), None)
        if descriptor is None:
            raise ProjectDocumentNotFoundError("Project document is not published")
        raw = await self._blob(root, revision, descriptor.path, self._document_bytes)
        assert raw is not None
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError:
            raise ValueError("Published project document must be UTF-8 text") from None
        if "\0" in content:
            raise ValueError("Published project document cannot contain NUL bytes")
        return ProjectDocument(
            project_id=project.id,
            revision=revision,
            document=descriptor,
            content=content,
            byte_size=len(raw),
            sha256=hashlib.sha256(raw).hexdigest(),
        )
