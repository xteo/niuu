"""Hand presented files to Forge's durable store (control-plane F2).

The broker stages every ``present-file`` copy next to itself, which only serves while
the broker runs. Right after staging, it uploads the bytes to Forge
(``PUT /sessions/{id}/files/presented/{file_id}``); Forge keeps them and answers with
the durable URL on the host's tailnet name. A failed upload is retried in the
background and never fails ``present-file`` itself: the live broker still serves the
staged copy, and the Forge route serves the durable one as soon as it lands.
"""

from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any
from urllib.parse import quote

from skuld.event_log import FORGE_SESSIONS_PATH

logger = logging.getLogger("skuld.broker")

DURABLE_MARKER = "durable.json"
NAME_HEADER = "x-presented-file-name"


def durable_record(staged: Path) -> dict[str, Any] | None:
    """The Forge receipt written beside a staged copy once it is durable."""
    marker = staged.parent / DURABLE_MARKER
    try:
        record = json.loads(marker.read_text())
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


async def upload_presented_file(
    broker: Any, file_id: str, staged: Path, *, name: str, mime: str
) -> dict[str, Any] | None:
    """Upload one staged copy; return Forge's receipt (``url``, ``path``…) or ``None``."""
    if not broker.volundr_api_url or not broker.session_id:
        return None
    existing = durable_record(staged)
    if existing is not None:
        return existing
    settings = broker._settings.presented_durable
    try:
        data = await asyncio.to_thread(staged.read_bytes)
        client = await broker._get_http_client()
        response = await client.put(
            f"{FORGE_SESSIONS_PATH}/{broker.session_id}/files/presented/{file_id}",
            content=data,
            headers={"content-type": mime, NAME_HEADER: quote(name, safe="")},
            timeout=settings.upload_timeout_seconds,
        )
    except Exception as exc:  # network, auth refresh or file errors: retry later
        logger.warning("present-file: durable upload of %s failed: %s", file_id, repr(exc))
        return None
    if response.status_code >= 300:
        logger.warning(
            "present-file: Forge refused durable copy of %s (HTTP %d)",
            file_id,
            response.status_code,
        )
        return None
    try:
        receipt = response.json()
    except ValueError:
        return None
    if not isinstance(receipt, dict):
        return None
    try:
        (staged.parent / DURABLE_MARKER).write_text(json.dumps(receipt))
    except OSError:
        logger.debug("present-file: could not write durable marker for %s", file_id)
    return receipt


async def retry_presented_upload(
    broker: Any, file_id: str, staged: Path, *, name: str, mime: str
) -> dict[str, Any] | None:
    """Retry a failed upload with exponential backoff, bounded by config."""
    settings = broker._settings.presented_durable
    delay = settings.retry_backoff_seconds
    for _ in range(settings.retry_attempts):
        await asyncio.sleep(delay)
        receipt = await upload_presented_file(broker, file_id, staged, name=name, mime=mime)
        if receipt is not None:
            return receipt
        delay = min(delay * 2, settings.retry_backoff_max_seconds)
    logger.warning("present-file: %s is not durable after retries", file_id)
    return None
