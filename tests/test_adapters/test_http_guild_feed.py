"""The push gateway's reader of the Guild's merged notification feed."""

from __future__ import annotations

import httpx
import pytest

from niuu.adapters.inbound.forge_notification_feed import decode_cursor, encode_cursor
from volundr.adapters.outbound import http_guild_feed
from volundr.adapters.outbound.http_guild_feed import HttpGuildFeed
from volundr.domain.push_ports import GuildFeedError

GUILD = "http://127.0.0.1:8080"
THOR = "56499f00-02b3-4d80-8137-09d6d3238056"
MINI = "c3d1d72f-14fb-4aca-91ae-c3a40106a27a"


def item(notification_id: str, instance_id: str, seq: int, **overrides) -> dict:
    data = {
        "id": notification_id,
        "seq": seq,
        "session_id": "sess-1",
        "session_name": "horde-coordinator",
        "owner_id": "dev-user",
        "kind": "milestone",
        "severity": "success",
        "source": "agent",
        "title": "Build finished",
        "body": "",
        "turn_id": "nt_1",
        "created_at": "2026-10-02T06:40:14.596304Z",
        "image": None,
        "read": False,
        "instance_id": instance_id,
    }
    data.update(overrides)
    return data


class Guild:
    def __init__(self, answer: httpx.Response | Exception) -> None:
        self.answer = answer
        self.requests: list[httpx.Request] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


def feed_for(guild: Guild, *, reader_token: str = "") -> HttpGuildFeed:
    return HttpGuildFeed(
        guild_url=GUILD,
        timeout_seconds=5,
        reader_token=reader_token,
        client=httpx.AsyncClient(transport=httpx.MockTransport(guild.handler)),
    )


class TestNewest:
    async def test_reads_the_merged_feed_with_heads_and_unread_count(self):
        guild = Guild(
            httpx.Response(
                200,
                json={
                    "items": [item("n-2", THOR, 323), item("m-1", MINI, 7, read=True)],
                    "next_before": "opaque",
                    "next_after": encode_cursor({THOR: 323, MINI: 7}),
                    "unread_count": 1,
                    "instances": {
                        THOR: {"head_seq": 323, "read_through_seq": 321, "unread_count": 1},
                        MINI: {"head_seq": 7, "read_through_seq": 7, "unread_count": 0},
                        "broken": {"head_seq": "7"},
                    },
                },
            )
        )

        page = await feed_for(guild).newest(limit=20)

        request = guild.requests[0]
        assert request.url.path == "/api/v1/forge/notifications"
        assert dict(request.url.params) == {"all_instances": "true", "limit": "20"}
        assert "authorization" not in request.headers
        assert [(n.id, n.instance_id, n.read) for n in page.items] == [
            ("n-2", THOR, False),
            ("m-1", MINI, True),
        ]
        assert page.positions == {THOR: 323, MINI: 7}
        assert page.unread_count == 1

    async def test_the_reader_token_is_sent_as_a_bearer(self):
        guild = Guild(httpx.Response(200, json={"items": [], "instances": {}}))
        page = await feed_for(guild, reader_token="pat-123").newest(limit=1)
        assert guild.requests[0].headers["authorization"] == "Bearer pat-123"
        assert page.items == [] and page.unread_count == 0

    async def test_a_single_node_is_not_a_guild_feed(self):
        guild = Guild(httpx.Response(200, json={"items": [], "head_seq": 3, "next_before": None}))
        with pytest.raises(GuildFeedError, match="must be a Guild API origin"):
            await feed_for(guild).newest(limit=1)

    async def test_malformed_items_are_skipped(self, caplog):
        guild = Guild(
            httpx.Response(
                200,
                json={
                    "items": ["nope", {"id": "x"}, item("n-1", THOR, 1)],
                    "instances": {THOR: {"head_seq": 1}},
                },
            )
        )
        page = await feed_for(guild).newest(limit=5)
        assert [n.id for n in page.items] == ["n-1"]
        assert "Ignoring a malformed Guild feed item" in caplog.text


class TestAfter:
    async def test_sends_the_positions_as_the_guilds_cursor(self):
        guild = Guild(
            httpx.Response(
                200,
                json={
                    "items": [item("m-2", MINI, 8), item("n-3", THOR, 324)],
                    "next_after": encode_cursor({THOR: 324, MINI: 8}),
                },
            )
        )

        page = await feed_for(guild).after({THOR: 323, MINI: 7}, limit=50)

        params = dict(guild.requests[0].url.params)
        assert params["all_instances"] == "true" and params["limit"] == "50"
        assert decode_cursor(params["after"]) == {THOR: 323, MINI: 7}
        assert [n.id for n in page.items] == ["m-2", "n-3"]
        assert page.positions == {THOR: 324, MINI: 8}

    async def test_a_null_position_is_left_out(self):
        guild = Guild(
            httpx.Response(200, json={"items": [], "next_after": encode_cursor({THOR: None})})
        )
        page = await feed_for(guild).after({THOR: 0}, limit=5)
        assert page.positions == {}

    @pytest.mark.parametrize("cursor", [None, 12])
    async def test_a_missing_cursor_is_an_error(self, cursor):
        guild = Guild(httpx.Response(200, json={"items": [], "next_after": cursor}))
        with pytest.raises(GuildFeedError, match="no next_after cursor"):
            await feed_for(guild).after({THOR: 0}, limit=5)

    async def test_an_unreadable_cursor_is_an_error(self):
        guild = Guild(httpx.Response(200, json={"items": [], "next_after": "!!!"}))
        with pytest.raises(GuildFeedError, match="unreadable cursor"):
            await feed_for(guild).after({THOR: 0}, limit=5)


class TestFailures:
    async def test_unreachable(self):
        guild = Guild(httpx.ConnectError("refused"))
        with pytest.raises(GuildFeedError, match="unreachable: ConnectError"):
            await feed_for(guild).newest(limit=1)

    @pytest.mark.parametrize("status", [401, 403, 502])
    async def test_refused_or_failing(self, status):
        guild = Guild(httpx.Response(status, json={"detail": "no"}))
        with pytest.raises(GuildFeedError, match=f"HTTP {status}"):
            await feed_for(guild).newest(limit=1)

    async def test_not_json(self):
        guild = Guild(httpx.Response(200, content=b"<html>"))
        with pytest.raises(GuildFeedError, match="invalid JSON"):
            await feed_for(guild).newest(limit=1)

    @pytest.mark.parametrize("payload", [[], {"items": "x"}, {}])
    async def test_unexpected_shape(self, payload):
        guild = Guild(httpx.Response(200, json=payload))
        with pytest.raises(GuildFeedError, match="unexpected shape"):
            await feed_for(guild).newest(limit=1)


class TestConstruction:
    def test_guild_url_is_required(self):
        with pytest.raises(ValueError, match="push.gateway.guild_url"):
            HttpGuildFeed(guild_url="", timeout_seconds=5)

    def test_guild_url_must_be_http(self):
        with pytest.raises(ValueError, match="http or https"):
            HttpGuildFeed(guild_url="ftp://guild", timeout_seconds=5)

    async def test_owns_and_closes_its_client(self):
        feed = HttpGuildFeed(guild_url=GUILD + "/", timeout_seconds=5)
        assert feed._feed_url == f"{GUILD}/api/v1/forge/notifications"
        await feed.close()
        assert feed._client.is_closed

    async def test_an_injected_client_is_left_open(self):
        client = httpx.AsyncClient()
        feed = HttpGuildFeed(guild_url=GUILD, timeout_seconds=5, client=client)
        await feed.close()
        assert not client.is_closed
        await client.aclose()


class TestEvents:
    async def test_yields_the_node_of_each_notification_event(self, monkeypatch):
        seen: dict = {}

        async def events(url, headers):
            seen["url"], seen["headers"] = url, dict(headers)
            yield "session_updated", {"instance_id": THOR}
            yield "session_notification", {"id": "n-1", "instance_id": MINI}
            yield "heartbeat", {}
            yield "session_notification", {"id": "n-2"}

        monkeypatch.setattr(http_guild_feed, "remote_events", events)
        feed = HttpGuildFeed(guild_url=GUILD, timeout_seconds=5, reader_token="pat-123")

        assert [instance async for instance in feed.notification_events()] == [MINI, ""]
        assert seen["url"] == f"{GUILD}/api/v1/forge/sessions/stream?all_instances=true"
        assert seen["headers"] == {"authorization": "Bearer pat-123"}
        await feed.close()

    async def test_a_lost_stream_is_a_feed_error(self, monkeypatch):
        async def events(url, headers):
            raise httpx.ReadTimeout("idle")
            yield  # pragma: no cover

        monkeypatch.setattr(http_guild_feed, "remote_events", events)
        feed = HttpGuildFeed(guild_url=GUILD, timeout_seconds=5)

        with pytest.raises(GuildFeedError, match="event stream is unreachable: ReadTimeout"):
            async for _ in feed.notification_events():
                pass
        await feed.close()
