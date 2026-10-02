"""The push gateway reads the real Guild facade: its merged feed, cursors and node tags.

``HttpGuildFeed`` is a client of ``create_volundr_router``. These tests run the two
against each other (the facade in-process, one embedded node and one remote node) so
the gateway's assumptions about the merged feed cannot drift from the facade.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import respx
from fastapi import FastAPI, Request
from httpx import Response

from niuu.adapters.inbound.rest_volundr import create_volundr_router
from tests.support.push_fakes import (
    Clock,
    FakeApns,
    FakeDeviceRepository,
    FakePushDeliveryRepository,
    device,
    gateway_settings,
)
from tests.test_niuu.test_rest_volundr import StubInstanceService, _instance
from tests.test_niuu.test_rest_volundr_notifications import T0, _item, _page
from volundr.adapters.outbound.http_guild_feed import HttpGuildFeed
from volundr.domain.push import PushTokenKind
from volundr.domain.services.push_gateway import PushGateway

OWNER = "dev-user"
# The gateway sends no credential: the facade then serves its local developer
# principal, whose tenant these instances belong to (the AllowAll Guild).
LOCAL = _instance(
    "local",
    base_url="embedded://local",
    tenant_id="default",
    is_default=True,
    config={"transport": "embedded"},
)
REMOTE = _instance("remote", base_url="http://remote", tenant_id="default")


class Guild:
    """The facade over one embedded node and one remote node, both with real feeds."""

    def __init__(self) -> None:
        self.local: list[dict[str, Any]] = [_item(1, 1, OWNER), _item(2, 3, OWNER)]
        self.remote: list[dict[str, Any]] = [_item(10, 2, OWNER), _item(11, 4, OWNER)]
        self.remote_up = True
        node = FastAPI()

        @node.get("/api/v1/forge/notifications")
        async def feed(request: Request):
            return _page(self.local, request.query_params, default_limit=50)

        app = FastAPI()
        app.include_router(  # type: ignore[arg-type]
            create_volundr_router(StubInstanceService([LOCAL, REMOTE]), embedded_forge_app=node)
        )
        self.feed = HttpGuildFeed(
            guild_url="http://guild",
            timeout_seconds=5,
            client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app)),
        )

    def remote_feed(self, request: httpx.Request) -> Response:
        if not self.remote_up:
            raise httpx.ConnectError("node down")
        return Response(200, json=_page(self.remote, request.url.params, default_limit=50))


def gateway_for(guild: Guild, clock: Clock) -> tuple[PushGateway, FakeApns, Any]:
    devices = FakeDeviceRepository(clock)
    devices.add(device("alert-1", owner_id=OWNER))
    devices.add(device("la-1", PushTokenKind.LIVE_ACTIVITY, owner_id=OWNER))
    deliveries, apns = FakePushDeliveryRepository(clock), FakeApns()
    gateway = PushGateway(guild.feed, devices, deliveries, apns, gateway_settings(), clock=clock)
    return gateway, apns, deliveries


@respx.mock
async def test_gateway_follows_the_facades_merged_feed_across_nodes():
    guild = Guild()
    respx.get("http://remote/api/v1/forge/notifications").mock(side_effect=guild.remote_feed)
    clock = Clock(T0 + timedelta(minutes=10))
    gateway, apns, deliveries = gateway_for(guild, clock)

    # First tick: every node is found at its head; history is not replayed.
    assert await gateway.run_once() == 0
    assert deliveries.positions == {"local": 2, "remote": 11}
    assert apns.sent == []

    # One notification on each node.
    guild.local.append(_item(3, 9, OWNER))
    guild.remote.append(_item(12, 10, OWNER))
    assert await gateway.run_once() == 2
    assert deliveries.positions == {"local": 3, "remote": 12}

    alerts = {push.payload["notification_id"]: push.payload for push in apns.of_type("alert")}
    assert alerts["n3"]["instance_id"] == "local"
    assert alerts["n3"]["deepLink"] == "lexichat://activity/n3?inst=local"
    assert alerts["n12"]["instance_id"] == "remote"
    assert alerts["n12"]["deepLink"] == "lexichat://activity/n12?inst=remote"

    # The Live Activity shows the Guild's newest three, with the facade's node tags
    # and the unread count summed over the nodes.
    (update,) = apns.of_type("liveactivity")
    state = update.payload["aps"]["content-state"]
    assert [(item["id"], item["instanceID"]) for item in state["items"]] == [
        ("n12", "remote"),
        ("n3", "local"),
        ("n11", "remote"),
    ]
    assert state["unreadCount"] == 6
    assert all(item["unread"] for item in state["items"])

    # Nothing new: nothing is read twice.
    assert await gateway.run_once() == 0
    assert len(apns.of_type("alert")) == 2
    await guild.feed.close()


@respx.mock
async def test_an_unreachable_node_keeps_its_position_and_is_caught_up_later():
    guild = Guild()
    respx.get("http://remote/api/v1/forge/notifications").mock(side_effect=guild.remote_feed)
    clock = Clock(T0 + timedelta(minutes=10))
    gateway, apns, deliveries = gateway_for(guild, clock)
    await gateway.run_once()

    guild.remote_up = False
    guild.remote.append(_item(12, 10, OWNER))
    guild.local.append(_item(3, 9, OWNER))
    assert await gateway.run_once() == 1
    assert deliveries.positions == {"local": 3, "remote": 11}

    guild.remote_up = True
    assert await gateway.run_once() == 1
    assert deliveries.positions == {"local": 3, "remote": 12}
    assert sorted(p.payload["notification_id"] for p in apns.of_type("alert")) == ["n12", "n3"]
    await guild.feed.close()


@respx.mock
async def test_a_node_down_at_the_first_tick_is_found_once_it_is_back():
    guild = Guild()
    guild.remote_up = False
    respx.get("http://remote/api/v1/forge/notifications").mock(side_effect=guild.remote_feed)
    clock = Clock(T0 + timedelta(minutes=10))
    gateway, apns, deliveries = gateway_for(guild, clock)

    await gateway.run_once()
    assert deliveries.positions == {"local": 2}

    guild.remote_up = True
    clock.advance(301)  # The next discovery.
    await gateway.run_once()
    assert deliveries.positions == {"local": 2, "remote": 11}

    guild.remote.append(_item(12, 16, OWNER))
    assert await gateway.run_once() == 1
    assert [p.payload["notification_id"] for p in apns.of_type("alert")] == ["n12"]
    await guild.feed.close()
