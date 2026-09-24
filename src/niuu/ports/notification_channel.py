"""Per-user notification channel port (the event-driven path Ting uses).

Moved from ``ting.ports.notification_channel`` so the shared channel adapters in
:mod:`niuu.adapters.notifications` can implement it; Ting re-exports it. Channels
are best-effort: ``send`` logs failures instead of raising. The Forge delivery
outbox uses the stricter :class:`niuu.ports.notifications.NotificationSink`.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import StrEnum


class NotificationUrgency(StrEnum):
    """Urgency level for a notification."""

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"


@dataclass(frozen=True)
class Notification:
    """A user-facing notification built from a domain event."""

    title: str
    body: str
    urgency: NotificationUrgency
    owner_id: str
    event_type: str
    metadata: dict[str, str] = field(default_factory=dict)


class NotificationChannel(ABC):
    """Abstract notification delivery channel (Telegram, Slack, etc.)."""

    @abstractmethod
    async def send(self, notification: Notification) -> None:
        """Deliver a notification through this channel."""

    @abstractmethod
    def should_notify(self, notification: Notification) -> bool:
        """Return True if this channel should deliver the given notification."""
