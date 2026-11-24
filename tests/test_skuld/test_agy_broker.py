"""The broker side of agy sessions: the hook route and the human-input-only guard."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from niuu.domain.message_sender import is_human
from skuld.broker import Broker
from skuld.chronicle import ChronicleMixin
from skuld.control_errors import HumanInputOnlyError
from tests.test_skuld.test_delivery_failed_steering_state import (
    _broker,
    _settle_delivery,
    delivery_claim_api,  # noqa: F401 - autouse fixture
)

USER = {"kind": "user"}
AGENT = {"kind": "agent", "sub_kind": "session", "id": "s2", "name": "coordinator"}


@pytest.mark.parametrize(
    ("sender", "human"),
    [
        (USER, True),
        ({"kind": "agent", "sub_kind": "voice_call"}, True),
        (AGENT, False),
        ({"kind": "agent", "sub_kind": "automation"}, False),
        ({"kind": "agent", "sub_kind": "unknown"}, False),
        (None, False),
    ],
)
def test_only_the_user_and_the_owner_by_voice_are_human(sender, human) -> None:
    assert is_human(sender) is human


@pytest.fixture
def broker(tmp_path) -> Broker:
    b = _broker(tmp_path)
    b._transport.send_message = AsyncMock(return_value=True)
    b._transport.human_input_only = True
    return b


async def _dispatch(broker: Broker, frame: dict) -> MagicMock:
    broker._channels.broadcast = AsyncMock()
    socket = MagicMock()
    broker._send_broker_frame_to = AsyncMock()
    broker._room_role_for = MagicMock(return_value="owner")
    await broker._dispatch_browser_message(frame, sender_ws=socket)
    await _settle_delivery()
    return socket


async def test_a_message_from_another_session_is_refused_and_recorded_nowhere(broker) -> None:
    await _dispatch(broker, {"content": "do the thing", "sender": AGENT})

    broker._transport.send_message.assert_not_awaited()
    assert broker._conversation_turns == []
    refusal = broker._send_broker_frame_to.await_args.args[1]
    assert refusal["code"] == "human_input_only"
    assert "typed by a person only" in refusal["content"]


async def test_an_unidentified_sender_is_refused(broker) -> None:
    await _dispatch(broker, {"content": "hello", "request_id": "agent-service-x"})

    broker._transport.send_message.assert_not_awaited()


async def test_the_users_own_message_is_delivered(broker) -> None:
    await _dispatch(broker, {"content": "hello", "sender": "user"})

    assert broker._transport.send_message.await_args.args[0] == "hello"


async def test_a_session_without_the_guard_still_takes_agent_messages(tmp_path) -> None:
    open_broker = _broker(tmp_path)
    open_broker._transport.send_message = AsyncMock(return_value=True)

    await _dispatch(open_broker, {"content": "do the thing", "sender": AGENT})

    open_broker._transport.send_message.assert_awaited()


async def test_a_bare_steer_is_refused(broker) -> None:
    broker._transport.capabilities = SimpleNamespace(steer=True)
    broker._room_role_for = MagicMock(return_value="owner")

    with pytest.raises(HumanInputOnlyError):
        await broker._dispatch_browser_message(
            {"type": "steer_active_turn", "content": "faster"}, sender_ws=None
        )
    broker._transport.send_control.assert_not_awaited()


async def test_a_room_relay_is_refused(broker) -> None:
    with pytest.raises(HumanInputOnlyError):
        await broker._send_explicit_human_message_to_transport("from the room")
    broker._transport.send_message.assert_not_awaited()


async def test_the_chronicle_sends_no_summary_prompt(broker) -> None:
    broker._transport.is_alive = True
    broker._artifacts = SimpleNamespace(files_changed=["a.py"])

    summary = await ChronicleMixin._generate_summary(broker)

    assert summary == {"summary": None, "key_changes": ["a.py"], "unfinished_work": None}
    broker._transport.send_message.assert_not_awaited()


async def test_the_hook_route_returns_the_transports_answer(module_broker_runtime) -> None:
    from skuld import broker as bmod

    saved = bmod.broker._transport
    handler = AsyncMock(return_value={"decision": "allow"})
    bmod.broker._transport = SimpleNamespace(handle_agy_hook=handler)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=bmod.app), base_url="http://broker"
        ) as client:
            auth = {"authorization": module_broker_runtime.authorization}
            denied = await client.post("/api/agy/hooks", json={"event": "Stop", "payload": {}})
            allowed = await client.post(
                "/api/agy/hooks",
                json={"event": "PreToolUse", "payload": {"conversationId": "c"}},
                headers=auth,
            )
            malformed = await client.post("/api/agy/hooks", json={"event": 1}, headers=auth)
            handler.side_effect = ValueError("unknown agy hook event 'X'")
            refused = await client.post(
                "/api/agy/hooks", json={"event": "X", "payload": {}}, headers=auth
            )
    finally:
        bmod.broker._transport = saved

    assert denied.status_code == 401
    assert allowed.status_code == 200 and allowed.json() == {"decision": "allow"}
    handler.assert_any_await("PreToolUse", {"conversationId": "c"})
    assert malformed.status_code == 422
    assert refused.status_code == 422 and "unknown agy hook event" in refused.text


async def test_an_agy_hook_to_another_transport_fails_loudly(broker) -> None:
    broker._transport = SimpleNamespace()

    with pytest.raises(RuntimeError, match="transport is not agy"):
        await broker.handle_agy_hook("Stop", {})


def test_gemini_sessions_run_agy_and_offer_its_models() -> None:
    from bifrost.config import _default_models
    from niuu.config_models import default_session_definitions
    from niuu.domain.reasoning import AGY_MODELS, MODEL_EFFORTS, preferred_effort
    from skuld.config import SkuldSettings
    from skuld.forge_mcp.host import engine_for_adapter

    definition = default_session_definitions()["skuldGemini"]
    adapter = "skuld.transports.tmux_agy.TmuxAgyTransport"
    assert definition.defaults["broker"]["transportAdapter"] == adapter
    assert definition.default_model == "gemini-4-neon"
    assert SkuldSettings(cli_type="agy").transport_adapter == adapter
    assert engine_for_adapter(adapter) == "agy"
    catalog = {m.id: m for m in _default_models() if m.session_definition == "skuldGemini"}
    assert set(catalog) == set(AGY_MODELS)
    assert MODEL_EFFORTS["gemini-4-neon"] == ("low", "medium", "high", "xhigh")
    assert preferred_effort(MODEL_EFFORTS["gemini-4-neon"], "gemini-4-neon") == "high"
