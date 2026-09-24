"""Push delivery for notifications: the push sink and the strict channel ``deliver`` path."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from niuu.domain.notifications import (
    NotificationKind,
    NotificationSeverity,
    NotificationSource,
)
from niuu.ports.notifications import NotificationDeliveryError, OutboundNotification
from volundr.adapters.outbound.push_channels import (
    ApnsNotificationChannel,
    LoggingNotificationChannel,
    WebhookNotificationChannel,
)
from volundr.adapters.outbound.push_notification_sink import (
    ATTENTION_ALREADY_PUSHED,
    PushNotificationSink,
)
from volundr.domain.models import DevicePlatform, DeviceToken, PushMessage
from volundr.domain.ports import DeviceTokenRepository, NotificationChannel

_TEST_P8 = """-----BEGIN PRIVATE KEY-----
MIGHAgEAMBMGByqGSM49AgEGCCqGSM49AwEHBG0wawIBAQQg47BWBTYgy58QhfDc
2xxgtw2Vfi5RDO+VdInPdZFFLOmhRANCAAS4PbtpY+C7NQWBP/f6iAHllY4povbT
xdaWFHucGzVFS+6FcW6Q1C6VqyYnpS48MBB/4EGS5QGUh8jRKW9zw6qK
-----END PRIVATE KEY-----"""


def outbound(**overrides) -> OutboundNotification:
    values = {
        "notification_id": "n-1",
        "title": "Tests are green",
        "kind": NotificationKind.MILESTONE,
        "severity": NotificationSeverity.SUCCESS,
        "source": NotificationSource.AGENT,
        "created_at": datetime(2026, 9, 23, tzinfo=UTC),
        "body": "All   342 tests\npassed.",
        "owner_id": "user-1",
        "session_id": "sess-1",
        "url": "https://forge.example.com/volundr/session/sess-1",
        "metadata": {"request_id": "req-9"},
    }
    values.update(overrides)
    return OutboundNotification(**values)


def ios(token: str = "d1") -> DeviceToken:
    return DeviceToken(owner_id="user-1", platform=DevicePlatform.IOS, token=token)


class Devices(DeviceTokenRepository):
    def __init__(self, devices: list[DeviceToken]) -> None:
        self.devices = devices
        self.asked: list[str] = []

    async def upsert(self, device: DeviceToken) -> DeviceToken:
        return device

    async def list_for_owner(self, owner_id: str) -> list[DeviceToken]:
        self.asked.append(owner_id)
        return self.devices

    async def delete(self, owner_id: str, token: str) -> bool:
        return False


class RecordingChannel(NotificationChannel):
    def __init__(self) -> None:
        self.delivered: list[tuple[PushMessage, list[DeviceToken]]] = []

    async def send(self, message: PushMessage, devices: list[DeviceToken]) -> None:
        self.delivered.append((message, devices))


class TestPushSink:
    async def test_pushes_to_the_owners_devices(self):
        device = ios()
        devices = Devices([device])
        channel = RecordingChannel()
        sink = PushNotificationSink(device_repository=devices, push_channel=channel)

        result = await sink.send(outbound())

        assert devices.asked == ["user-1"]
        ((message, sent_to),) = channel.delivered
        assert sent_to == [device]
        assert message.event_type == "session.notification"
        assert message.title == "Tests are green"
        assert message.body == "All 342 tests passed."
        assert message.kind == "milestone"
        assert message.urgency == 0.5
        assert message.request_id == "req-9"
        assert message.notification_id == "n-1"
        assert message.url == "https://forge.example.com/volundr/session/sess-1"
        assert result.detail == "handed to RecordingChannel"
        assert sink.name == "push"

    async def test_body_is_shortened_and_urgency_is_configurable(self):
        channel = RecordingChannel()
        sink = PushNotificationSink(
            device_repository=Devices([]),
            push_channel=channel,
            max_body_chars=10,
            severity_urgency={"critical": 0.95},
            name="phone",
        )
        await sink.send(outbound(body="x" * 50, severity=NotificationSeverity.CRITICAL))
        message = channel.delivered[0][0]
        assert message.body == "x" * 9 + "…"
        assert message.urgency == 0.95
        assert sink.name == "phone"

    def test_invalid_body_limit_is_rejected(self):
        with pytest.raises(ValueError):
            PushNotificationSink(
                device_repository=Devices([]), push_channel=RecordingChannel(), max_body_chars=0
            )

    async def test_ownerless_notification_is_permanent(self):
        sink = PushNotificationSink(device_repository=Devices([]), push_channel=RecordingChannel())
        with pytest.raises(NotificationDeliveryError) as info:
            await sink.send(outbound(owner_id=""))
        assert not info.value.retryable

    def test_system_attention_is_not_pushed_twice_when_attention_push_is_on(self):
        on = PushNotificationSink(
            device_repository=Devices([]),
            push_channel=RecordingChannel(),
            attention_push_enabled=True,
        )
        off = PushNotificationSink(device_repository=Devices([]), push_channel=RecordingChannel())
        system_attention = outbound(
            kind=NotificationKind.ATTENTION, source=NotificationSource.SYSTEM
        )
        agent_attention = outbound(kind=NotificationKind.ATTENTION)
        assert on.skip_reason(system_attention) == ATTENTION_ALREADY_PUSHED
        assert on.skip_reason(agent_attention) is None
        assert on.skip_reason(outbound()) is None
        assert off.skip_reason(system_attention) is None

    async def test_channel_failures_propagate(self):
        channel = MagicMock(spec=NotificationChannel)
        channel.deliver = AsyncMock(
            side_effect=NotificationDeliveryError("relay down", retryable=True)
        )
        sink = PushNotificationSink(device_repository=Devices([ios()]), push_channel=channel)
        with pytest.raises(NotificationDeliveryError, match="relay down"):
            await sink.send(outbound())


def _message(**overrides) -> PushMessage:
    values = {
        "owner_id": "user-1",
        "title": "t",
        "body": "b",
        "session_id": "s",
        "kind": "milestone",
        "urgency": 0.5,
        "event_type": "session.notification",
        "notification_id": "n-1",
        "url": "https://forge.example.com/x",
    }
    values.update(overrides)
    return PushMessage(**values)


def _client(*responses) -> MagicMock:
    client = MagicMock()
    client.post = AsyncMock(side_effect=list(responses))
    return client


class TestRelayDeliver:
    async def test_success_includes_notification_fields(self):
        client = _client(MagicMock(status_code=202))
        channel = WebhookNotificationChannel(url="https://relay.example/push/SECRET", client=client)
        detail = await channel.deliver(_message(), [ios()])
        payload = client.post.call_args[1]["json"]
        assert payload["type"] == "session.notification"
        assert payload["notification_id"] == "n-1"
        assert payload["url"] == "https://forge.example.com/x"
        assert detail == "push relay https://relay.example accepted HTTP 202"

    async def test_server_error_is_retryable(self):
        client = _client(httpx.Response(503))
        channel = WebhookNotificationChannel(url="https://relay.example/push", client=client)
        with pytest.raises(NotificationDeliveryError) as info:
            await channel.deliver(_message(), [])
        assert info.value.retryable

    async def test_network_error_is_retryable(self):
        client = _client(httpx.ConnectError("refused"))
        channel = WebhookNotificationChannel(url="https://relay.example/push", client=client)
        with pytest.raises(NotificationDeliveryError) as info:
            await channel.deliver(_message(), [])
        assert info.value.retryable

    async def test_missing_url_is_permanent_but_send_stays_silent(self):
        channel = WebhookNotificationChannel(url="", client=_client())
        with pytest.raises(NotificationDeliveryError):
            await channel.deliver(_message(), [])
        await channel.send(_message(), [])

    async def test_send_logs_a_rejected_push(self, caplog):
        client = _client(httpx.Response(400))
        channel = WebhookNotificationChannel(url="https://relay.example/push", client=client)
        with caplog.at_level("WARNING"):
            await channel.send(_message(), [])
        assert "Push webhook delivery failed" in caplog.text

    async def test_default_deliver_falls_back_to_send(self):
        detail = await LoggingNotificationChannel().deliver(_message(), [ios()])
        assert detail == "handed to LoggingNotificationChannel"


class TestApnsDeliver:
    def _channel(self, client) -> ApnsNotificationChannel:
        return ApnsNotificationChannel(
            team_id="TEAM", key_id="KEY", private_key=_TEST_P8, bundle_id="b", client=client
        )

    async def test_partial_success_counts_as_delivered(self, caplog):
        client = _client(MagicMock(status_code=200), MagicMock(status_code=410))
        with caplog.at_level("WARNING"):
            detail = await self._channel(client).deliver(_message(), [ios("a"), ios("b")])
        assert detail == "APNs accepted 1 of 2 devices"
        assert "APNs rejected push (status=410)" in caplog.text
        body = client.post.call_args_list[0][1]["json"]
        assert body["notification_id"] == "n-1" and body["url"] == "https://forge.example.com/x"

    async def test_all_devices_failing_transiently_is_retryable(self):
        client = _client(MagicMock(status_code=503), RuntimeError("reset"))
        with pytest.raises(NotificationDeliveryError) as info:
            await self._channel(client).deliver(_message(), [ios("a"), ios("b")])
        assert info.value.retryable
        assert "none of 2 devices" in str(info.value)

    async def test_all_devices_rejected_is_permanent(self):
        client = _client(MagicMock(status_code=400))
        with pytest.raises(NotificationDeliveryError) as info:
            await self._channel(client).deliver(_message(), [ios()])
        assert not info.value.retryable

    async def test_no_ios_devices_is_permanent(self):
        web = DeviceToken(owner_id="u", platform=DevicePlatform.WEB, token="w")
        with pytest.raises(NotificationDeliveryError, match="no registered iOS devices"):
            await self._channel(_client()).deliver(_message(), [web])

    async def test_bad_key_is_permanent(self):
        channel = ApnsNotificationChannel(
            team_id="T", key_id="K", private_key="not a key", bundle_id="b", client=_client()
        )
        with pytest.raises(NotificationDeliveryError, match="setup failed") as info:
            await channel.deliver(_message(), [ios()])
        assert not info.value.retryable

    async def test_send_swallows_what_deliver_raises(self, caplog):
        client = _client(MagicMock(status_code=500))
        with caplog.at_level("WARNING"):
            await self._channel(client).send(_message(), [ios()])
        assert "APNs push failed" in caplog.text
