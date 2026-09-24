"""Channel resolver port: resolves a user's notification channels.

Moved from ``ting.ports.channel_resolver``; Ting re-exports it.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from niuu.ports.notification_channel import NotificationChannel


class ChannelResolverPort(ABC):
    """Abstract resolver for per-user notification channels."""

    @abstractmethod
    async def for_owner(self, owner_id: str) -> list[NotificationChannel]:
        """Return all active notification channels for the given owner."""
