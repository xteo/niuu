"""F2 at the broker: present-file hands Forge a durable copy and records its URL."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from skuld.config import PresentedDurableConfig
from skuld.presented_durable import (
    DURABLE_MARKER,
    durable_record,
    retry_presented_upload,
    upload_presented_file,
)

URL = "http://thor.tail737f2a.ts.net:8380/api/v1/forge/sessions/s/files/presented/pf_x"


def _broker(response=None, exc=None, *, api="http://forge", retry_attempts=2):
    client = MagicMock()
    if exc is not None:
        client.put = AsyncMock(side_effect=exc)
    else:
        client.put = AsyncMock(return_value=response)
    return SimpleNamespace(
        volundr_api_url=api,
        session_id="11111111-1111-1111-1111-111111111111",
        _get_http_client=AsyncMock(return_value=client),
        _settings=SimpleNamespace(
            presented_durable=PresentedDurableConfig(
                retry_attempts=retry_attempts,
                retry_backoff_seconds=0.001,
                retry_backoff_max_seconds=0.002,
            )
        ),
        client=client,
    )


def _ok(url=URL):
    response = MagicMock(status_code=200)
    response.json.return_value = {"url": url, "path": "/p", "sha256": "x"}
    return response


@pytest.fixture
def staged(tmp_path):
    path = tmp_path / "pf_x" / "content"
    path.parent.mkdir()
    path.write_bytes(b"%PDF")
    return path


async def test_upload_puts_bytes_and_writes_the_receipt(staged):
    broker = _broker(_ok())
    receipt = await upload_presented_file(
        broker, "pf_x", staged, name="My report.pdf", mime="application/pdf"
    )
    assert receipt["url"] == URL
    call = broker.client.put.await_args
    assert call.args[0].endswith(
        "/sessions/11111111-1111-1111-1111-111111111111/files/presented/pf_x"
    )
    assert call.kwargs["content"] == b"%PDF"
    assert call.kwargs["headers"]["x-presented-file-name"] == "My%20report.pdf"
    assert durable_record(staged)["url"] == URL
    # Already durable: no second upload.
    await upload_presented_file(broker, "pf_x", staged, name="n", mime="m")
    assert broker.client.put.await_count == 1


@pytest.mark.parametrize(
    "broker",
    [
        _broker(exc=OSError("down")),
        _broker(MagicMock(status_code=403)),
        _broker(api=""),
    ],
)
async def test_upload_failures_return_none(staged, broker):
    assert await upload_presented_file(broker, "pf_x", staged, name="n", mime="m") is None
    assert not (staged.parent / DURABLE_MARKER).exists()


async def test_unparseable_receipt_is_a_failure(staged):
    response = MagicMock(status_code=200)
    response.json.side_effect = ValueError
    assert (
        await upload_presented_file(_broker(response), "pf_x", staged, name="n", mime="m") is None
    )
    response.json.side_effect = None
    response.json.return_value = ["not", "a", "dict"]
    assert (
        await upload_presented_file(_broker(response), "pf_x", staged, name="n", mime="m") is None
    )


async def test_retry_until_forge_accepts(staged):
    broker = _broker()
    broker.client.put = AsyncMock(side_effect=[OSError("down"), _ok()])
    receipt = await retry_presented_upload(broker, "pf_x", staged, name="n", mime="m")
    assert receipt["url"] == URL
    assert broker.client.put.await_count == 2


async def test_retry_gives_up_after_the_configured_attempts(staged):
    broker = _broker(exc=OSError("down"), retry_attempts=3)
    assert await retry_presented_upload(broker, "pf_x", staged, name="n", mime="m") is None
    assert broker.client.put.await_count == 3


def test_durable_record_tolerates_garbage(tmp_path):
    staged = tmp_path / "content"
    assert durable_record(staged) is None
    (tmp_path / DURABLE_MARKER).write_text("[1]")
    assert durable_record(staged) is None


@pytest.fixture
def client(tmp_path, monkeypatch, module_broker_runtime):
    from skuld import broker as bmod
    from skuld import broker_api as api_mod

    monkeypatch.setattr(api_mod, "_presented_staging_dir", lambda: tmp_path / "staging")
    monkeypatch.setattr(bmod.broker, "workspace_dir", str(tmp_path))

    async def _fake_broadcast(frame):
        return None

    monkeypatch.setattr(bmod.broker, "_enqueue_event_log", lambda *_: None)
    monkeypatch.setattr(bmod.broker, "_save_conversation_history", lambda: None)
    monkeypatch.setattr(bmod.broker._channels, "broadcast", _fake_broadcast)
    monkeypatch.setattr(bmod.broker, "_conversation_turns", [])
    api_mod._presented_registry.clear()
    return TestClient(
        bmod.app,
        headers={
            "authorization": module_broker_runtime.authorization,
            "x-niuu-room-role": "owner",
        },
    )


def test_present_file_records_the_durable_url(client, tmp_path, monkeypatch):
    from skuld import broker as bmod
    from skuld import broker_api as api_mod

    async def fake_upload(broker, file_id, staged, *, name, mime, timeout=None):
        assert timeout == broker._settings.presented_durable.inline_upload_timeout_seconds
        assert staged.read_bytes() == b"hello"
        return {"url": URL}

    monkeypatch.setattr(api_mod, "upload_presented_file", fake_upload)
    src = tmp_path / "notes.txt"
    src.write_bytes(b"hello")
    body = client.post("/api/present-file", json={"path": str(src)}).json()
    assert body["url"] == URL and body["durable"] is True
    part = bmod.broker._conversation_turns[-1].parts[0]
    assert part["input"]["url"] == URL


def test_present_file_still_succeeds_when_forge_is_down(client, tmp_path, monkeypatch):
    from skuld import broker as bmod
    from skuld import broker_api as api_mod

    scheduled: list = []

    async def failing_upload(*args, **kwargs):
        return None

    async def fake_retry(*args, **kwargs):
        scheduled.append(args[1])

    monkeypatch.setattr(api_mod, "upload_presented_file", failing_upload)
    monkeypatch.setattr(api_mod, "retry_presented_upload", fake_retry)
    monkeypatch.setattr(bmod.broker, "volundr_api_url", "http://forge")
    src = tmp_path / "notes.txt"
    src.write_bytes(b"hello")
    resp = client.post("/api/present-file", json={"path": str(src)})
    assert resp.status_code == 200
    body = resp.json()
    assert body["durable"] is False and "url" not in body
    assert "url" not in bmod.broker._conversation_turns[-1].parts[0]["input"]
    assert scheduled == [body["file_id"]]


def test_registry_rebuild_never_serves_the_receipt(tmp_path, monkeypatch):
    from skuld import broker_api as api_mod

    root = tmp_path / "staging"
    entry = root / ("pf_" + "b" * 32)
    entry.mkdir(parents=True)
    (entry / DURABLE_MARKER).write_text('{"url": "x"}')
    (entry / "content").write_bytes(b"bytes")
    monkeypatch.setattr(api_mod, "_presented_staging_dir", lambda: root)
    api_mod._rebuild_presented_registry()
    assert api_mod._presented_registry[entry.name].endswith("/content")
