"""Session icons: the contract shared by Forge, the Guild facade and the session tools.

A session registers one square icon (LEX-2628/LEX-2629). Forge accepts a PNG, JPEG or
HEIC image that is exactly 1:1, between ``ICON_MIN_INPUT_PX`` and ``ICON_MAX_INPUT_PX``
on a side and at most ``ICON_MAX_INPUT_BYTES``, and re-encodes it to one
``ICON_SIZE`` square PNG (plus the smaller ``ICON_SIZES`` renditions), metadata
stripped. The content hash of the 512 px PNG is the icon's ETag, carried on the
session record as ``icon_etag`` so clients refetch only when it changes.

These values are the published API contract the iPhone and Mac clients code against,
not tunables: changing one is a contract change.
"""

from __future__ import annotations

#: The one stored icon is a square PNG of this side.
ICON_SIZE = 512
#: Sizes ``GET /sessions/{id}/icon?size=`` serves (``ICON_SIZE`` first, the default).
ICON_SIZES: tuple[int, ...] = (ICON_SIZE, 256, 128)
#: Accepted input sides, in pixels (inclusive).
ICON_MIN_INPUT_PX = 64
ICON_MAX_INPUT_PX = 2048
#: Accepted input body size: 2 MiB.
ICON_MAX_INPUT_BYTES = 2 * 1024 * 1024
#: What every rendition is served as.
ICON_MEDIA_TYPE = "image/png"
#: The icon changes rarely and its URL is checked with its ETag; never shared caches.
ICON_CACHE_CONTROL = "private, max-age=86400"
#: The human names of the accepted input types, for error messages.
ICON_INPUT_TYPES_TEXT = "PNG, JPEG or HEIC"


def icon_etag_header(etag: str) -> str:
    """The strong ``ETag`` header value for a stored icon hash."""
    return f'"{etag}"'


def if_none_match_hits(header: str | None, etag: str) -> bool:
    """Whether an ``If-None-Match`` header names this icon (RFC 9110 weak comparison)."""
    if not header:
        return False
    for candidate in header.split(","):
        candidate = candidate.strip()
        if candidate == "*":
            return True
        if candidate.startswith("W/"):
            candidate = candidate[2:]
        if candidate == icon_etag_header(etag):
            return True
    return False
