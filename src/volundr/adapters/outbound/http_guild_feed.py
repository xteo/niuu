"""The Guild's merged notification feed, read through the Guild API.

The Guild facade already merges every node's feed (``all_instances=true``), tags
each item with its node (``instance_id``) and routes image URLs to it. The push
gateway reads that view over HTTP like any client, so what it pushes names the
same ids the app sees. Positions are the facade's opaque ``after`` cursor
(:mod:`niuu.adapters.inbound.forge_notification_feed`).

The gateway reads as one identity (the *reader*): the requests carry
``reader_token`` as a bearer token when one is configured, and nothing otherwise
(a Guild with the AllowAll identity). Read state is the reader's.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any

import httpx

from niuu.adapters.inbound.forge_notification_feed import (
    CursorError,
    decode_cursor,
    encode_cursor,
)
from niuu.adapters.inbound.forge_session_stream import remote_events
from niuu.adapters.inbound.remote_urls import build_remote_url
from volundr.domain.push import GuildNotification
from volundr.domain.push_ports import GuildFeed, GuildFeedError, GuildFeedPage

logger = logging.getLogger(__name__)

FORGE_API_PREFIX = "/api/v1/forge"
NOTIFICATIONS_PATH = "/notifications"
STREAM_PATH = "/sessions/stream"
NOTIFICATION_EVENT = "session_notification"
_ALL_INSTANCES = ("all_instances", "true")


class HttpGuildFeed(GuildFeed):
    """Reads ``GET /api/v1/forge/notifications?all_instances=true`` on the Guild."""

    def __init__(
        self,
        *,
        guild_url: str,
        timeout_seconds: float,
        reader_token: str = "",
        client: httpx.AsyncClient | None = None,
    ) -> None:
        if not guild_url:
            raise ValueError("The push gateway needs the Guild API origin (push.gateway.guild_url)")
        self._feed_url = build_remote_url(guild_url, FORGE_API_PREFIX, NOTIFICATIONS_PATH)
        self._stream_url = (
            build_remote_url(guild_url, FORGE_API_PREFIX, STREAM_PATH) + "?all_instances=true"
        )
        self._headers = {"authorization": f"Bearer {reader_token}"} if reader_token else {}
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout_seconds)

    async def _get(self, params: list[tuple[str, str]]) -> dict[str, Any]:
        try:
            response = await self._client.get(
                self._feed_url, params=[_ALL_INSTANCES, *params], headers=self._headers
            )
        except httpx.HTTPError as exc:
            raise GuildFeedError(f"The Guild feed is unreachable: {type(exc).__name__}") from None
        if response.status_code >= httpx.codes.BAD_REQUEST:
            raise GuildFeedError(f"The Guild feed answered HTTP {response.status_code}")
        try:
            payload = response.json()
        except ValueError:
            raise GuildFeedError("The Guild feed answered with invalid JSON") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise GuildFeedError("The Guild feed answered with an unexpected shape")
        return payload

    @staticmethod
    def _items(payload: Mapping[str, Any]) -> list[GuildNotification]:
        items: list[GuildNotification] = []
        for raw in payload["items"]:
            if not isinstance(raw, Mapping):
                continue
            try:
                items.append(GuildNotification.from_feed_item(raw))
            except ValueError as exc:
                # One item a node sent wrong must not stop every later notification.
                logger.warning("Ignoring a malformed Guild feed item: %s", exc)
        return items

    async def newest(self, *, limit: int) -> GuildFeedPage:
        payload = await self._get([("limit", str(limit))])
        summaries = payload.get("instances")
        if not isinstance(summaries, Mapping):
            raise GuildFeedError(
                "The Guild feed has no per-node summaries: push.gateway.guild_url must be "
                "a Guild API origin, not a single Forge node"
            )
        positions = {
            str(instance_id): summary["head_seq"]
            for instance_id, summary in summaries.items()
            if isinstance(summary, Mapping) and type(summary.get("head_seq")) is int
        }
        unread = payload.get("unread_count")
        return GuildFeedPage(
            items=self._items(payload),
            positions=positions,
            unread_count=unread if type(unread) is int else 0,
        )

    async def after(self, positions: Mapping[str, int], *, limit: int) -> GuildFeedPage:
        payload = await self._get(
            [("after", encode_cursor(dict(positions))), ("limit", str(limit))]
        )
        cursor = payload.get("next_after")
        if not isinstance(cursor, str):
            raise GuildFeedError("The Guild feed returned no next_after cursor")
        try:
            decoded = decode_cursor(cursor)
        except CursorError as exc:
            raise GuildFeedError(f"The Guild feed returned an unreadable cursor: {exc}") from None
        return GuildFeedPage(
            items=self._items(payload),
            positions={key: seq for key, seq in decoded.items() if seq is not None},
        )

    async def notification_events(self) -> AsyncIterator[str]:
        try:
            async for name, payload in remote_events(self._stream_url, self._headers):
                if name != NOTIFICATION_EVENT:
                    continue
                instance_id = payload.get("instance_id")
                yield instance_id if isinstance(instance_id, str) else ""
        except httpx.HTTPError as exc:
            raise GuildFeedError(
                f"The Guild event stream is unreachable: {type(exc).__name__}"
            ) from None

    async def close(self) -> None:
        if self._owns_client:
            await self._client.aclose()
