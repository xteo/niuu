"""Ports of the push gateway: APNs, the Guild's merged feed and the gateway's own store."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

from volundr.domain.push import ApnsPush, ApnsResult, GuildNotification


class ApplePushClient(ABC):
    """Sends one push to APNs and reports its answer."""

    @abstractmethod
    async def send(self, push: ApnsPush) -> ApnsResult:
        """Send ``push``. APNs' refusals and an unreachable APNs are answers
        (:class:`~volundr.domain.push.ApnsResult`), never exceptions."""

    @abstractmethod
    async def close(self) -> None:
        """Release the HTTP/2 connection."""


class GuildFeedError(Exception):
    """The Guild feed could not be read (unreachable, refused or malformed)."""


@dataclass(frozen=True)
class GuildFeedPage:
    """One page of the Guild's merged feed.

    ``positions`` is where reading continues, per node (the newest ``seq`` seen, or
    the node's head for a newest-first page). ``unread_count`` is the reader's.
    """

    items: list[GuildNotification]
    positions: dict[str, int] = field(default_factory=dict)
    unread_count: int = 0


class GuildFeed(ABC):
    """Reads every Guild node's notifications through the Guild API."""

    @abstractmethod
    async def newest(self, *, limit: int) -> GuildFeedPage:
        """The newest notifications across the Guild, newest first, with each
        reachable node's head position."""

    @abstractmethod
    async def after(self, positions: Mapping[str, int], *, limit: int) -> GuildFeedPage:
        """Notifications recorded after ``positions`` on the nodes it names,
        oldest first, with the positions to continue from."""

    @abstractmethod
    def notification_events(self) -> AsyncIterator[str]:
        """Yield the ``instance_id`` of every notification event as it happens.

        A wake-up signal only: the feed is the source of truth. The iterator ends
        or raises :class:`GuildFeedError` when the stream is lost."""

    @abstractmethod
    async def close(self) -> None:
        """Release network clients."""


class PushDeliveryStatus(StrEnum):
    """Where one notification's alert stands in the gateway."""

    PENDING = "pending"
    #: APNs accepted it for at least one device.
    SENT = "sent"
    #: Held back by the alert policy (kind, quiet hours, rate limit, age).
    SUPPRESSED = "suppressed"
    #: Nothing to send to: the owner has no alert device.
    SKIPPED = "skipped"
    #: APNs refused it for every device, or the attempts ran out.
    FAILED = "failed"


@dataclass(frozen=True)
class PushDelivery:
    """A notification the gateway ingested, claimed for one alert attempt.

    ``attempts`` already counts this claim; ``(instance_id, notification_id,
    attempts)`` fences the settle calls, so a worker whose lease expired can no
    longer settle the row.
    """

    notification: GuildNotification
    attempts: int
    created_at: datetime


class PushDeliveryRepository(ABC):
    """The gateway's durable state: its feed positions and the alerts it owes."""

    @abstractmethod
    async def load_positions(self) -> dict[str, int]:
        """Where the gateway stopped reading each node's feed."""

    @abstractmethod
    async def save_positions(self, positions: Mapping[str, int]) -> None:
        """Start reading nodes at ``positions`` (merged into the stored ones)."""

    @abstractmethod
    async def record(
        self, notifications: list[GuildNotification], positions: Mapping[str, int]
    ) -> list[GuildNotification]:
        """Store newly read notifications as pending alerts and advance the
        positions, in one transaction. Returns the ones not seen before."""

    @abstractmethod
    async def claim_due(self, *, limit: int, lease_seconds: float) -> list[PushDelivery]:
        """Lease up to ``limit`` pending alerts whose next attempt is due."""

    @abstractmethod
    async def settle(self, delivery: PushDelivery, status: PushDeliveryStatus, detail: str) -> bool:
        """Close a claimed alert. False when the claim was lost to another worker."""

    @abstractmethod
    async def retry(self, delivery: PushDelivery, *, detail: str, retry_at: datetime) -> bool:
        """Keep a claimed alert pending until ``retry_at``."""

    @abstractmethod
    async def count_sent_since(self, owner_id: str, since: datetime) -> int:
        """How many alerts were sent to ``owner_id`` since ``since`` (rate limit)."""

    @abstractmethod
    async def prune_settled(self, before: datetime) -> int:
        """Delete alerts settled before ``before``; returns how many."""
