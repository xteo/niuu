"""LEX-1825: a hook that fires just after a turn ended must not duplicate the saved reply.

The evidence: a late MessageDisplay event 29 ms after turn 1's result refilled the pending
buffer the flush had just cleared, so the stale text was served as a live turn and saved
again at the front of turn 2. Both folds (live broker, log rebuild) drop such a frame, and
each saved reply carries the agent's message ids.
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace

from skuld.transports.tmux_interactive import TmuxInteractiveTransport
from tests.test_domain.test_transcript_reducer_parity import (
    SID,
    _broker,
    _drive,
    _live_turns,
    _rebuilt_turns,
)
from volundr.domain.models import SessionLogEntry
from volundr.domain.services.transcript_rebuild import rebuild_turns


def hook_text(text: str, message_id: str | None = "msg-1") -> dict:
    metadata: dict = {"source": "claude_hook", "hook_event_name": "MessageDisplay"}
    if message_id is not None:
        metadata["claude_message_id"] = message_id
    return {
        "type": "assistant",
        "message": {"model": "interactive", "content": [{"type": "text", "text": text}]},
        "metadata": metadata,
    }


def result(text: str) -> dict:
    return {"type": "result", "result": text, "stop_reason": "end_turn", "modelUsage": {}}


def user(text: str) -> dict:
    return {"type": "user", "message": {"role": "user", "content": text}}


def assistant_turns(turns: list[dict]) -> list[str]:
    return [turn["content"] for turn in turns if turn["role"] == "assistant"]


async def test_a_late_duplicate_is_dropped_live_never_logged_and_rebuilt_alike(tmp_path):
    broker = _broker(tmp_path)

    await _drive(
        broker,
        [
            user("are you there?"),
            hook_text("ready"),
            result("ready"),
            hook_text("ready"),  # the late hook, same message id
            user("now do the next thing"),
            hook_text("done", message_id="msg-2"),
            result("done"),
        ],
    )

    live = _live_turns(broker)
    assert assistant_turns(live) == ["ready", "done"]  # two replies, not three turns
    assert live[1]["metadata"]["message_ids"] == ["msg-1"]
    assert live[3]["metadata"]["message_ids"] == ["msg-2"]
    logged = [e for e in broker._event_log_buffer if e["kind"] == "assistant"]
    assert len(logged) == 2  # the late frame never reached the durable log (or clients)
    assert _rebuilt_turns(broker) == live  # INV-4: the rebuild agrees
    assert broker._serialize_in_progress_turn() is None


async def test_a_late_duplicate_without_an_id_is_matched_by_its_text(tmp_path):
    broker = _broker(tmp_path)

    await _drive(
        broker,
        [user("go"), hook_text("All set.\nready", None), result("All set.\nready")],
    )
    await _drive(broker, [hook_text("ready", None)])

    assert assistant_turns(_live_turns(broker)) == ["All set.\nready"]
    assert broker._serialize_in_progress_turn() is None


async def test_a_genuine_repeat_after_a_terminal_prompt_is_kept(tmp_path):
    broker = _broker(tmp_path)

    await _drive(
        broker,
        [
            user("say ready"),
            hook_text("ready", None),
            result("ready"),
            {"type": "terminal_prompt_submitted", "prompt": "say ready again"},
            hook_text("ready", None),
            result("ready"),
        ],
    )

    assert assistant_turns(_live_turns(broker)) == ["ready", "ready"]


def entry(seq: int, kind: str, payload: dict) -> SessionLogEntry:
    return SessionLogEntry(
        session_id=SID, seq=seq, kind=kind, payload=payload, ts=datetime.now(UTC)
    )


def test_a_rebuild_of_a_log_written_before_the_guard_drops_the_late_frame():
    rows = [
        entry(1, "user", user("are you there?")),
        entry(2, "assistant", hook_text("ready")),
        entry(3, "result", result("ready")),
        entry(4, "assistant", hook_text("ready")),  # logged by the old broker
        entry(5, "user", user("next")),
        entry(6, "assistant", hook_text("done", message_id="msg-2")),
        entry(7, "result", result("done")),
    ]

    turns = rebuild_turns(rows).turns

    assert [t["content"] for t in turns if t["role"] == "assistant"] == ["ready", "done"]


def transport_state(**overrides) -> SimpleNamespace:
    state = {
        "_displayed_message_ids": {"msg-1"},
        "_turn_displayed_texts": ["Working on it.", "ready"],
        "_last_result": {"result": "Working on it.\nready"},
        "_normalize_prompt": TmuxInteractiveTransport._normalize_prompt,
    }
    state.update(overrides)
    return SimpleNamespace(**state)


def test_the_transport_recognises_a_late_echo_of_the_finished_turn():
    late = TmuxInteractiveTransport._is_late_echo
    state = transport_state()
    assert late(state, "msg-1", "anything")  # an id the turn already displayed
    assert late(state, "msg-9", "ready")  # text the turn displayed
    assert late(transport_state(_turn_displayed_texts=[]), "current", "ready")  # in the result
    assert not late(state, "msg-9", "a genuinely new message")
