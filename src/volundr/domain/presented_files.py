"""Durable presented files (control-plane F2).

``present-file`` stages a copy of a file next to the session's broker. That copy is
served only while the broker runs, so a link to it dies with the session. Forge keeps
its own durable copy of every presented file, keyed by ``(session_id, file_id)``, and
serves it whether or not the session is still running.

Each copy has an absolute URL on the host's tailnet name (``Artifact.url``: http(s),
never ``*.xteo.mesh``), so a phone on the tailnet opens it in Safari.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from uuid import UUID

from niuu.domain.presented_files import PRESENTED_FILE_ID_PATTERN

_PRESENTED_FILE_ID = re.compile(PRESENTED_FILE_ID_PATTERN)
_FORBIDDEN_HOST_SUFFIX = ".xteo.mesh"


class PresentedFileConflictError(ValueError):
    """A different file was already stored under this ``(session_id, file_id)``."""


def is_presented_file_id(value: str) -> bool:
    return bool(_PRESENTED_FILE_ID.fullmatch(value or ""))


@dataclass(frozen=True)
class PresentedFile:
    """Metadata of one durable presented file."""

    session_id: UUID
    file_id: str
    name: str
    mime: str
    size: int
    sha256: str
    stored_at: datetime


class PresentedFileStore(ABC):
    """Durable storage for presented files."""

    @abstractmethod
    async def put(
        self, session_id: UUID, file_id: str, *, name: str, mime: str, data: bytes
    ) -> PresentedFile:
        """Store a file once. Re-storing identical bytes returns the stored record;
        different bytes under the same id raise :class:`PresentedFileConflictError`."""

    @abstractmethod
    async def get(self, session_id: UUID, file_id: str) -> tuple[PresentedFile, Path] | None:
        """The stored record and the path of its bytes, or ``None`` when absent."""

    @abstractmethod
    async def delete_session(self, session_id: UUID) -> int:
        """Remove every file stored for a session; return how many were removed."""


def validate_public_base_url(value: str) -> str:
    """Normalize the configured base URL; empty means "no absolute URL"."""
    value = (value or "").strip().rstrip("/")
    if not value:
        return ""
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("presented_files.public_base_url must be an http(s) URL with a host")
    host = parts.hostname.lower()
    if host.endswith(_FORBIDDEN_HOST_SUFFIX) or host == _FORBIDDEN_HOST_SUFFIX.lstrip("."):
        raise ValueError(
            "presented_files.public_base_url must use the host's tailnet name, not *.xteo.mesh"
        )
    if parts.query or parts.fragment:
        raise ValueError("presented_files.public_base_url must not carry a query or fragment")
    return value


def presented_file_path(session_id: UUID, file_id: str, *, prefix: str = "/api/v1/forge") -> str:
    """The Forge route that serves a presented file (relative)."""
    return f"{prefix}/sessions/{session_id}/files/presented/{quote(file_id, safe='')}"


def durable_file_url(
    public_base_url: str, session_id: UUID, file_id: str, *, prefix: str = "/api/v1/forge"
) -> str | None:
    """The absolute, phone-reachable URL of a presented file, or ``None`` when no base
    URL is configured (callers then keep the relative route)."""
    base = validate_public_base_url(public_base_url)
    if not base:
        return None
    return base + presented_file_path(session_id, file_id, prefix=prefix)
