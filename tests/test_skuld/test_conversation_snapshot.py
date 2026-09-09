"""Reconnect snapshots must fit deployed iOS receivers without erasing history."""

import copy
import json
from unittest.mock import AsyncMock

import pytest
from starlette.websockets import WebSocket

from skuld.broker import ConversationTurn
from skuld.conversation_snapshot import (
    ConversationSnapshotTooLargeError,
    prepare_conversation_snapshot,
    snapshot_byte_size,
)
from skuld.transports import TransportCapabilities
from tests.test_skuld.test_event_log import _broker


def _history(output="small output"):
    return {
        "type": "conversation_history",
        "head_seq": 321,
        "turns": [
            {"id": "human", "role": "user", "content": "Keep this request", "parts": []},
            {
                "id": "assistant",
                "role": "assistant",
                "content": "Keep this answer",
                "parts": [
                    {
                        "type": "tool_use",
                        "id": "native-call-1",
                        "name": "Bash",
                        "input": {"command": "echo synthetic"},
                    },
                    {
                        "type": "tool_result",
                        "tool_use_id": "native-call-1",
                        "content": output,
                        "is_error": False,
                    },
                    {"type": "text", "text": "Keep this answer"},
                ],
            },
        ],
    }


def test_small_history_is_unchanged_including_cursor_and_extra_fields():
    frame = _history()
    frame["extra"] = {"version": 1}
    assert prepare_conversation_snapshot(frame, max_bytes=snapshot_byte_size(frame)) is frame


def test_large_history_elides_only_heavy_tools_and_preserves_lazy_reference():
    frame = _history("synthetic tool output " * 100_000)
    original = copy.deepcopy(frame)
    bounded = prepare_conversation_snapshot(frame, max_bytes=900 * 1024)

    assert snapshot_byte_size(bounded) <= 900 * 1024
    assert bounded["head_seq"] == 321
    assert bounded["detail"] == "shallow"
    assert [t["id"] for t in bounded["turns"]] == ["human", "assistant"]
    assert [t["content"] for t in bounded["turns"]] == [t["content"] for t in frame["turns"]]
    result = bounded["turns"][1]["parts"][1]
    assert result["tool_use_id"] == "native-call-1"
    assert result["truncated"] is True
    assert result["byte_size"] == len(original["turns"][1]["parts"][1]["content"].encode())
    assert "content" not in result
    assert frame == original, "Full REST/lazy-result data must remain intact"


@pytest.mark.parametrize("output", ["😀" * 400, "界" * 500, "é" * 700])
async def test_size_uses_actual_starlette_utf8_wire_bytes(output):
    frame = _history(output)
    # Unicode codepoints undercount all these messages; bytes require elision.
    character_count = len(json.dumps(frame, separators=(",", ":"), ensure_ascii=False))
    byte_count = snapshot_byte_size(frame)
    assert character_count < byte_count
    bounded = prepare_conversation_snapshot(frame, max_bytes=byte_count - 1)
    assert bounded["detail"] == "shallow"
    sent = []
    socket = WebSocket(
        {"type": "websocket"},
        AsyncMock(return_value={"type": "websocket.connect"}),
        AsyncMock(side_effect=sent.append),
    )
    await socket.accept()
    await socket.send_json(bounded)
    actual = sent[-1]["text"].encode("utf-8")
    assert len(actual) == snapshot_byte_size(bounded)
    assert len(actual) < byte_count


def test_oversized_prose_never_windows_or_returns_empty_authoritative_history():
    frame = _history()
    frame["turns"][0]["content"] = "Keep all this human text " * 1000
    with pytest.raises(ConversationSnapshotTooLargeError):
        prepare_conversation_snapshot(frame, max_bytes=1024)
    assert len(frame["turns"]) == 2


def test_invalid_budget_fails():
    with pytest.raises(ValueError, match="positive"):
        prepare_conversation_snapshot(_history(), max_bytes=0)


@pytest.mark.parametrize("oversized_prose", [False, True])
async def test_broker_sends_bounded_snapshot_or_visible_error_and_keeps_socket_usable(
    tmp_path, oversized_prose
):
    broker = _broker(tmp_path, conversation_snapshot_max_bytes=2048)
    broker._transport = AsyncMock()
    broker._transport.is_alive = True
    broker._transport.capabilities = TransportCapabilities()
    frame = _history("output " * 100_000)
    if oversized_prose:
        frame["turns"][0]["content"] = "human text " * 1000
    broker._conversation_turns = [ConversationTurn(**turn) for turn in frame["turns"]]
    original = copy.deepcopy(broker._conversation_turns)
    sent = []
    receive = AsyncMock(
        side_effect=[
            {"type": "websocket.connect"},
            {"type": "websocket.disconnect", "code": 1000},
        ]
    )
    socket = WebSocket(
        {"type": "websocket", "headers": [], "query_string": b""},
        receive,
        AsyncMock(side_effect=sent.append),
    )

    await broker.handle_websocket(socket)

    frames = [json.loads(item["text"]) for item in sent if item.get("type") == "websocket.send"]
    history = [item for item in frames if item.get("type") == "conversation_history"]
    if oversized_prose:
        assert history == []
        assert any(item.get("code") == "conversation_history_too_large" for item in frames)
    else:
        assert len(history) == 1
        assert len(history[0]["turns"]) == 2
        assert history[0]["detail"] == "shallow"
    relevant = [
        item
        for item in sent
        if item.get("type") == "websocket.send"
        and json.loads(item["text"]).get("type") in {"conversation_history", "error"}
    ]
    assert all(len(item["text"].encode("utf-8")) <= 2048 for item in relevant)
    assert receive.await_count == 2, "Replay sizing must not abort the live receive loop"
    assert broker._conversation_turns == original
