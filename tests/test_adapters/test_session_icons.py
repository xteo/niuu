"""Session icons (LEX-2629): processing contract, storage, REST, auth, MCP and the facade."""

from __future__ import annotations

import hashlib
import io
import struct
import uuid

import pytest
from PIL import Image, PngImagePlugin

from niuu.domain.session_icon import (
    ICON_CACHE_CONTROL,
    ICON_MAX_INPUT_BYTES,
    ICON_SIZE,
    ICON_SIZES,
    icon_etag_header,
    if_none_match_hits,
)
from tests.conftest import InMemorySessionRepository
from tests.support.forge_session import OWNER, PREFIX, TENANT, bearer, build_forge_app
from volundr.adapters.inbound.rest import SessionResponse
from volundr.adapters.outbound.broadcaster import InMemoryEventBroadcaster
from volundr.adapters.outbound.pillow_session_icons import PillowSessionIconProcessor
from volundr.domain.models import EventType, Session
from volundr.domain.session_icons import SessionIconError, SessionIconImages

OWNER_HEADERS = {"x-auth-user-id": OWNER, "x-auth-tenant": TENANT}
JSON_RPC = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}


# ------------------------------------------------------------------------------ images


def _image(size=(100, 100), *, fmt="PNG", mode="RGB", color="red", **save) -> bytes:
    buffer = io.BytesIO()
    Image.new(mode, size, color).save(buffer, format=fmt, **save)
    return buffer.getvalue()


def _png_chunks(data: bytes) -> list[str]:
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    chunks, offset = [], 8
    while offset < len(data):
        (length,) = struct.unpack(">I", data[offset : offset + 4])
        chunks.append(data[offset + 4 : offset + 8].decode("ascii"))
        offset += 12 + length
    return chunks


def _decoded(data: bytes) -> Image.Image:
    image = Image.open(io.BytesIO(data))
    image.load()
    return image


@pytest.fixture
def processor() -> PillowSessionIconProcessor:
    return PillowSessionIconProcessor()


class TestContract:
    def test_etag_header_is_quoted(self) -> None:
        assert icon_etag_header("abc") == '"abc"'

    @pytest.mark.parametrize(
        ("header", "hit"),
        [
            ('"abc"', True),
            ('W/"abc"', True),
            ('"zzz", "abc"', True),
            ("*", True),
            ('"zzz"', False),
            ("abc", False),
            ("", False),
            (None, False),
        ],
    )
    def test_if_none_match(self, header, hit) -> None:
        assert if_none_match_hits(header, "abc") is hit

    def test_images_need_every_size(self) -> None:
        with pytest.raises(ValueError, match="missing"):
            SessionIconImages(etag="x", renditions={ICON_SIZE: b"png"})


class TestProcessor:
    def test_reencodes_to_square_pngs_of_every_size(self, processor) -> None:
        icon = processor.process(_image((300, 300), fmt="JPEG"))
        assert set(icon.renditions) == set(ICON_SIZES)
        for size, png in icon.renditions.items():
            decoded = _decoded(png)
            assert decoded.format == "PNG"
            assert decoded.size == (size, size)
        assert icon.etag == hashlib.sha256(icon.renditions[ICON_SIZE]).hexdigest()

    def test_small_squares_are_scaled_up(self, processor) -> None:
        icon = processor.process(_image((64, 64)))
        assert _decoded(icon.renditions[ICON_SIZE]).size == (ICON_SIZE, ICON_SIZE)

    def test_largest_square_is_accepted(self, processor) -> None:
        icon = processor.process(_image((2048, 2048)))
        assert _decoded(icon.renditions[128]).size == (128, 128)

    def test_same_upload_same_etag(self, processor) -> None:
        data = _image((200, 200), color="blue")
        assert processor.process(data).etag == processor.process(data).etag
        assert processor.process(data).etag != processor.process(_image((200, 200))).etag

    def test_transparency_is_kept(self, processor) -> None:
        icon = processor.process(_image((128, 128), mode="RGBA", color=(0, 0, 255, 0)))
        decoded = _decoded(icon.renditions[ICON_SIZE])
        assert decoded.mode == "RGBA"
        assert decoded.getpixel((10, 10))[3] == 0

    def test_heic_is_accepted(self, processor) -> None:
        icon = processor.process(_image((256, 256), fmt="HEIF"))
        assert _decoded(icon.renditions[ICON_SIZE]).size == (ICON_SIZE, ICON_SIZE)

    def test_metadata_is_stripped(self, processor) -> None:
        info = PngImagePlugin.PngInfo()
        info.add_text("Author", "someone private")
        exif = Image.Exif()
        exif[0x010E] = "secret description"
        source = io.BytesIO()
        image = Image.new("RGB", (100, 100), "green")
        image.save(source, format="PNG", pnginfo=info, exif=exif, icc_profile=b"\0" * 128)
        assert {"tEXt", "eXIf", "iCCP"} <= set(_png_chunks(source.getvalue()))
        for png in processor.process(source.getvalue()).renditions.values():
            assert set(_png_chunks(png)) <= {"IHDR", "IDAT", "IEND"}
            assert b"secret" not in png and b"private" not in png

    def test_jpeg_exif_is_stripped_and_orientation_applied(self, processor) -> None:
        # Left half red, right half blue; EXIF orientation 3 rotates it by 180 degrees.
        image = Image.new("RGB", (100, 100), "red")
        image.paste(Image.new("RGB", (50, 100), "blue"), (50, 0))
        exif = Image.Exif()
        exif[0x0112] = 3
        exif[0x010E] = "secret description"
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", exif=exif, quality=95)
        png = processor.process(buffer.getvalue()).renditions[ICON_SIZE]
        assert b"secret" not in png
        upright = _decoded(png).convert("RGB")
        left, right = upright.getpixel((20, 256)), upright.getpixel((490, 256))
        assert left[2] > left[0] and right[0] > right[2]

    @pytest.mark.parametrize("size", [(100, 99), (512, 256), (64, 65)])
    def test_not_square_is_422(self, processor, size) -> None:
        with pytest.raises(SessionIconError, match="icon must be 1:1") as raised:
            processor.process(_image(size))
        assert raised.value.status == 422
        assert f"{size[0]}x{size[1]}" in str(raised.value)

    @pytest.mark.parametrize("side", [63, 2049])
    def test_sides_outside_the_range_are_422(self, processor, side) -> None:
        with pytest.raises(SessionIconError, match="64 to 2048 px") as raised:
            processor.process(_image((side, side)))
        assert raised.value.status == 422

    @pytest.mark.parametrize(
        "data", [_image((100, 100), fmt="GIF"), _image((100, 100), fmt="WEBP"), b"not an image"]
    )
    def test_other_types_are_422(self, processor, data) -> None:
        with pytest.raises(SessionIconError, match="PNG, JPEG or HEIC") as raised:
            processor.process(data)
        assert raised.value.status == 422

    def test_empty_is_422(self, processor) -> None:
        with pytest.raises(SessionIconError, match="empty"):
            processor.process(b"")

    def test_over_two_mib_is_413(self, processor) -> None:
        with pytest.raises(SessionIconError, match="2 MiB") as raised:
            processor.process(b"\0" * (ICON_MAX_INPUT_BYTES + 1))
        assert raised.value.status == 413


# ------------------------------------------------------------------------------ storage


class TestRepository:
    async def test_set_bumps_once_and_clear_removes(self) -> None:
        repo = InMemorySessionRepository()
        session = await repo.create(Session(name="s"))
        icon = PillowSessionIconProcessor().process(_image())
        later = session.created_at.replace(year=2099)
        stored = await repo.set_icon(session.id, icon, later)
        assert stored.icon_etag == icon.etag and stored.updated_at == later
        again = await repo.set_icon(session.id, icon, later.replace(year=2100))
        assert again.updated_at == later
        assert await repo.get_icon(session.id, 256) == (icon.etag, icon.renditions[256])
        cleared = await repo.set_icon(session.id, None, later.replace(year=2101))
        assert cleared.icon_etag is None
        assert await repo.get_icon(session.id, 256) is None

    async def test_generic_update_never_overwrites_a_fresher_icon(self) -> None:
        repo = InMemorySessionRepository()
        session = await repo.create(Session(name="s"))
        stale = await repo.get(session.id)
        icon = PillowSessionIconProcessor().process(_image())
        await repo.set_icon(session.id, icon, session.created_at)
        saved = await repo.update(stale.model_copy(update={"message_count": 3}))
        assert saved.icon_etag == icon.etag

    async def test_missing_session(self) -> None:
        repo = InMemorySessionRepository()
        assert await repo.set_icon(uuid.uuid4(), None, None) is None


class _Conn:
    """Enough of an asyncpg connection to drive ``PostgresSessionRepository.set_icon``."""

    def __init__(self, row) -> None:
        self.row = row
        self.sql: list[tuple[str, tuple]] = []

    async def fetchrow(self, sql, *args):
        self.sql.append((sql, args))
        return self.row

    async def execute(self, sql, *args):
        self.sql.append((sql, args))

    async def executemany(self, sql, rows):
        self.sql.append((sql, tuple(rows)))

    def transaction(self):
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _Pool:
    def __init__(self, conn: _Conn, icon_row=None) -> None:
        self.conn = conn
        self.icon_row = icon_row
        self.fetched: list[tuple] = []

    def acquire(self):
        return self.conn

    async def fetchrow(self, sql, *args):
        self.fetched.append((sql, args))
        return self.icon_row


class TestPostgresRepository:
    @pytest.fixture(autouse=True)
    def _rows(self, monkeypatch):
        from volundr.adapters.outbound.postgres import PostgresSessionRepository

        monkeypatch.setattr(
            PostgresSessionRepository,
            "_row_to_session",
            lambda self, row: Session(name="s", icon_etag=row["icon_etag"]),
        )

    @staticmethod
    def _row(session_id, etag):
        return {"id": session_id, "icon_etag": etag}

    async def test_set_writes_session_row_then_every_rendition(self) -> None:
        from volundr.adapters.outbound.postgres import PostgresSessionRepository

        session_id = uuid.uuid4()
        icon = PillowSessionIconProcessor().process(_image())
        conn = _Conn(self._row(session_id, icon.etag))
        session = await PostgresSessionRepository(_Pool(conn)).set_icon(session_id, icon, None)
        assert session.icon_etag == icon.etag
        update, delete, insert = conn.sql
        assert update[0].lstrip().startswith("UPDATE sessions SET")
        assert "icon_etag IS DISTINCT FROM" in update[0]
        assert update[1][:2] == (session_id, icon.etag)
        assert delete[0].startswith("DELETE FROM session_icons")
        assert sorted(row[1] for row in insert[1]) == sorted(ICON_SIZES)
        assert all(row[2] == icon.etag for row in insert[1])

    async def test_clear_deletes_without_insert_and_missing_session_stops_early(self) -> None:
        from volundr.adapters.outbound.postgres import PostgresSessionRepository

        session_id = uuid.uuid4()
        conn = _Conn(self._row(session_id, None))
        session = await PostgresSessionRepository(_Pool(conn)).set_icon(session_id, None, None)
        assert session.icon_etag is None
        assert [sql.split()[0] for sql, _ in conn.sql] == ["UPDATE", "DELETE"]
        gone = _Conn(None)
        assert await PostgresSessionRepository(_Pool(gone)).set_icon(session_id, None, None) is None
        assert len(gone.sql) == 1

    async def test_get_icon(self) -> None:
        from volundr.adapters.outbound.postgres import PostgresSessionRepository

        session_id = uuid.uuid4()
        pool = _Pool(_Conn(None), icon_row={"etag": "e", "png": memoryview(b"png")})
        assert await PostgresSessionRepository(pool).get_icon(session_id, 128) == ("e", b"png")
        assert pool.fetched[0][1] == (session_id, 128)
        empty = _Pool(_Conn(None))
        assert await PostgresSessionRepository(empty).get_icon(session_id, 128) is None


# ------------------------------------------------------------------------------ REST


class RecordingBroadcaster(InMemoryEventBroadcaster):
    def __init__(self) -> None:
        super().__init__()
        self.events = []

    async def publish(self, event) -> None:
        self.events.append(event)
        await super().publish(event)

    def updates(self) -> list[dict]:
        return [e.data for e in self.events if e.type is EventType.SESSION_UPDATED]


@pytest.fixture
def broadcaster():
    return RecordingBroadcaster()


@pytest.fixture
def forge(broadcaster):
    return build_forge_app(broadcaster=broadcaster)


def _put(client, session_id, data, headers=OWNER_HEADERS):
    return client.put(
        f"{PREFIX}/sessions/{session_id}/icon",
        content=data,
        headers={**headers, "content-type": "image/png"},
    )


def _get(client, session_id, size=None, headers=OWNER_HEADERS):
    params = {"size": size} if size is not None else None
    return client.get(f"{PREFIX}/sessions/{session_id}/icon", params=params, headers=headers)


class TestRest:
    async def test_put_get_sizes_etag_and_304(self, forge, broadcaster) -> None:
        session = await forge.session("own")
        put = _put(forge.client, session.id, _image((300, 300)))
        assert put.status_code == 200, put.text
        etag = put.json()["icon_etag"]
        assert put.json() == {"icon_etag": etag, "size": 512}

        for size in (None, 512, 256, 128):
            got = _get(forge.client, session.id, size)
            assert got.status_code == 200, got.text
            assert got.headers["content-type"] == "image/png"
            assert got.headers["etag"] == f'"{etag}"'
            assert got.headers["cache-control"] == ICON_CACHE_CONTROL
            side = size or 512
            assert _decoded(got.content).size == (side, side)

        repeat = forge.client.get(
            f"{PREFIX}/sessions/{session.id}/icon",
            headers={**OWNER_HEADERS, "if-none-match": f'"{etag}"'},
        )
        assert repeat.status_code == 304
        assert repeat.content == b""
        assert repeat.headers["etag"] == f'"{etag}"'
        stale = forge.client.get(
            f"{PREFIX}/sessions/{session.id}/icon",
            headers={**OWNER_HEADERS, "if-none-match": '"old"'},
        )
        assert stale.status_code == 200

        [frame] = broadcaster.updates()
        assert frame["icon_etag"] == etag

    async def test_session_json_carries_icon_etag(self, forge) -> None:
        session = await forge.session("own")
        url = f"{PREFIX}/sessions/{session.id}"
        assert forge.client.get(url, headers=OWNER_HEADERS).json()["icon_etag"] is None
        before = forge.client.get(url, headers=OWNER_HEADERS).json()["updated_at"]
        etag = _put(forge.client, session.id, _image()).json()["icon_etag"]
        record = forge.client.get(url, headers=OWNER_HEADERS).json()
        assert record["icon_etag"] == etag
        assert record["updated_at"] > before
        listed = forge.client.get(f"{PREFIX}/sessions", headers=OWNER_HEADERS).json()
        assert [item["icon_etag"] for item in listed if item["id"] == str(session.id)] == [etag]
        summary = forge.client.get(
            f"{PREFIX}/sessions", params={"fields": "summary"}, headers=OWNER_HEADERS
        ).json()
        assert [item["icon_etag"] for item in summary if item["id"] == str(session.id)] == [etag]

    async def test_same_image_again_changes_nothing(self, forge, broadcaster) -> None:
        session = await forge.session("own")
        data = _image()
        first = _put(forge.client, session.id, data).json()
        stamp = (await forge.sessions.get(session.id)).updated_at
        second = _put(forge.client, session.id, data).json()
        assert first == second
        assert (await forge.sessions.get(session.id)).updated_at == stamp
        assert len(broadcaster.updates()) == 1

    async def test_replace_and_clear(self, forge, broadcaster) -> None:
        session = await forge.session("own")
        first = _put(forge.client, session.id, _image(color="red")).json()["icon_etag"]
        second = _put(forge.client, session.id, _image(color="blue")).json()["icon_etag"]
        assert first != second
        assert _get(forge.client, session.id).headers["etag"] == f'"{second}"'
        cleared = forge.client.delete(f"{PREFIX}/sessions/{session.id}/icon", headers=OWNER_HEADERS)
        assert cleared.status_code == 204
        assert _get(forge.client, session.id).status_code == 404
        assert (await forge.sessions.get(session.id)).icon_etag is None
        assert [frame["icon_etag"] for frame in broadcaster.updates()] == [first, second, None]
        again = forge.client.delete(f"{PREFIX}/sessions/{session.id}/icon", headers=OWNER_HEADERS)
        assert again.status_code == 204
        assert len(broadcaster.updates()) == 3

    async def test_no_icon_is_404(self, forge) -> None:
        session = await forge.session("own")
        response = _get(forge.client, session.id)
        assert response.status_code == 404
        assert "has no icon" in response.json()["detail"]

    async def test_unknown_session_is_404(self, forge) -> None:
        assert _get(forge.client, uuid.uuid4()).status_code == 404
        assert _put(forge.client, uuid.uuid4(), _image()).status_code == 404

    async def test_bad_size_is_422(self, forge) -> None:
        session = await forge.session("own")
        _put(forge.client, session.id, _image())
        response = _get(forge.client, session.id, 300)
        assert response.status_code == 422
        assert "512, 256, 128" in response.json()["detail"]

    async def test_refusals_say_what_to_fix(self, forge) -> None:
        session = await forge.session("own")
        wide = _put(forge.client, session.id, _image((200, 100)))
        assert wide.status_code == 422
        assert "icon must be 1:1" in wide.json()["detail"]
        gif = _put(forge.client, session.id, _image(fmt="GIF"))
        assert gif.status_code == 422
        huge = _put(forge.client, session.id, b"\0" * (ICON_MAX_INPUT_BYTES + 1))
        assert huge.status_code == 413
        assert (await forge.sessions.get(session.id)).icon_etag is None

    async def test_a_chunked_body_is_bounded_too(self, forge) -> None:
        session = await forge.session("own")

        def chunks():
            for _ in range(3):
                yield b"\0" * (ICON_MAX_INPUT_BYTES // 2)

        response = forge.client.put(
            f"{PREFIX}/sessions/{session.id}/icon", content=chunks(), headers=OWNER_HEADERS
        )
        assert response.status_code == 413

    def test_response_maps_the_etag(self) -> None:
        session = Session(name="s", icon_etag="abc")
        assert SessionResponse.from_session(session).icon_etag == "abc"
        assert SessionResponse.from_session(Session(name="t")).icon_etag is None


class TestSessionToken:
    async def _pair(self, forge):
        own = await forge.session("own")
        peer = await forge.session("peer")
        return own, peer, (await forge.start(own)).token

    async def test_own_token_sets_reads_and_clears_its_icon(self, forge) -> None:
        own, _, token = await self._pair(forge)
        put = _put(forge.client, own.id, _image(), headers=bearer(token))
        assert put.status_code == 200, put.text
        assert _get(forge.client, own.id, 128, headers=bearer(token)).status_code == 200
        cleared = forge.client.delete(f"{PREFIX}/sessions/{own.id}/icon", headers=bearer(token))
        assert cleared.status_code == 204

    async def test_own_token_may_not_set_a_peers_icon_but_may_read_it(self, forge) -> None:
        _, peer, token = await self._pair(forge)
        response = _put(forge.client, peer.id, _image(), headers=bearer(token))
        assert response.status_code == 403
        assert (await forge.sessions.get(peer.id)).icon_etag is None
        deleted = forge.client.delete(f"{PREFIX}/sessions/{peer.id}/icon", headers=bearer(token))
        assert deleted.status_code == 403
        _put(forge.client, peer.id, _image())
        assert _get(forge.client, peer.id, headers=bearer(token)).status_code == 200


def _mcp(forge, token, arguments):
    message = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "tools/call",
        "params": {"name": "set_session_icon", "arguments": arguments},
    }
    response = forge.client.post(
        f"{PREFIX}/mcp", json=message, headers={**JSON_RPC, **bearer(token)}
    )
    return response.json()["result"]


class TestForgeMcp:
    async def test_clear_over_the_forge_hosted_mcp(self, forge) -> None:
        own = await forge.session("own")
        token = (await forge.start(own)).token
        _put(forge.client, own.id, _image())
        cleared = _mcp(forge, token, {"clear": True})
        assert cleared["isError"] is False, cleared
        assert cleared["structuredContent"]["cleared"] is True
        assert (await forge.sessions.get(own.id)).icon_etag is None

    @pytest.mark.parametrize(
        "arguments", [{"path": "/tmp/icon.png"}, {"file_id": "pf_" + "a" * 32}]
    )
    async def test_files_need_the_sessions_own_broker(self, forge, arguments) -> None:
        own = await forge.session("own")
        token = (await forge.start(own)).token
        result = _mcp(forge, token, arguments)
        assert result["isError"] is True
        assert "session's own Forge MCP" in result["content"][0]["text"]

    @pytest.mark.parametrize(
        "arguments",
        [{}, {"path": "/a.png", "clear": True}, {"file_id": "nope"}, {"path": "  "}],
    )
    async def test_arguments_are_validated(self, forge, arguments) -> None:
        own = await forge.session("own")
        token = (await forge.start(own)).token
        assert _mcp(forge, token, arguments)["isError"] is True


class TestFacade:
    """Through the Guild facade, as the phone and the Mac read it."""

    async def test_put_get_304_and_delete(self, forge) -> None:
        from tests.test_niuu.test_rest_volundr import _client, _headers, _instance

        local = _instance(
            "local",
            base_url="embedded://local-forge",
            is_default=True,
            config={"transport": "embedded"},
        )
        facade = _client([local], embedded_forge_app=forge.app)
        created = facade.post(f"{PREFIX}/sessions", json={"name": "fleet"}, headers=_headers())
        assert created.status_code == 201, created.text
        session_id = created.json()["id"]
        url = f"{PREFIX}/sessions/{session_id}/icon"

        put = facade.put(
            url, content=_image((256, 256)), headers={**_headers(), "content-type": "image/png"}
        )
        assert put.status_code == 200, put.text
        etag = put.json()["icon_etag"]
        record = facade.get(f"{PREFIX}/sessions/{session_id}", headers=_headers()).json()
        assert record["icon_etag"] == etag

        got = facade.get(url, params={"size": 256}, headers=_headers())
        assert got.status_code == 200
        assert got.headers["etag"] == f'"{etag}"'
        assert got.headers["cache-control"] == ICON_CACHE_CONTROL
        assert _decoded(got.content).size == (256, 256)
        cached = facade.get(url, headers={**_headers(), "if-none-match": f'"{etag}"'})
        assert cached.status_code == 304
        assert cached.headers["etag"] == f'"{etag}"'

        wide = facade.put(url, content=_image((20, 10)), headers=_headers())
        assert wide.status_code == 422
        assert "1:1" in wide.text
        huge = facade.put(url, content=b"\0" * (ICON_MAX_INPUT_BYTES + 1), headers=_headers())
        assert huge.status_code == 413

        assert facade.delete(url, headers=_headers()).status_code == 204
        assert facade.get(url, headers=_headers()).status_code == 404
