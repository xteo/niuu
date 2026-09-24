"""Push delivery for Forge notifications through Volundr's push channels.

``PushNotificationSink`` wraps the configured push ``NotificationChannel`` (APNs,
webhook relay or logging, from ``push.adapter``) and the owner's registered
devices (``DeviceTokenRepository``). It is opt-in: an operator lists it under
``notifications.sinks`` and an owner rule selects it.

It never repeats the needs-input push. When ``push.enabled`` is on, the
``PushAttentionNotifier`` already pushes every "session needs you" moment as it
happens; the matching ``attention`` notification (``source: system``) is then
recorded as ``suppressed`` for this sink instead of being pushed a second time.
Agent-raised ``attention`` notifications are still pushed.
"""

from __future__ import annotations

from collections.abc import Mapping

from niuu.domain.notifications import (
    NotificationKind,
    NotificationSeverity,
    NotificationSource,
)
from niuu.ports.notifications import (
    DeliveryResult,
    NotificationDeliveryError,
    NotificationSink,
    OutboundNotification,
)
from volundr.domain.models import PushMessage
from volundr.domain.ports import DeviceTokenRepository, NotificationChannel

PUSH_NOTIFICATION_EVENT = "session.notification"

#: Push urgency per severity (the channel's 0..1 scale; needs-input pushes use 0.9).
DEFAULT_SEVERITY_URGENCY: dict[str, float] = {
    NotificationSeverity.INFO.value: 0.3,
    NotificationSeverity.SUCCESS.value: 0.5,
    NotificationSeverity.WARNING.value: 0.8,
    NotificationSeverity.CRITICAL.value: 1.0,
}

#: Push bodies stay short: APNs caps the whole payload at 4 KB.
DEFAULT_PUSH_BODY_CHARS = 280

ATTENTION_ALREADY_PUSHED = (
    "The needs-input push (push.enabled) already alerted the owner for this attention"
)


def _shorten(text: str, limit: int) -> str:
    text = " ".join(text.split())
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


class PushNotificationSink(NotificationSink):
    """Pushes a notification to every device the owner registered.

    ``device_repository``, ``push_channel`` and ``attention_push_enabled`` are
    supplied by Volundr when it builds the configured sinks; the remaining
    keyword arguments come from the sink's config entry.
    """

    def __init__(
        self,
        *,
        device_repository: DeviceTokenRepository,
        push_channel: NotificationChannel,
        attention_push_enabled: bool = False,
        name: str = "push",
        severity_urgency: Mapping[str, float] | None = None,
        max_body_chars: int = DEFAULT_PUSH_BODY_CHARS,
        **_unused: object,
    ) -> None:
        if max_body_chars < 1:
            raise ValueError("max_body_chars must be at least 1")
        self._name = name
        self._devices = device_repository
        self._channel = push_channel
        self._attention_push_enabled = attention_push_enabled
        self._urgency = {**DEFAULT_SEVERITY_URGENCY, **(severity_urgency or {})}
        self._max_body_chars = max_body_chars

    @property
    def name(self) -> str:
        return self._name

    def skip_reason(self, message: OutboundNotification) -> str | None:
        if (
            self._attention_push_enabled
            and message.kind is NotificationKind.ATTENTION
            and message.source is NotificationSource.SYSTEM
        ):
            return ATTENTION_ALREADY_PUSHED
        return None

    async def send(self, message: OutboundNotification) -> DeliveryResult:
        if not message.owner_id:
            raise NotificationDeliveryError(
                "Push needs an owner to find devices for", retryable=False
            )
        devices = await self._devices.list_for_owner(message.owner_id)
        push = PushMessage(
            owner_id=message.owner_id,
            title=message.title,
            body=_shorten(message.body, self._max_body_chars),
            session_id=message.session_id or "",
            kind=message.kind.value,
            urgency=self._urgency.get(message.severity.value, 0.0),
            request_id=str(message.metadata.get("request_id") or ""),
            event_type=PUSH_NOTIFICATION_EVENT,
            notification_id=message.notification_id,
            url=message.url or "",
        )
        detail = await self._channel.deliver(push, devices)
        return DeliveryResult(detail=detail)
