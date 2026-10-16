"""LEX-1259: ``GET /sessions/{id}/attachments`` — one call, every document and image.

The fixture is a durable event log (``conversation.turn`` rows plus a raw in-progress
tail) driven through the real REST router, ``SessionService`` and
``SessionArchiveService`` over in-memory repos, so the index is built from the same
``rebuild_turns`` fold the conversation route falls back to.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from uuid import uuid4

import pytest

from tests.test_domain.test_session_archive_service import InMemorySessionEventLog
from tests.test_volundr.test_conversation_db_fallback import (
    _build_client,
    _strip_ambient_env,  # noqa: F401 - autouse fixture, hermetic env
)
from volundr.domain.models import Session, SessionLogEntry, SessionStatus
from volundr.domain.services import SessionArchiveService
from volundr.domain.services.attachments_index import (
    build_attachments_index,
    filter_attachments,
)

_PATH = "/api/v1/forge/sessions/{sid}/attachments"
_NOW = datetime(2026, 10, 16, 10, 4, 0, tzinfo=UTC)
_FULL_ENVELOPE = json.dumps(
    {
        "type": "image",
        "file": {
            "base64": "A" * 4096,
            "type": "image/webp",
            "originalSize": 3072,
            "dimensions": {"displayWidth": 640, "displayHeight": 480},
        },
    }
)
_SEND_USER_FILE_RESULT = json.dumps(
    {
        "attachments": [
            {
                "file_uuid": "feb5e108-7543-4660-a114-d61def541c2d",
                "isImage": False,
                "media_type": "text/markdown",
                "path": "/w/report.md",
                "size": 512,
            }
        ],
        "caption": "The report",
    }
)


def _read(call_id: str, path: str) -> dict:
    return {"type": "tool_use", "id": call_id, "name": "Read", "input": {"file_path": path}}


def _placeholder(call_id: str, **hint) -> dict:
    return {
        "type": "tool_result",
        "tool_use_id": call_id,
        "is_error": False,
        "truncated": True,
        "byte_size": 90_000,
        "preview": "iVBORw0KGgo",
        **hint,
    }


def _turn_row(session_id, seq: int, turn: dict) -> SessionLogEntry:
    return SessionLogEntry(
        session_id=session_id,
        seq=seq,
        kind="conversation.turn",
        payload={"turn": {"created_at": _NOW.isoformat(), "metadata": {}, **turn}},
        ts=_NOW,
        role=turn["role"],
    )


def _present_file_turn(turn_id: str, file_id: str, name: str, mime: str, **extra) -> dict:
    return {
        "id": turn_id,
        "role": "assistant",
        "content": name,
        "parts": [
            {
                "id": file_id,
                "name": "present_file",
                "type": "tool_use",
                "input": {"file_id": file_id, "name": name, "mime": mime, "size": 2048, **extra},
            }
        ],
        "metadata": {"present_file": True},
    }


def _frames(session_id) -> list[SessionLogEntry]:
    reads = {
        "id": "t-reads",
        "role": "assistant",
        "content": "Looked at the files.",
        "parts": [
            _read("r_hint", "/w/shot.png"),
            _placeholder("r_hint", is_image=True, mime_type="image/png", img_w=1200, img_h=800),
            _read("r_nohint", "/w/old.jpg"),
            _placeholder("r_nohint"),
            _read("r_full", "/w/full.webp"),
            {"type": "tool_result", "tool_use_id": "r_full", "content": _FULL_ENVELOPE},
            _read("r_text", "/w/main.py"),
            {"type": "tool_result", "tool_use_id": "r_text", "content": "print(1)\n" * 400},
            _read("r_text_elided", "/w/notes.md"),
            _placeholder("r_text_elided"),
            _read("r_error", "/w/missing.png"),
            {"type": "tool_result", "tool_use_id": "r_error", "is_error": True, "content": "no"},
        ],
    }
    send_user_file = {
        "id": "t-send",
        "role": "assistant",
        "content": "Delivered.",
        "parts": [
            {
                "type": "tool_use",
                "id": "su1",
                "name": "SendUserFile",
                "input": {"files": ["/w/report.md"], "caption": "The report"},
            },
            {"type": "tool_result", "tool_use_id": "su1", "content": _SEND_USER_FILE_RESULT},
        ],
    }
    rows = [
        _turn_row(session_id, 1, {"id": "u1", "role": "user", "content": "show me", "parts": []}),
        _turn_row(session_id, 2, reads),
        _turn_row(
            session_id,
            3,
            _present_file_turn("t-doc", "pf_doc", "plan.md", "text/markdown", caption="Plan"),
        ),
        _turn_row(session_id, 4, _present_file_turn("t-png", "pf_png", "chart.png", "image/png")),
        _turn_row(session_id, 5, send_user_file),
        _turn_row(session_id, 6, {"id": "u2", "role": "user", "content": "more", "parts": []}),
    ]
    # The running agent's in-progress tail: a SendUserFile call with no result yet.
    rows.append(
        SessionLogEntry(
            session_id=session_id,
            seq=7,
            kind="assistant",
            payload={
                "message": {
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "su_live",
                            "name": "SendUserFile",
                            "input": {"files": ["/w/live.pdf"]},
                        }
                    ]
                }
            },
            ts=_NOW,
            role="assistant",
            request_id="req-live",
        )
    )
    return rows


async def _client_for(frames_for):
    sid = uuid4()
    event_log = InMemorySessionEventLog(frames_for(sid))
    session = Session(id=sid, name="attachments", model="m", status=SessionStatus.STOPPED)
    client, repository = _build_client(session, event_log)
    await repository.create(session)
    return client, sid, event_log


@pytest.mark.asyncio
async def test_index_lists_every_attachment_in_turn_then_block_order():
    client, sid, _ = await _client_for(_frames)

    response = client.get(_PATH.format(sid=sid))

    assert response.status_code == 200
    body = response.json()
    assert body["session_id"] == str(sid)
    assert body["revision"] == "7"
    assert body["complete"] is True
    ids = [(i["tool_use_id"], i["kind"], i["carrier"]) for i in body["items"]]
    assert ids == [
        ("r_hint", "image", "read"),
        ("r_nohint", "image", "read"),
        ("r_full", "image", "read"),
        ("pf_doc", "document", "present_file"),
        ("pf_png", "image", "present_file"),
        ("su1", "document", "send_user_file"),
        ("su_live", "document", "send_user_file"),
    ]
    turn_seqs = [i["turn_seq"] for i in body["items"]]
    assert turn_seqs == sorted(turn_seqs)


@pytest.mark.asyncio
async def test_item_fields_follow_the_contract():
    client, sid, _ = await _client_for(_frames)

    items = {i["tool_use_id"]: i for i in client.get(_PATH.format(sid=sid)).json()["items"]}

    hinted = items["r_hint"]
    assert hinted["name"] == "shot.png" and hinted["mime"] == "image/png"
    assert hinted["image"] == {"w": 1200, "h": 800}
    assert hinted["preview_url"] == f"/api/v1/forge/sessions/{sid}/tool-result/r_hint/preview"
    assert hinted["turn_id"] == "t-reads" and hinted["at"] == _NOW.isoformat()
    assert "file_id" not in hinted

    assert items["r_nohint"]["mime"] == "image/jpeg" and "image" not in items["r_nohint"]
    assert items["r_full"]["mime"] == "image/webp"
    assert items["r_full"]["image"] == {"w": 640, "h": 480}

    doc = items["pf_doc"]
    assert doc == {
        "kind": "document",
        "carrier": "present_file",
        "tool_use_id": "pf_doc",
        "file_id": "pf_doc",
        "name": "plan.md",
        "mime": "text/markdown",
        "size": 2048,
        "caption": "Plan",
        "turn_id": "t-doc",
        "turn_seq": doc["turn_seq"],
        "at": _NOW.isoformat(),
    }
    png = items["pf_png"]
    assert png["preview_url"] == f"/api/v1/forge/sessions/{sid}/files/presented/pf_png"

    sent = items["su1"]
    assert sent["file_id"] == "feb5e108-7543-4660-a114-d61def541c2d"
    assert sent["name"] == "report.md" and sent["mime"] == "text/markdown"
    assert sent["size"] == 512 and sent["caption"] == "The report"

    live = items["su_live"]
    assert live["name"] == "live.pdf" and live["mime"] == "application/pdf"
    assert "file_id" not in live


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("kind", "expected"),
    [
        ("document", ["pf_doc", "su1", "su_live"]),
        ("image", ["r_hint", "r_nohint", "r_full", "pf_png"]),
    ],
)
async def test_kind_filter(kind, expected):
    client, sid, _ = await _client_for(_frames)

    body = client.get(_PATH.format(sid=sid), params={"kind": kind}).json()

    assert [i["tool_use_id"] for i in body["items"]] == expected


@pytest.mark.asyncio
async def test_bad_kind_is_rejected():
    client, sid, _ = await _client_for(_frames)

    assert client.get(_PATH.format(sid=sid), params={"kind": "video"}).status_code == 400


@pytest.mark.asyncio
async def test_unknown_session_is_404():
    client, _, _ = await _client_for(_frames)

    assert client.get(_PATH.format(sid=uuid4())).status_code == 404


@pytest.mark.asyncio
async def test_index_is_cached_on_head_seq_and_refreshes_when_the_log_grows():
    client, sid, event_log = await _client_for(_frames)
    first = client.get(_PATH.format(sid=sid)).json()

    reads = 0
    original = event_log.read_after

    async def counting_read_after(*args, **kwargs):
        nonlocal reads
        reads += 1
        return await original(*args, **kwargs)

    event_log.read_after = counting_read_after
    assert client.get(_PATH.format(sid=sid)).json() == first
    assert reads == 0

    await event_log.append(
        [_turn_row(sid, 8, _present_file_turn("t-late", "pf_late", "late.txt", "text/plain"))]
    )
    refreshed = client.get(_PATH.format(sid=sid)).json()
    assert reads > 0
    assert refreshed["revision"] == "8"
    assert refreshed["items"][-1]["tool_use_id"] == "pf_late"


@pytest.mark.asyncio
async def test_truncated_log_reports_incomplete(monkeypatch):
    client, sid, _ = await _client_for(_frames)
    original = SessionArchiveService.get_attachments

    async def small_window(self, session_id, max_frames=50_000):
        return await original(self, session_id, max_frames=3)

    monkeypatch.setattr(SessionArchiveService, "get_attachments", small_window)

    assert client.get(_PATH.format(sid=sid)).json()["complete"] is False


@pytest.mark.asyncio
async def test_session_without_any_history_has_no_items():
    client, sid, _ = await _client_for(lambda _sid: [])

    body = client.get(_PATH.format(sid=sid)).json()

    assert body["items"] == [] and body["complete"] is True


@pytest.mark.asyncio
async def test_feature_flag_advertises_the_index():
    client, _, _ = await _client_for(_frames)
    client.app.state.storage = type("S", (), {"supports_home_volumes": False})()

    assert client.get("/api/v1/forge/feature-flags").json()["attachments_index"] is True


def test_builder_handles_content_arrays_and_orphan_results():
    turns = [
        {
            "id": "t1",
            "role": "assistant",
            "parts": [
                {
                    "type": "tool_result",
                    "tool_use_id": "orphan",
                    "content": [{"type": "image", "source": {"media_type": "image/gif"}}],
                },
                {"type": "tool_use", "id": "codex", "name": "view_image", "input": {}},
                {
                    "type": "tool_result",
                    "tool_use_id": "codex",
                    "content": [{"type": "input_image", "image_url": "data:image/png;base64,AAAA"}],
                },
                {"type": "tool_use", "id": "mcp", "name": "screenshot", "input": {}},
                {
                    "type": "tool_result",
                    "tool_use_id": "mcp",
                    "content": [{"type": "image", "data": "AAAA", "mimeType": "image/jpeg"}],
                },
                {"type": "tool_use", "id": "sent", "name": "SendUserFile", "input": {}},
                {
                    "type": "tool_result",
                    "tool_use_id": "sent",
                    "content": [
                        {
                            "type": "text",
                            "text": json.dumps(
                                {"attachments": [{"path": "/w/a.png", "media_type": "image/png"}]}
                            ),
                        }
                    ],
                },
                {"type": "tool_use", "id": "failed", "name": "SendUserFile", "input": {}},
                {"type": "tool_result", "tool_use_id": "failed", "is_error": True},
                {
                    "type": "tool_use",
                    "id": "pf_x",
                    "name": "present_file",
                    "input": {"_elided_input": True, "preview": "x"},
                },
                {"type": "tool_use", "id": "noimg", "name": "Bash", "input": {}},
                {"type": "tool_result", "tool_use_id": "noimg", "content": [{"type": "text"}]},
            ],
        },
        "not-a-turn",
        {"id": "t2", "parts": None},
    ]

    items = build_attachments_index(turns, "s")

    by_id = {i["tool_use_id"]: i for i in items}
    assert list(by_id) == ["orphan", "codex", "mcp", "sent", "pf_x"]
    assert by_id["orphan"]["mime"] == "image/gif" and by_id["orphan"]["name"] == "orphan"
    assert by_id["codex"]["mime"] == "image/png"
    assert by_id["mcp"]["mime"] == "image/jpeg"
    assert by_id["sent"]["kind"] == "image" and "file_id" not in by_id["sent"]
    assert by_id["pf_x"]["file_id"] == "pf_x" and by_id["pf_x"]["kind"] == "document"
    assert filter_attachments(items, None) == items


def test_builder_ignores_non_image_shapes():
    turns = [
        {
            "id": "t1",
            "role": "assistant",
            "parts": [
                {"type": "tool_use", "id": "a", "name": "Read", "input": "x"},
                {"type": "tool_result", "tool_use_id": "a", "content": '{"type": "text"}'},
                {"type": "tool_use", "id": "b", "name": "Read", "input": {}},
                {"type": "tool_result", "tool_use_id": "b", "content": 7},
                {"type": "tool_use", "id": "c", "name": "SendUserFile", "input": {}},
                {"type": "tool_result", "tool_use_id": "c", "content": "not json"},
                {"type": "tool_use", "id": "d", "name": "SendUserFile", "input": {}},
                {"type": "tool_result", "tool_use_id": "d", "content": "x" * 70_000},
                {"type": "tool_use", "id": "e", "name": "SendUserFile", "input": {}},
                {"type": "tool_result", "tool_use_id": "e", "content": "[1]"},
                {"type": "tool_use", "id": "f", "name": "SendUserFile", "input": {}},
                {"type": "tool_result", "tool_use_id": "f", "content": '{"attachments": 1}'},
                {"type": "tool_use", "id": "g", "name": "view", "input": {}},
                {
                    "type": "tool_result",
                    "tool_use_id": "g",
                    "content": [{"type": "image_url", "image_url": {"url": "https://x/y.png"}}],
                },
                {"type": "tool_use", "id": "h", "name": "Read", "input": {"_elided_input": True}},
                _placeholder("h"),
                "not-a-block",
            ],
        }
    ]

    assert build_attachments_index(turns, "s") == []


class _NoopStorage:
    def resolve_session_workspace_path(self, _session_id: str) -> str | None:
        return None

    async def get_workspace_by_session(self, _session_id: str):
        return None


@pytest.mark.asyncio
async def test_service_falls_back_to_the_stored_transcript_without_an_event_log(monkeypatch):
    service = SessionArchiveService(None, _NoopStorage(), None)
    turn = _present_file_turn("t-doc", "pf_doc", "plan.md", "text/markdown")

    async def stored_transcript(_session_id):
        return {"turns": [turn]}

    monkeypatch.setattr(service, "get_transcript", stored_transcript)

    index = await service.get_attachments(uuid4())

    assert [i["file_id"] for i in index["items"]] == ["pf_doc"]
    assert index["complete"] is True and index["revision"]


@pytest.mark.asyncio
async def test_service_cache_is_lru_bounded():
    sids = [uuid4(), uuid4()]
    event_log = InMemorySessionEventLog([row for sid in sids for row in _frames(sid)])
    service = SessionArchiveService(
        None, _NoopStorage(), None, event_log_repository=event_log, attachments_cache_size=1
    )

    for sid in sids:
        await service.get_attachments(sid)

    assert list(service._attachments_cache) == [str(sids[1])]
