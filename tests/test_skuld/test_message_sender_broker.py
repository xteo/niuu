"""LEX-1910 at the broker: the turn records its sender, the model sees the header.

Each sender kind is driven through the real ``_dispatch_browser_message`` with the
durable buffer live, then read back three ways: the stored turn, the broadcast
``user_confirmed`` frame, and the shared reducer's rebuild from the log.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from niuu.domain.transcript_reducer import reduce_frames
from skuld.broker import Broker
from skuld.config import SkuldSettings
from skuld.conversation_read import conversation_rows
from tests.test_skuld.test_delivery_failed_steering_state import (
    _broker,
    _buffer_payloads,
    _frames_from_buffer,
    _settle_delivery,
    delivery_claim_api,  # noqa: F401 - autouse fixture
)

UNKNOWN = {"kind": "agent", "sub_kind": "unknown"}
UNKNOWN_HEADER = (
    "[Message from an unidentified sender through the Forge API, not typed by the user]"
)
SESSION = {
    "kind": "agent",
    "sub_kind": "session",
    "id": "88bb2747-c103-525e-a382-d2a8218b5cb6",
    "name": "ios-integration",
    "host": "mini-m6",
    "via": "guild",
}
SESSION_HEADER = (
    "[Message from session ios-integration (88bb2747-c103-525e-a382-d2a8218b5cb6) on "
    "mini-m6, relayed through the Guild]"
)


async def _dispatch(broker: Broker, frame: dict, *, from_socket: bool = True) -> None:
    broker._channels.broadcast = AsyncMock()
    socket = MagicMock() if from_socket else None
    broker._send_broker_frame_to = AsyncMock()
    await broker._dispatch_browser_message(frame, sender_ws=socket)
    await _settle_delivery()


def _delivered(broker: Broker) -> str:
    return broker._transport.send_message.await_args.args[0]


def _confirmed(broker: Broker) -> dict:
    return next(p for p in _buffer_payloads(broker) if p.get("type") == "user_confirmed")


def _rebuilt_user_turn(broker: Broker) -> dict:
    turns = reduce_frames(_frames_from_buffer(broker)).turns
    return next(t for t in turns if t["role"] == "user")


@pytest.fixture
def broker(tmp_path) -> Broker:
    b = _broker(tmp_path)
    b._transport.send_message = AsyncMock(return_value=True)
    return b


async def test_a_session_frame_from_forge_is_recorded_as_sent_and_headed_for_the_model(broker):
    await _dispatch(
        broker,
        {
            "type": "user",
            "content": f"{SESSION_HEADER}\n\nLANDED LEX-1803",
            "display_content": "LANDED LEX-1803",
            "request_id": "fmcp-1",
            "sender": SESSION,
        },
    )

    turn = broker._conversation_turns[-1]
    assert turn.content == "LANDED LEX-1803"
    assert turn.sender == SESSION
    assert _delivered(broker) == f"{SESSION_HEADER}\n\nLANDED LEX-1803"
    assert _confirmed(broker)["sender"] == SESSION
    assert _confirmed(broker)["content"] == "LANDED LEX-1803"
    rebuilt = _rebuilt_user_turn(broker)
    assert rebuilt["sender"] == SESSION
    assert rebuilt["content"] == "LANDED LEX-1803"
    assert conversation_rows(broker)[-1]["sender"] == SESSION


async def test_the_owners_ios_request_id_is_the_user_and_reaches_the_model_unchanged(broker):
    await _dispatch(
        broker,
        {"content": "where is the deck?", "request_id": "2E95A1C7-A6F4-4BF7-8F33-CAF794B55B86"},
    )

    assert broker._conversation_turns[-1].sender == {"kind": "user"}
    assert _delivered(broker) == "where is the deck?"
    assert _rebuilt_user_turn(broker)["sender"] == {"kind": "user"}


async def test_a_client_that_names_a_voice_call_is_headed(broker):
    await _dispatch(
        broker,
        {
            "content": "Damien's instruction: add milk",
            "request_id": "voice-call-ios-7",
            "sender": {"kind": "agent", "sub_kind": "voice_call", "id": "call-7"},
        },
    )

    turn = broker._conversation_turns[-1]
    assert turn.sender == {"kind": "agent", "sub_kind": "voice_call", "id": "call-7"}
    assert turn.content == "Damien's instruction: add milk"
    assert _delivered(broker) == (
        "[Message from voice call call-7, not typed by the user]\n\nDamien's instruction: add milk"
    )


async def test_an_unnamed_socket_message_is_an_unknown_sender_never_the_user(broker):
    await _dispatch(broker, {"content": "READY LEX-1904", "request_id": "agent-service-x"})

    turn = broker._conversation_turns[-1]
    assert turn.sender == UNKNOWN
    assert turn.content == "READY LEX-1904"
    assert _delivered(broker) == f"{UNKNOWN_HEADER}\n\nREADY LEX-1904"
    assert _rebuilt_user_turn(broker)["sender"] == UNKNOWN


async def test_the_owners_telegram_channel_is_the_user(broker):
    await _dispatch(broker, {"content": "status?"}, from_socket=False)

    assert broker._conversation_turns[-1].sender == {"kind": "user"}
    assert _delivered(broker) == "status?"


async def test_an_automation_slash_command_still_runs_as_a_command(broker):
    await _dispatch(broker, {"content": "/compact", "sender": "automation"})

    assert broker._conversation_turns[-1].sender == {"kind": "agent", "sub_kind": "automation"}
    assert _delivered(broker) == "/compact"


async def test_a_malformed_sender_is_refused_loudly_and_records_nothing(broker):
    await _dispatch(broker, {"content": "hi", "sender": {"kind": "operator"}})

    assert broker._conversation_turns == []
    broker._transport.send_message.assert_not_awaited()
    refusal = broker._send_broker_frame_to.await_args.args[1]
    assert refusal["code"] == "invalid_sender"


async def test_an_unlabelled_turn_from_before_senders_has_no_sender_key(broker):
    from skuld.conversation_models import ConversationTurn

    broker._conversation_turns.append(ConversationTurn(id="old", role="user", content="hi"))
    broker._conversation_turns.append(ConversationTurn(id="a", role="assistant", content="yo"))

    rows = conversation_rows(broker)

    assert all("sender" not in row for row in rows)


def _prompt_broker(tmp_path, sender: dict | None) -> Broker:
    settings = SkuldSettings(
        session={
            "id": "s1",
            "workspace_dir": str(tmp_path),
            "initial_prompt": "Build LEX-1910",
            **({"initial_prompt_sender": sender} if sender else {}),
        },
        transport="sdk",
    )
    b = Broker(settings=settings)
    b._transport = AsyncMock()
    b._save_conversation_history = MagicMock()
    b._channels.broadcast = AsyncMock()
    return b


async def test_a_session_created_by_another_session_records_and_heads_its_first_prompt(tmp_path):
    sender = {"kind": "agent", "sub_kind": "session", "id": "s-0", "name": "ios-coordinator"}
    b = _prompt_broker(tmp_path, sender)

    await b._auto_start_transport()

    assert b._conversation_turns[0].sender == sender
    assert b._conversation_turns[0].content == "Build LEX-1910"
    assert b._build_transport_kwargs()["initial_prompt"] == (
        "[Message from session ios-coordinator (s-0)]\n\nBuild LEX-1910"
    )


async def test_a_first_prompt_with_no_named_sender_stays_unlabelled_and_unheaded(tmp_path):
    b = _prompt_broker(tmp_path, None)

    await b._auto_start_transport()

    assert b._conversation_turns[0].sender is None
    assert b._build_transport_kwargs()["initial_prompt"] == "Build LEX-1910"


def test_a_malformed_first_prompt_sender_fails_at_startup(tmp_path):
    with pytest.raises(ValueError, match="sender kind"):
        SkuldSettings(
            session={
                "id": "s1",
                "workspace_dir": str(tmp_path),
                "initial_prompt_sender": {"kind": "operator"},
            }
        )
