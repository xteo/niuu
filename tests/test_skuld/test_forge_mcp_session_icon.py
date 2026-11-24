"""set_session_icon plumbing: the REST client calls and the broker's file readers (LEX-2629)."""

from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from niuu.domain.session_icon import ICON_MAX_INPUT_BYTES
from niuu.forge_mcp.models import ForgeApiError
from niuu.forge_mcp.rest_client import RestForgeClient
from skuld.forge_mcp.host import BrokerForgeMcpHost

FILE_ID = "pf_" + "0123456789abcdef" * 2


def _client(handler) -> RestForgeClient:
    async def factory() -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="http://forge")

    return RestForgeClient(
        http_client=factory, headers={"Authorization": "Bearer scoped"}, timeout_s=5
    )


class TestRestClient:
    async def test_set_icon_puts_the_raw_bytes(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(200, json={"icon_etag": "abc", "size": 512})

        stored = await _client(handler).set_icon("s-1", b"\x89PNG")
        assert stored == {"icon_etag": "abc", "size": 512}
        [request] = seen
        assert (request.method, request.url.path) == ("PUT", "/api/v1/forge/sessions/s-1/icon")
        assert request.content == b"\x89PNG"
        assert request.headers["authorization"] == "Bearer scoped"
        assert request.headers["content-type"] == "application/octet-stream"

    async def test_clear_icon_deletes(self) -> None:
        seen: list[httpx.Request] = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request)
            return httpx.Response(204)

        await _client(handler).clear_icon("s-1")
        assert (seen[0].method, seen[0].url.path) == ("DELETE", "/api/v1/forge/sessions/s-1/icon")

    async def test_refusal_carries_forges_detail(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"detail": "icon must be 1:1 (square)"})

        with pytest.raises(ForgeApiError) as raised:
            await _client(handler).set_icon("s-1", b"x")
        assert raised.value.status == 422 and "1:1" in raised.value.detail

    async def test_unreachable_forge(self) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("refused")

        with pytest.raises(ForgeApiError, match="ConnectError"):
            await _client(handler).clear_icon("s-1")

    @pytest.mark.parametrize(
        "response",
        [httpx.Response(200, text="not json"), httpx.Response(200, json=["a"])],
    )
    async def test_bad_answers_raise(self, response) -> None:
        with pytest.raises(ForgeApiError):
            await _client(lambda _request: response).set_icon("s-1", b"x")


@pytest.fixture
def host(tmp_path):
    workspace, home = tmp_path / "ws", tmp_path / "home"
    workspace.mkdir()
    home.mkdir()
    staging = tmp_path / "staging"
    broker = SimpleNamespace(
        session_id="s-1",
        workspace_dir=str(workspace),
        _settings=SimpleNamespace(home_path=str(home), presented_files_path=str(staging)),
    )
    return BrokerForgeMcpHost(broker, client=None), workspace, home, staging


class TestBrokerReadsIconFiles:
    async def test_reads_from_workspace_and_home(self, host) -> None:
        broker_host, workspace, home, _ = host
        (workspace / "a.png").write_bytes(b"ws")
        (home / "b.png").write_bytes(b"home")
        assert await broker_host.read_icon_file(str(workspace / "a.png")) == b"ws"
        assert await broker_host.read_icon_file(str(home / "b.png")) == b"home"

    async def test_refuses_outside_the_roots_and_through_symlinks(self, host, tmp_path) -> None:
        broker_host, workspace, _, _ = host
        outside = tmp_path / "outside.png"
        outside.write_bytes(b"x")
        (workspace / "link.png").symlink_to(outside)
        for path in (str(outside), str(workspace / "link.png"), str(workspace / ".." / "x")):
            with pytest.raises(ForgeApiError, match="outside the session roots") as raised:
                await broker_host.read_icon_file(path)
            assert raised.value.status == 400

    async def test_missing_and_directories(self, host) -> None:
        broker_host, workspace, _, _ = host
        with pytest.raises(ForgeApiError, match="no such file"):
            await broker_host.read_icon_file(str(workspace / "absent.png"))
        with pytest.raises(ForgeApiError, match="no such file"):
            await broker_host.read_icon_file(str(workspace))

    async def test_too_large_is_refused_before_reading(self, host) -> None:
        broker_host, workspace, _, _ = host
        big = workspace / "big.png"
        big.write_bytes(b"\0" * (ICON_MAX_INPUT_BYTES + 1))
        with pytest.raises(ForgeApiError, match="2 MiB") as raised:
            await broker_host.read_icon_file(str(big))
        assert raised.value.status == 413


class TestBrokerReadsPresentedFiles:
    async def test_reads_the_staged_copy(self, host) -> None:
        broker_host, _, _, staging = host
        (staging / FILE_ID).mkdir(parents=True)
        (staging / FILE_ID / "content").write_bytes(b"staged")
        assert await broker_host.read_presented_file(FILE_ID) == b"staged"

    async def test_unknown_and_invalid_ids(self, host) -> None:
        broker_host, _, _, staging = host
        staging.mkdir()
        with pytest.raises(ForgeApiError, match="no presented file") as raised:
            await broker_host.read_presented_file(FILE_ID)
        assert raised.value.status == 404
        with pytest.raises(ForgeApiError, match="not a present-file id") as raised:
            await broker_host.read_presented_file("../etc/passwd")
        assert raised.value.status == 400

    async def test_a_directory_in_place_of_the_copy_is_gone(self, host) -> None:
        broker_host, _, _, staging = host
        (staging / FILE_ID / "content").mkdir(parents=True)
        with pytest.raises(ForgeApiError, match="no longer available") as raised:
            await broker_host.read_presented_file(FILE_ID)
        assert raised.value.status == 410

    async def test_too_large(self, host) -> None:
        broker_host, _, _, staging = host
        (staging / FILE_ID).mkdir(parents=True)
        (staging / FILE_ID / "content").write_bytes(b"\0" * (ICON_MAX_INPUT_BYTES + 1))
        with pytest.raises(ForgeApiError, match="2 MiB"):
            await broker_host.read_presented_file(FILE_ID)
