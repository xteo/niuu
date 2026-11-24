"""Session icons on the Forge side: the processed icon and the processor port.

The contract (accepted input, stored size, ETag) lives in
:mod:`niuu.domain.session_icon`. Forge decodes and re-encodes every upload through a
:class:`SessionIconProcessor`; the repository stores the renditions next to the
session row and keeps ``sessions.icon_etag`` in step, in one transaction.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from niuu.domain.session_icon import ICON_SIZES


class SessionIconError(ValueError):
    """An upload Forge refuses; the message says what to fix.

    ``status`` is the HTTP answer: 413 for a body over the size limit, 422 for
    anything else (not square, wrong type, too small or too large in pixels).
    """

    def __init__(self, message: str, *, status: int = 422) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class SessionIconImages:
    """A processed icon: its content hash and one PNG per served size."""

    etag: str
    renditions: dict[int, bytes] = field(repr=False)

    def __post_init__(self) -> None:
        missing = set(ICON_SIZES) - set(self.renditions)
        if missing:
            raise ValueError(f"icon renditions missing for sizes {sorted(missing)}")


class SessionIconProcessor(ABC):
    """Validates an uploaded image and builds the stored renditions."""

    @abstractmethod
    def process(self, data: bytes) -> SessionIconImages:
        """Return the renditions, or raise :class:`SessionIconError` naming the fix."""
