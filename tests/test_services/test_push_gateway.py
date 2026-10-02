"""The Guild push gateway: ingest, the alert policy, Live Activity, widgets, pruning."""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest

from niuu.domain.notifications import NotificationSeverity
from tests.support.push_fakes import (
    BUNDLE,
    MINI,
    START,
    THOR,
    Clock,
    FakeApns,
    FakeDeviceRepository,
    FakeGuildFeed,
    FakePushDeliveryRepository,
    alert_settings,
    device,
    gateway_settings,
    guild_notification,
)
from volundr.adapters.outbound.broadcaster import InMemoryEventBroadcaster
from volundr.domain.models import DevicePlatform, EventType, RealtimeEvent
from volundr.domain.notifications import NotificationQuietHours, NotificationRateLimit
from volundr.domain.ports import EventBroadcaster
from volundr.domain.push import (
    ApnsResult,
    PushEnvironment,
    PushTokenKind,
)
from volundr.domain.push_ports import GuildFeedError, PushDeliveryStatus
from volundr.domain.services.push_gateway import (
    ALERTS_DISABLED,
    ALREADY_READ,
    NO_ALERT_DEVICE,
    AlertSettings,
    LiveActivitySettings,
    PushGateway,
    PushGatewaySettings,
    WidgetSettings,
)

START_UNIX = int(START.timestamp())
START_SWIFT = START.timestamp() - 978_307_200


class Harness:
    def __init__(
        self,
        settings: PushGatewaySettings | None = None,
        *,
        local_events: EventBroadcaster | None = None,
    ) -> None:
        self.clock = Clock()
        self.feed = FakeGuildFeed()
        self.devices = FakeDeviceRepository(self.clock)
        self.deliveries = FakePushDeliveryRepository(self.clock)
        self.apns = FakeApns()
        self.gateway = PushGateway(
            self.feed,
            self.devices,
            self.deliveries,
            self.apns,
            settings or gateway_settings(),
            local_events=local_events,
            clock=self.clock,
        )

    async def started(self) -> Harness:
        """Both nodes are known and the gateway stands at their heads."""
        self.feed.add_node(THOR)
        self.feed.add_node(MINI)
        await self.gateway.run_once()
        return self


def live_activity(**overrides) -> LiveActivitySettings:
    values = {
        "enabled": True,
        "priority": 10,
        "stale_seconds": 28800,
        "start_enabled": False,
        "start_min_interval_seconds": 3600.0,
    }
    values.update(overrides)
    return LiveActivitySettings(**values)


@pytest.fixture
async def harness() -> Harness:
    return await Harness().started()


class TestIngest:
    async def test_first_tick_starts_at_each_nodes_head_without_replaying_history(self):
        h = Harness()
        h.feed.add(guild_notification("old-1", seq=1), guild_notification("old-2", seq=2))
        h.feed.add(guild_notification("m-old", instance_id=MINI, seq=7))
        h.devices.add(device("alert-1"))

        assert await h.gateway.run_once() == 0

        assert h.deliveries.positions == {THOR: 2, MINI: 7}
        assert h.deliveries.rows == {}
        assert h.apns.sent == []

    async def test_a_notification_on_any_node_is_ingested_once(self, harness):
        harness.devices.add(device("alert-1"))
        harness.feed.add(guild_notification("n-1", seq=1))
        harness.feed.add(guild_notification("m-1", instance_id=MINI, seq=1))

        assert await harness.gateway.run_once() == 2
        assert await harness.gateway.run_once() == 0

        assert harness.deliveries.positions == {THOR: 1, MINI: 1}
        assert harness.deliveries.statuses() == {"n-1": "sent", "m-1": "sent"}
        assert len(harness.apns.of_type("alert")) == 2

    async def test_pages_until_the_feed_is_drained(self):
        h = await Harness(gateway_settings(page_size=2)).started()
        for seq in range(1, 6):
            h.feed.add(guild_notification(f"n-{seq}", seq=seq))

        assert await h.gateway.run_once() == 5
        assert h.deliveries.positions[THOR] == 5

    async def test_a_new_node_is_found_and_its_latest_items_caught_up(self):
        h = await Harness(gateway_settings(catch_up_items=2)).started()
        h.devices.add(device("alert-1"))
        new_node = "inst-spark"
        for seq in range(1, 6):
            h.feed.add(
                guild_notification(
                    f"s-{seq}",
                    instance_id=new_node,
                    seq=seq,
                    created_at=START - timedelta(hours=1 if seq < 5 else 0),
                )
            )

        # An event from a node the gateway does not read yet triggers discovery.
        h.gateway.note_event(new_node)
        assert await h.gateway.run_once() == 2

        assert h.deliveries.positions[new_node] == 5
        # The older caught-up item is past the age limit: recorded, not alerted.
        assert h.deliveries.statuses() == {"s-4": "suppressed", "s-5": "sent"}

    async def test_discovery_runs_again_after_its_interval(self, harness):
        harness.feed.newest_calls.clear()
        await harness.gateway.run_once()
        assert harness.feed.newest_calls == []

        harness.feed.add(guild_notification("x-1", instance_id="inst-late", seq=1))
        harness.clock.advance(301)
        await harness.gateway.run_once()

        assert harness.feed.newest_calls == [1]
        assert harness.deliveries.positions["inst-late"] == 1

    async def test_positions_survive_a_restart(self, harness):
        harness.feed.add(guild_notification("n-1", seq=1))
        await harness.gateway.run_once()

        restarted = PushGateway(
            harness.feed,
            harness.devices,
            harness.deliveries,
            harness.apns,
            gateway_settings(),
            clock=harness.clock,
        )
        harness.feed.add(guild_notification("n-2", seq=2))
        assert await restarted.run_once() == 1
        assert harness.feed.after_calls[-2] == {THOR: 1, MINI: 0}

    async def test_an_unreadable_feed_raises(self, harness):
        harness.feed.error = GuildFeedError("The Guild feed answered HTTP 502")
        harness.feed.add(guild_notification("n-1", seq=1))
        with pytest.raises(GuildFeedError):
            await harness.gateway.run_once()


class TestAlerts:
    async def test_alert_push_matches_the_app_contract(self, harness):
        harness.devices.add(device("alert-1", environment=PushEnvironment.SANDBOX))
        harness.feed.add(
            guild_notification(
                "1a97554b-96b1-5fac-baa4-5bf53ba35d92",
                instance_id=MINI,
                seq=1,
                kind="decision",
                title="Final round green: merge now",
                body="Round 5 on the exact pair:\n 209/244",
                session_id="sess-9",
                turn_id="nt_11c2",
            )
        )

        await harness.gateway.run_once()

        (push,) = harness.apns.of_type("alert")
        assert push.token == "alert-1"
        assert push.environment is PushEnvironment.SANDBOX
        assert push.topic == BUNDLE
        assert push.priority == 10
        assert push.collapse_id == "1a97554b-96b1-5fac-baa4-5bf53ba35d92"
        assert push.expiration == START_UNIX + 3600
        assert push.payload == {
            "aps": {
                "alert": {
                    "title": "Final round green: merge now",
                    "body": "Round 5 on the exact pair: 209/244",
                    "subtitle": "horde-coordinator",
                },
                "interruption-level": "time-sensitive",
                "sound": "default",
                "category": "FORGE_ACTIVITY",
                "thread-id": "sess-9",
            },
            "deepLink": (
                "lexichat://activity/1a97554b-96b1-5fac-baa4-5bf53ba35d92"
                f"?inst={MINI}&session=sess-9&turn=nt_11c2"
            ),
            "session_id": "sess-9",
            "notification_id": "1a97554b-96b1-5fac-baa4-5bf53ba35d92",
            "instance_id": MINI,
            "kind": "decision",
        }
        row = harness.deliveries.row("1a97554b-96b1-5fac-baa4-5bf53ba35d92", MINI)
        assert row.status is PushDeliveryStatus.SENT
        assert row.detail == "APNs accepted 1 of 1 devices"
        assert harness.gateway.stats.alerts_sent == 1

    async def test_milestone_is_active_and_a_title_naming_the_session_has_no_subtitle(
        self, harness
    ):
        harness.devices.add(device("alert-1"))
        harness.feed.add(
            guild_notification(
                "n-1", kind="attention", source="system", title="horde-coordinator needs your input"
            ),
            guild_notification("n-2", seq=2, kind="milestone", session_name=None, turn_id=None),
        )

        await harness.gateway.run_once()

        attention, milestone = harness.apns.of_type("alert")
        assert "subtitle" not in attention.payload["aps"]["alert"]
        assert attention.payload["aps"]["interruption-level"] == "time-sensitive"
        assert milestone.payload["aps"]["interruption-level"] == "active"
        assert "subtitle" not in milestone.payload["aps"]["alert"]
        assert (
            milestone.payload["deepLink"] == f"lexichat://activity/n-2?inst={THOR}&session=sess-1"
        )

    async def test_image_urls_are_absolute_on_the_public_origin(self, harness):
        harness.devices.add(device("alert-1"))
        harness.feed.add(
            guild_notification(
                "n-1",
                image_url=f"/api/v1/forge/notifications/n-1/image?instance_id={THOR}",
                image_thumbnail_url=(
                    f"/api/v1/forge/notifications/n-1/image?variant=thumb&instance_id={THOR}"
                ),
            )
        )

        await harness.gateway.run_once()

        payload = harness.apns.of_type("alert")[0].payload
        base = "http://thor.tail737f2a.ts.net:8080/api/v1/forge/notifications/n-1/image"
        assert payload["aps"]["mutable-content"] == 1
        assert payload["image_url"] == f"{base}?instance_id={THOR}"
        assert payload["image_thumbnail_url"] == f"{base}?variant=thumb&instance_id={THOR}"

    async def test_without_a_public_origin_the_image_is_left_out(self):
        h = await Harness(gateway_settings(public_api_url="")).started()
        h.devices.add(device("alert-1"))
        h.feed.add(guild_notification("n-1", image_url="/api/v1/forge/notifications/n-1/image"))

        await h.gateway.run_once()

        payload = h.apns.of_type("alert")[0].payload
        assert "image_url" not in payload and "mutable-content" not in payload["aps"]

    async def test_low_priority_kinds_and_a_long_collapse_id(self):
        settings = gateway_settings(
            alerts=alert_settings(
                kinds=frozenset({"info"}),
                low_priority_kinds=frozenset({"info"}),
                session_subtitle=False,
            )
        )
        h = await Harness(settings).started()
        h.devices.add(device("alert-1", app_bundle_id=None))
        h.feed.add(guild_notification("x" * 65, kind="info"))

        await h.gateway.run_once()

        (push,) = h.apns.of_type("alert")
        assert push.priority == 5
        assert push.collapse_id is None
        assert push.topic == BUNDLE
        assert "subtitle" not in push.payload["aps"]["alert"]

    async def test_every_alert_device_gets_it_on_its_own_environment(self, harness):
        harness.devices.add(
            device("prod-1"),
            device("sandbox-1", environment=PushEnvironment.SANDBOX),
            device("android-1", platform=DevicePlatform.ANDROID),
            device("other-owner", owner_id="someone-else"),
            device("widget-1", PushTokenKind.WIDGET),
        )
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()

        alerts = {push.token: push.environment for push in harness.apns.of_type("alert")}
        assert alerts == {
            "prod-1": PushEnvironment.PRODUCTION,
            "sandbox-1": PushEnvironment.SANDBOX,
        }

    @pytest.mark.parametrize(
        ("overrides", "reason"),
        [
            ({"kind": "info"}, "Kind 'info' is not alerted (push.gateway.alerts.kinds)"),
            ({"kind": "reply_ready"}, "Kind 'reply_ready' is not alerted"),
            ({"read": True}, ALREADY_READ),
            (
                {"created_at": START - timedelta(seconds=901)},
                "Older than 900s when the gateway picked it up",
            ),
        ],
    )
    async def test_policy_holds_back(self, harness, overrides, reason):
        harness.devices.add(device("alert-1"))
        harness.feed.add(guild_notification("n-1", **overrides))

        await harness.gateway.run_once()

        row = harness.deliveries.row("n-1")
        assert row.status is PushDeliveryStatus.SUPPRESSED
        assert row.detail.startswith(reason)
        assert harness.apns.of_type("alert") == []
        assert harness.gateway.stats.alerts_suppressed == 1

    @pytest.mark.parametrize(
        ("alerts", "notification", "reason"),
        [
            ({"enabled": False}, {}, ALERTS_DISABLED),
            (
                {"min_severity": NotificationSeverity.WARNING},
                {"severity": "success"},
                "Below the alert severity 'warning'",
            ),
            (
                {"sources": frozenset({"agent"})},
                {"source": "system"},
                "Source 'system' is not alerted (push.gateway.alerts.sources)",
            ),
        ],
    )
    async def test_configured_policy_holds_back(self, alerts, notification, reason):
        h = await Harness(gateway_settings(alerts=alert_settings(**alerts))).started()
        h.devices.add(device("alert-1"))
        h.feed.add(guild_notification("n-1", **notification))

        await h.gateway.run_once()

        assert h.deliveries.row("n-1").detail == reason
        assert h.apns.of_type("alert") == []

    async def test_a_notification_without_a_time_or_with_an_unknown_severity_still_alerts(
        self, harness
    ):
        harness.devices.add(device("alert-1"))
        harness.feed.add(guild_notification("n-1", created_at=None, severity="urgent"))

        await harness.gateway.run_once()

        assert harness.deliveries.statuses() == {"n-1": "sent"}

    async def test_quiet_hours_hold_back_all_but_severe_notifications(self):
        quiet = NotificationQuietHours(
            start="06:00", end="07:00", timezone="UTC", allow_min_severity="critical"
        )
        h = await Harness(gateway_settings(alerts=alert_settings(quiet_hours=quiet))).started()
        h.devices.add(device("alert-1"))
        h.feed.add(
            guild_notification("calm", seq=1, severity="warning"),
            guild_notification("severe", seq=2, severity="critical", kind="error"),
            guild_notification("odd", seq=3, severity="urgent"),
        )

        await h.gateway.run_once()

        assert h.deliveries.statuses() == {
            "calm": "suppressed",
            "severe": "sent",
            "odd": "suppressed",
        }
        assert h.deliveries.row("calm").detail == (
            "Quiet hours 06:00-07:00 UTC (only critical and above are alerted)"
        )

    async def test_rate_limit_counts_sent_alerts_per_owner_in_the_window(self):
        limit = NotificationRateLimit(max_count=2, window_seconds=3600)
        h = await Harness(gateway_settings(alerts=alert_settings(rate_limit=limit))).started()
        h.devices.add(device("alert-1"), device("alert-2", owner_id="other"))
        for seq in (1, 2, 3):
            h.feed.add(guild_notification(f"n-{seq}", seq=seq))
        h.feed.add(guild_notification("o-1", instance_id=MINI, seq=1, owner_id="other"))

        await h.gateway.run_once()

        assert h.deliveries.statuses() == {
            "n-1": "sent",
            "n-2": "sent",
            "n-3": "suppressed",
            "o-1": "sent",
        }
        assert h.deliveries.row("n-3").detail == "Rate limit: at most 2 alerts per 3600s"

        # The window slides: an hour later the owner is alerted again.
        h.clock.advance(3601)
        h.feed.add(guild_notification("n-4", seq=4, created_at=h.clock.now))
        await h.gateway.run_once()
        assert h.deliveries.row("n-4").status is PushDeliveryStatus.SENT

    async def test_owner_without_an_alert_device_is_skipped(self, harness):
        harness.devices.add(device("widget-1", PushTokenKind.WIDGET))
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()

        row = harness.deliveries.row("n-1")
        assert (row.status, row.detail) == (PushDeliveryStatus.SKIPPED, NO_ALERT_DEVICE)
        assert harness.gateway.stats.alerts_skipped == 1

    async def test_a_dead_token_is_deleted_and_the_other_device_still_alerted(self, harness):
        harness.devices.add(device("gone"), device("alive"))
        harness.apns.answer("gone", ApnsResult(410, "Unregistered"))
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()

        assert harness.devices.tokens() == ["alive"]
        assert harness.deliveries.row("n-1").detail == "APNs accepted 1 of 2 devices"
        assert harness.gateway.stats.tokens_pruned == 1

    @pytest.mark.parametrize(
        "answer",
        [
            ApnsResult(410),
            ApnsResult(400, "BadDeviceToken"),
            ApnsResult(400, "ExpiredToken"),
            ApnsResult(410, "Unregistered"),
        ],
    )
    async def test_only_dead_tokens_left_fails_the_alert_and_prunes(self, harness, answer):
        harness.devices.add(device("gone"))
        harness.apns.answer("gone", answer)
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()

        assert harness.devices.tokens() == []
        row = harness.deliveries.row("n-1")
        assert row.status is PushDeliveryStatus.FAILED
        assert row.detail == f"APNs: {answer.describe()}"
        assert harness.gateway.stats.alerts_failed == 1

    async def test_a_refusal_that_is_not_about_the_token_keeps_it(self, harness):
        harness.devices.add(device("alert-1"))
        harness.apns.answer("alert-1", ApnsResult(400, "BadTopic"))
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()

        assert harness.devices.tokens() == ["alert-1"]
        assert harness.deliveries.row("n-1").status is PushDeliveryStatus.FAILED
        assert harness.deliveries.row("n-1").detail == "APNs: HTTP 400 BadTopic"

    async def test_unreachable_apns_is_retried_with_backoff_then_sent(self, harness):
        harness.devices.add(device("alert-1"))
        harness.apns.answer("alert-1", ApnsResult(None, "ConnectError"), ApnsResult(503))
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()
        row = harness.deliveries.row("n-1")
        assert row.status is PushDeliveryStatus.PENDING
        assert row.detail == "APNs: not reached (ConnectError)"
        assert row.next_attempt_at == START + timedelta(seconds=15)
        assert harness.gateway.stats.alerts_retried == 1

        # Not due yet: nothing is sent.
        harness.clock.advance(10)
        await harness.gateway.run_once()
        assert len(harness.apns.of_type("alert")) == 1

        harness.clock.advance(5)
        await harness.gateway.run_once()
        assert row.next_attempt_at == harness.clock.now + timedelta(seconds=30)

        harness.clock.advance(30)
        await harness.gateway.run_once()
        assert row.status is PushDeliveryStatus.SENT
        assert len(harness.apns.of_type("alert")) == 3

    async def test_a_throttling_apns_sets_the_wait(self, harness):
        harness.devices.add(device("alert-1"))
        harness.apns.answer("alert-1", ApnsResult(429, "TooManyRequests", retry_after=120.0))
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()

        assert harness.deliveries.row("n-1").next_attempt_at == START + timedelta(seconds=120)

    async def test_attempts_run_out(self, harness):
        harness.devices.add(device("alert-1"))
        harness.apns.answer("alert-1", *[ApnsResult(503, "ServiceUnavailable")] * 3)
        harness.feed.add(guild_notification("n-1", created_at=None))

        for _ in range(3):
            await harness.gateway.run_once()
            harness.clock.advance(700)

        row = harness.deliveries.row("n-1")
        assert row.status is PushDeliveryStatus.FAILED
        assert row.detail == "APNs: HTTP 503 ServiceUnavailable"
        assert row.attempts == 3

    async def test_a_row_whose_lease_expired_too_often_is_given_up(self, harness):
        harness.devices.add(device("alert-1"))
        harness.feed.add(guild_notification("n-1"))
        await harness.gateway._ingest(harness.deliveries.positions)
        harness.deliveries.row("n-1").attempts = 3

        await harness.gateway.run_once()

        row = harness.deliveries.row("n-1")
        assert (row.status, row.detail) == (PushDeliveryStatus.FAILED, "Gave up after 3 attempts")
        assert harness.apns.sent == []

    async def test_a_lost_claim_is_counted_not_settled_twice(self, harness):
        harness.devices.add(device("alert-1"))
        harness.feed.add(guild_notification("n-1"), guild_notification("n-2", seq=2))
        await harness.gateway._ingest(harness.deliveries.positions)
        claimed = await harness.deliveries.claim_due(limit=5, lease_seconds=60)
        # Another worker re-claimed both rows before this one settled them.
        for delivery in claimed:
            harness.deliveries.row(delivery.notification.id).attempts += 1
        harness.apns.answer("alert-1", ApnsResult(200), ApnsResult(503))

        for delivery in claimed:
            await harness.gateway._deliver_alert(delivery)

        assert harness.gateway.stats.stale_settles == 2
        assert harness.gateway.stats.alerts_sent == 0
        assert harness.deliveries.statuses() == {"n-1": "pending", "n-2": "pending"}

    async def test_a_full_batch_claims_again(self):
        h = await Harness(gateway_settings(alerts=alert_settings(batch_size=2))).started()
        h.devices.add(device("alert-1"))
        for seq in range(1, 6):
            h.feed.add(guild_notification(f"n-{seq}", seq=seq))

        await h.gateway.run_once()

        assert set(h.deliveries.statuses().values()) == {"sent"}
        assert h.gateway.stats.alerts_sent == 5


class TestLiveActivity:
    async def test_update_carries_the_guilds_newest_three_as_swift_content_state(self, harness):
        harness.devices.add(
            device("la-1", PushTokenKind.LIVE_ACTIVITY, activity_id="act-1"),
            device("la-2", PushTokenKind.LIVE_ACTIVITY, environment=PushEnvironment.SANDBOX),
        )
        harness.feed.unread_count = 2
        harness.feed.nodes[THOR] = [
            guild_notification("t-1", seq=1, created_at=START - timedelta(minutes=30), read=True),
            guild_notification("t-2", seq=2, created_at=START - timedelta(minutes=5)),
        ]
        harness.feed.nodes[MINI] = [
            guild_notification(
                "m-1", instance_id=MINI, seq=1, created_at=START - timedelta(minutes=10), read=True
            ),
            guild_notification(
                "m-other", instance_id=MINI, seq=2, owner_id="other", created_at=START
            ),
        ]
        harness.deliveries.positions.update({THOR: 2, MINI: 2})
        harness.feed.add(
            guild_notification(
                "m-2",
                instance_id=MINI,
                seq=3,
                kind="reply_ready",
                title="T" * 120,
                session_name="S" * 60,
                turn_id=None,
            )
        )

        await harness.gateway.run_once()

        pushes = harness.apns.of_type("liveactivity")
        assert [push.token for push in pushes] == ["la-1", "la-2"]
        push = pushes[0]
        assert push.topic == "com.xteo.lexi.chat.push-type.liveactivity"
        assert push.priority == 10
        assert push.collapse_id is None and push.expiration is None
        assert pushes[1].environment is PushEnvironment.SANDBOX
        assert push.payload == {
            "aps": {
                "timestamp": START_UNIX,
                "event": "update",
                "stale-date": START_UNIX + 28800,
                "content-state": {
                    "items": [
                        {
                            "id": "m-2",
                            "kind": "reply_ready",
                            "title": "T" * 90,
                            "sessionName": "S" * 40,
                            "instanceID": MINI,
                            "sessionID": "sess-1",
                            "createdAt": START_SWIFT,
                            "unread": True,
                        },
                        {
                            "id": "t-2",
                            "kind": "milestone",
                            "title": "Build finished",
                            "sessionName": "horde-coordinator",
                            "instanceID": THOR,
                            "sessionID": "sess-1",
                            "turnID": "nt_1",
                            "createdAt": START_SWIFT - 300,
                            "unread": True,
                        },
                        {
                            "id": "m-1",
                            "kind": "milestone",
                            "title": "Build finished",
                            "sessionName": "horde-coordinator",
                            "instanceID": MINI,
                            "sessionID": "sess-1",
                            "turnID": "nt_1",
                            "createdAt": START_SWIFT - 600,
                            "unread": False,
                        },
                    ],
                    "unreadCount": 2,
                    "updatedAt": START_SWIFT,
                    "origin": "Thor",
                },
            }
        }
        assert harness.gateway.stats.live_activity_updates == 2

    async def test_a_kind_that_does_not_alert_still_updates_the_activity(self, harness):
        harness.devices.add(device("alert-1"), device("la-1", PushTokenKind.LIVE_ACTIVITY))
        harness.feed.add(guild_notification("n-1", kind="info"))

        await harness.gateway.run_once()

        assert harness.apns.of_type("alert") == []
        assert len(harness.apns.of_type("liveactivity")) == 1

    async def test_without_an_origin_label_the_key_is_absent(self):
        h = await Harness(gateway_settings(origin_label="")).started()
        h.devices.add(device("la-1", PushTokenKind.LIVE_ACTIVITY))
        h.feed.add(guild_notification("n-1"))

        await h.gateway.run_once()

        state = h.apns.of_type("liveactivity")[0].payload["aps"]["content-state"]
        assert "origin" not in state

    async def test_an_ended_activitys_token_is_deleted(self, harness):
        harness.devices.add(device("la-gone", PushTokenKind.LIVE_ACTIVITY))
        harness.apns.answer("la-gone", ApnsResult(410, "ExpiredToken"))
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()

        assert harness.devices.tokens(PushTokenKind.LIVE_ACTIVITY) == []

    async def test_one_newest_page_serves_every_owner(self, harness):
        harness.devices.add(
            device("la-a", PushTokenKind.LIVE_ACTIVITY),
            device("la-b", PushTokenKind.LIVE_ACTIVITY, owner_id="other"),
        )
        harness.feed.add(
            guild_notification("n-1"), guild_notification("o-1", seq=2, owner_id="other")
        )
        harness.feed.newest_calls.clear()

        await harness.gateway.run_once()

        assert harness.feed.newest_calls == [20]
        states = {
            push.token: [item["id"] for item in push.payload["aps"]["content-state"]["items"]]
            for push in harness.apns.of_type("liveactivity")
        }
        assert states == {"la-a": ["n-1"], "la-b": ["o-1"]}

    async def test_a_failed_update_is_retried_on_the_next_tick(self, harness):
        harness.devices.add(device("la-1", PushTokenKind.LIVE_ACTIVITY))
        harness.apns.answer("la-1", ApnsResult(None, "ReadTimeout"))
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()
        assert harness.gateway.stats.live_activity_updates == 0

        await harness.gateway.run_once()
        assert harness.gateway.stats.live_activity_updates == 1
        assert len(harness.apns.of_type("liveactivity")) == 2

        # Delivered: no third push without a new notification.
        await harness.gateway.run_once()
        assert len(harness.apns.of_type("liveactivity")) == 2

    async def test_disabled_live_activity_sends_none(self):
        h = await Harness(gateway_settings(live_activity=live_activity(enabled=False))).started()
        h.devices.add(
            device("la-1", PushTokenKind.LIVE_ACTIVITY),
            device("start-1", PushTokenKind.LIVE_ACTIVITY_START),
        )
        h.feed.add(guild_notification("n-1"))

        await h.gateway.run_once()

        assert h.apns.of_type("liveactivity") == []

    async def test_owner_whose_items_left_the_newest_page_gets_no_update(self, harness):
        harness.devices.add(device("la-1", PushTokenKind.LIVE_ACTIVITY))
        harness.gateway._retry_owners.add("dev-user")

        await harness.gateway.run_once()

        assert harness.apns.sent == []


class TestPushToStart:
    def _harness(self, **overrides) -> Harness:
        return Harness(
            gateway_settings(live_activity=live_activity(start_enabled=True, **overrides))
        )

    async def test_start_when_no_activity_runs(self):
        h = await self._harness().started()
        h.devices.add(device("start-1", PushTokenKind.LIVE_ACTIVITY_START))
        h.feed.add(guild_notification("n-1", title="Deploy finished", session_name="release-bot"))

        await h.gateway.run_once()

        (push,) = h.apns.of_type("liveactivity")
        aps = push.payload["aps"]
        assert push.token == "start-1"
        assert push.topic == "com.xteo.lexi.chat.push-type.liveactivity"
        assert aps["event"] == "start"
        assert aps["attributes-type"] == "ForgeNotificationsActivityAttributes"
        assert aps["attributes"] == {}
        assert aps["alert"] == {"title": "Deploy finished", "body": "release-bot"}
        assert aps["timestamp"] == START_UNIX and aps["stale-date"] == START_UNIX + 28800
        assert [item["id"] for item in aps["content-state"]["items"]] == ["n-1"]
        assert h.gateway.stats.live_activity_starts == 1

    async def test_a_sessionless_start_alert_names_the_server(self):
        h = await self._harness().started()
        h.devices.add(device("start-1", PushTokenKind.LIVE_ACTIVITY_START))
        h.feed.add(guild_notification("n-1", session_name=None))

        await h.gateway.run_once()

        assert h.apns.of_type("liveactivity")[0].payload["aps"]["alert"]["body"] == "Thor"

    async def test_no_start_while_an_activity_runs(self):
        h = await self._harness().started()
        h.devices.add(
            device("la-1", PushTokenKind.LIVE_ACTIVITY),
            device("start-1", PushTokenKind.LIVE_ACTIVITY_START),
        )
        h.feed.add(guild_notification("n-1"))

        await h.gateway.run_once()

        assert [p.payload["aps"]["event"] for p in h.apns.of_type("liveactivity")] == ["update"]

    async def test_start_when_every_update_token_was_dead(self):
        h = await self._harness().started()
        h.devices.add(
            device("la-gone", PushTokenKind.LIVE_ACTIVITY),
            device("start-1", PushTokenKind.LIVE_ACTIVITY_START),
        )
        h.apns.answer("la-gone", ApnsResult(410, "Unregistered"))
        h.feed.add(guild_notification("n-1"))

        await h.gateway.run_once()

        events = [(p.token, p.payload["aps"]["event"]) for p in h.apns.of_type("liveactivity")]
        assert events == [("la-gone", "update"), ("start-1", "start")]

    async def test_at_most_one_start_per_interval(self):
        h = await self._harness().started()
        h.devices.add(device("start-1", PushTokenKind.LIVE_ACTIVITY_START))
        h.feed.add(guild_notification("n-1"))
        await h.gateway.run_once()

        h.clock.advance(3599)
        h.feed.add(guild_notification("n-2", seq=2, created_at=h.clock.now))
        await h.gateway.run_once()
        assert len(h.apns.of_type("liveactivity")) == 1

        h.clock.advance(1)
        h.feed.add(guild_notification("n-3", seq=3, created_at=h.clock.now))
        await h.gateway.run_once()
        assert len(h.apns.of_type("liveactivity")) == 2

    async def test_a_start_apns_could_not_take_does_not_use_up_the_interval(self):
        h = await self._harness().started()
        h.devices.add(device("start-1", PushTokenKind.LIVE_ACTIVITY_START))
        h.apns.answer("start-1", ApnsResult(503))
        h.feed.add(guild_notification("n-1"))

        await h.gateway.run_once()
        assert h.gateway.stats.live_activity_starts == 0

        await h.gateway.run_once()
        assert h.gateway.stats.live_activity_starts == 1

    async def test_a_refused_start_keeps_the_interval(self):
        h = await self._harness().started()
        h.devices.add(device("start-1", PushTokenKind.LIVE_ACTIVITY_START))
        h.apns.answer("start-1", ApnsResult(400, "BadTopic"))
        h.feed.add(guild_notification("n-1"))

        await h.gateway.run_once()
        await h.gateway.run_once()

        assert len(h.apns.of_type("liveactivity")) == 1

    async def test_start_is_off_by_default(self, harness):
        harness.devices.add(device("start-1", PushTokenKind.LIVE_ACTIVITY_START))
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()

        assert harness.apns.of_type("liveactivity") == []


class TestWidgets:
    async def test_reload_push_shape(self, harness):
        harness.devices.add(
            device("widget-1", PushTokenKind.WIDGET, environment=PushEnvironment.SANDBOX)
        )
        harness.feed.add(guild_notification("n-1", kind="info"))

        await harness.gateway.run_once()

        (push,) = harness.apns.of_type("widgets")
        assert push.token == "widget-1"
        assert push.environment is PushEnvironment.SANDBOX
        assert push.topic == "com.xteo.lexi.chat.push-type.widgets"
        assert push.priority == 5
        assert push.payload == {"aps": {"content-changed": True}}
        assert push.collapse_id is None and push.expiration is None
        assert harness.gateway.stats.widget_reloads == 1

    async def test_at_most_one_reload_per_interval_then_the_owed_one(self, harness):
        harness.devices.add(device("widget-1", PushTokenKind.WIDGET))
        harness.feed.add(guild_notification("n-1"))
        await harness.gateway.run_once()

        for seq, seconds in ((2, 60), (3, 120)):
            harness.clock.advance(60)
            harness.feed.add(guild_notification(f"n-{seq}", seq=seq, created_at=harness.clock.now))
            await harness.gateway.run_once()
            assert seconds < 300
        assert len(harness.apns.of_type("widgets")) == 1

        # 299 s after the first reload: still held back.
        harness.clock.advance(179)
        await harness.gateway.run_once()
        assert len(harness.apns.of_type("widgets")) == 1

        # The interval ended: the reload the widget is owed goes out, once.
        harness.clock.advance(1)
        await harness.gateway.run_once()
        await harness.gateway.run_once()
        assert len(harness.apns.of_type("widgets")) == 2

    async def test_each_token_has_its_own_interval(self, harness):
        harness.devices.add(device("widget-1", PushTokenKind.WIDGET))
        harness.feed.add(guild_notification("n-1"))
        await harness.gateway.run_once()

        harness.clock.advance(60)
        harness.devices.add(device("widget-2", PushTokenKind.WIDGET))
        harness.feed.add(guild_notification("n-2", seq=2, created_at=harness.clock.now))
        await harness.gateway.run_once()

        assert [push.token for push in harness.apns.of_type("widgets")] == ["widget-1", "widget-2"]

    async def test_a_reload_apns_could_not_take_stays_owed(self, harness):
        harness.devices.add(device("widget-1", PushTokenKind.WIDGET))
        harness.apns.answer("widget-1", ApnsResult(None, "ConnectError"))
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()
        assert harness.gateway.stats.widget_reloads == 0

        await harness.gateway.run_once()
        assert harness.gateway.stats.widget_reloads == 1

    async def test_a_dead_widget_token_is_deleted(self, harness):
        harness.devices.add(device("widget-gone", PushTokenKind.WIDGET))
        harness.apns.answer("widget-gone", ApnsResult(400, "BadDeviceToken"))
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.run_once()
        await harness.gateway.run_once()

        assert harness.devices.tokens() == []
        assert len(harness.apns.of_type("widgets")) == 1

    async def test_disabled_widgets_send_none(self):
        settings = gateway_settings(
            widgets=WidgetSettings(enabled=False, priority=5, min_interval_seconds=300.0)
        )
        h = await Harness(settings).started()
        h.devices.add(device("widget-1", PushTokenKind.WIDGET))
        h.devices.owed.add(next(iter(h.devices.devices)))
        h.feed.add(guild_notification("n-1"))

        await h.gateway.run_once()

        assert h.apns.of_type("widgets") == []

    async def test_an_owed_reload_of_a_non_ios_token_is_not_sent(self, harness):
        android = device("web-1", PushTokenKind.WIDGET, platform=DevicePlatform.WEB)
        harness.devices.add(android)
        harness.devices.owed.add(android.id)

        await harness.gateway.run_once()

        assert harness.apns.sent == []


class TestTestPush:
    async def test_alert(self, harness):
        harness.devices.add(device("0123456789abcdef", environment=PushEnvironment.SANDBOX))

        results = await harness.gateway.send_test(
            "dev-user", PushTokenKind.ALERT, title="Forge test push", body="It works"
        )

        (push,) = harness.apns.sent
        assert push.payload == {
            "aps": {
                "alert": {"title": "Forge test push", "body": "It works"},
                "interruption-level": "active",
                "sound": "default",
                "category": "FORGE_ACTIVITY",
            },
            "deepLink": "lexichat://activity",
        }
        assert push.priority == 10 and push.collapse_id is None
        (result,) = results
        assert result.token_suffix == "89abcdef"
        assert (result.kind, result.environment) == ("alert", "sandbox")
        assert result.accepted and result.detail == "accepted" and result.apns_id == "apns-1"

    async def test_no_token_of_that_kind(self, harness):
        harness.devices.add(device("alert-1"))
        results = await harness.gateway.send_test(
            "dev-user", PushTokenKind.WIDGET, title="t", body="b"
        )
        assert results == [] and harness.apns.sent == []

    async def test_live_activity_update_carries_the_real_state(self, harness):
        harness.devices.add(device("la-1", PushTokenKind.LIVE_ACTIVITY))
        harness.feed.add(guild_notification("n-1"))

        await harness.gateway.send_test(
            "dev-user", PushTokenKind.LIVE_ACTIVITY, title="t", body="b"
        )

        aps = harness.apns.sent[0].payload["aps"]
        assert aps["event"] == "update"
        assert [item["id"] for item in aps["content-state"]["items"]] == ["n-1"]

    async def test_start_ignores_the_switch_and_the_interval(self, harness):
        start = device("start-1", PushTokenKind.LIVE_ACTIVITY_START)
        start.last_push_at = harness.clock.now
        harness.devices.add(start)

        results = await harness.gateway.send_test(
            "dev-user", PushTokenKind.LIVE_ACTIVITY_START, title="T" * 100, body="Body"
        )

        aps = harness.apns.sent[0].payload["aps"]
        assert aps["event"] == "start"
        assert aps["alert"] == {"title": "T" * 90, "body": "Body"}
        assert aps["content-state"]["items"] == []
        assert results[0].accepted

    async def test_widget_ignores_the_interval_and_reports_a_dead_token(self, harness):
        widget = device("widget-1", PushTokenKind.WIDGET)
        widget.last_push_at = harness.clock.now
        harness.devices.add(widget)
        harness.apns.answer("widget-1", ApnsResult(410, "Unregistered"))

        (result,) = await harness.gateway.send_test(
            "dev-user", PushTokenKind.WIDGET, title="t", body="b"
        )

        assert harness.apns.sent[0].payload == {"aps": {"content-changed": True}}
        assert not result.accepted and result.detail == "HTTP 410 Unregistered"
        assert harness.devices.tokens() == []


class TestHousekeeping:
    async def test_settled_rows_are_pruned_once_per_interval(self, harness):
        harness.feed.add(guild_notification("n-1"))
        await harness.gateway.run_once()
        assert harness.deliveries.pruned_before == [START - timedelta(seconds=604800)]

        harness.clock.advance(100)
        await harness.gateway.run_once()
        assert len(harness.deliveries.pruned_before) == 1

        harness.clock.advance(604800)
        await harness.gateway.run_once()
        assert len(harness.deliveries.pruned_before) == 2
        assert harness.deliveries.rows == {}

    async def test_status_reports_counters_and_nodes(self, harness):
        harness.devices.add(device("alert-1"))
        harness.feed.add(guild_notification("n-1"))
        await harness.gateway.run_once()

        status = harness.gateway.status()

        assert status["running"] is False
        assert status["nodes"] == {MINI: 0, THOR: 1}
        assert status["alerts_sent"] == 1 and status["ingested"] == 1
        assert status["last_tick_at"] == START.isoformat()
        assert status["alerts_enabled"] and status["widgets_enabled"]
        assert status["live_activity_start_enabled"] is False
        assert Harness().gateway.status()["last_tick_at"] is None


class TestLoop:
    async def _until(self, predicate, timeout: float = 2.0) -> None:
        async with asyncio.timeout(timeout):
            while not predicate():
                await asyncio.sleep(0.005)

    async def test_an_event_wakes_the_loop_and_stop_closes_the_clients(self):
        h = Harness(gateway_settings(poll_interval_seconds=3600.0))
        h.feed.add_node(THOR)
        h.devices.add(device("alert-1"))
        await h.gateway.start()
        await h.gateway.start()  # Idempotent.
        await self._until(lambda: h.gateway.stats.ticks == 1)
        assert h.gateway.status()["running"] is True

        h.feed.add(guild_notification("n-1"))
        await h.feed.events.put(THOR)
        await self._until(lambda: h.gateway.stats.alerts_sent == 1)

        await h.gateway.stop()
        assert h.feed.closed and h.apns.closed
        assert h.gateway.status()["running"] is False

    async def test_this_nodes_own_notification_wakes_the_loop_without_a_stream(self):
        broadcaster = InMemoryEventBroadcaster()
        h = Harness(
            gateway_settings(poll_interval_seconds=3600.0, stream_enabled=False),
            local_events=broadcaster,
        )
        h.feed.add_node(THOR)
        h.devices.add(device("alert-1"))
        await h.gateway.start()
        await self._until(lambda: h.gateway.stats.ticks == 1 and broadcaster.subscriber_count == 1)

        # Another kind of event does not read the feed.
        h.feed.add(guild_notification("n-1"))
        await broadcaster.publish(RealtimeEvent(type=EventType.HEARTBEAT, data={}, timestamp=START))
        await asyncio.sleep(0.05)
        assert h.gateway.stats.ticks == 1

        await broadcaster.publish(
            RealtimeEvent(type=EventType.SESSION_NOTIFICATION, data={"id": "n-1"}, timestamp=START)
        )
        await self._until(lambda: h.gateway.stats.alerts_sent == 1)

        await h.gateway.stop()
        assert broadcaster.subscriber_count == 0

    async def test_an_unexpected_stream_failure_reconnects(self):
        h = Harness(gateway_settings(poll_interval_seconds=3600.0, stream_retry_seconds=0.01))
        h.feed.add_node(THOR)
        await h.gateway.start()
        await h.feed.events.put(RuntimeError("decoder bug"))
        await self._until(lambda: h.gateway.stats.stream_reconnects >= 1)

        h.feed.add(guild_notification("n-1"))
        await h.feed.events.put(THOR)
        await self._until(lambda: h.gateway.stats.ingested == 1)
        await h.gateway.stop()

    async def test_wake_runs_a_tick_now(self):
        h = Harness(gateway_settings(poll_interval_seconds=3600.0, stream_enabled=False))
        h.feed.add_node(THOR)
        await h.gateway.start()
        await self._until(lambda: h.gateway.stats.ticks == 1)

        h.gateway.wake()
        await self._until(lambda: h.gateway.stats.ticks == 2)
        await h.gateway.stop()

    async def test_a_feed_failure_is_counted_and_the_loop_goes_on(self):
        h = Harness(gateway_settings(poll_interval_seconds=0.01, stream_enabled=False))
        h.feed.error = GuildFeedError("The Guild feed is unreachable: ConnectError")
        await h.gateway.start()
        await self._until(lambda: h.gateway.stats.loop_errors >= 1)
        assert h.gateway.stats.last_error == "The Guild feed is unreachable: ConnectError"

        h.feed.error = None
        await self._until(lambda: h.gateway.stats.ticks >= 1)
        assert h.gateway.stats.last_error == ""
        await h.gateway.stop()

    async def test_an_unexpected_failure_is_counted_by_type(self):
        h = Harness(gateway_settings(poll_interval_seconds=0.01, stream_enabled=False))

        async def broken() -> dict[str, int]:
            raise RuntimeError("database gone")

        h.deliveries.load_positions = broken
        await h.gateway.start()
        await self._until(lambda: h.gateway.stats.loop_errors >= 1)
        assert h.gateway.stats.last_error == "RuntimeError"
        await h.gateway.stop()

    async def test_a_lost_stream_reconnects(self):
        h = Harness(gateway_settings(poll_interval_seconds=3600.0, stream_retry_seconds=0.01))
        h.feed.add_node(THOR)
        await h.gateway.start()
        await h.feed.events.put(GuildFeedError("stream lost"))
        await h.feed.events.put(None)
        await self._until(lambda: h.gateway.stats.stream_reconnects >= 2)

        h.feed.add(guild_notification("n-1"))
        await h.feed.events.put(THOR)
        await self._until(lambda: h.gateway.stats.ingested == 1)
        await h.gateway.stop()

    async def test_stop_before_start_is_safe(self):
        h = Harness()
        await h.gateway.stop()
        assert h.feed.closed and h.apns.closed


class TestSettings:
    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"batch_size": 0}, "must be >= 1"),
            ({"max_attempts": 0}, "must be >= 1"),
            ({"body_chars": 0}, "must be >= 1"),
            ({"lease_seconds": 0}, "must be positive"),
            ({"max_age_seconds": 0}, "must be positive"),
            ({"backoff_base_seconds": 700.0}, "must not exceed"),
            ({"deep_link_base": ""}, "deep_link_base"),
        ],
    )
    def test_alert_settings_are_checked(self, overrides, message):
        with pytest.raises(ValueError, match=message):
            alert_settings(**overrides)

    @pytest.mark.parametrize(
        ("overrides", "message"),
        [
            ({"bundle_id": ""}, "bundle id"),
            ({"page_size": 0}, "page_size"),
            ({"feed_page_size": 2}, "feed_page_size"),
            ({"poll_interval_seconds": 0}, "must be positive"),
            ({"stream_retry_seconds": 0}, "must be positive"),
            ({"catch_up_items": -1}, "catch_up_items"),
        ],
    )
    def test_gateway_settings_are_checked(self, overrides, message):
        with pytest.raises(ValueError, match=message):
            gateway_settings(**overrides)

    def test_alert_settings_type(self):
        assert isinstance(alert_settings(), AlertSettings)
