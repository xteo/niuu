"""The durable log keeps only the pane repaints a rebuild can use (LEX-1984).

A tmux session repaints its pane many times a second, and every repaint was a ~3.4 KB
row in session_event_log (79% of the mini's 21 GB). A rebuild reads only the last
repaint of a run of terminal frames: its rows (the last-resort scrape) and its seq and
time. So the broker holds each pane's newest repaint and logs it just before the next
other frame. Live viewers still get every repaint, and a rebuild gives the same turns.
"""

from __future__ import annotations

import random
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

from niuu.domain.transcript_reducer import (
    TERMINAL_FRAME_KIND,
    TURN_INTERRUPTED_KIND,
    reduce_frames,
)
from skuld.broker import Broker
from skuld.config import SkuldSettings
from skuld.conversation_models import turn_record
from skuld.transports import TransportCapabilities

_SID = "pane-frame-log-session"


class _Frame:
    def __init__(self, entry: dict) -> None:
        self.session_id = _SID
        self.seq = int(entry["seq"])
        self.kind = entry["kind"]
        self.payload = entry["payload"]
        self.request_id = entry.get("request_id")
        self.ts = datetime.fromisoformat(entry["ts"])


def _broker(tmp_path, mode: str = "latest") -> Broker:
    broker = Broker(
        SkuldSettings(
            session={"id": _SID, "workspace_dir": str(tmp_path)},
            transport="subprocess",
            volundr_api_url="http://forge.test",
            event_log_enabled=True,
            event_log_terminal_frames=mode,
        )
    )
    broker._transport = MagicMock()
    broker._transport.is_alive = True
    broker._transport.capabilities = TransportCapabilities()
    broker._report_activity_state = AsyncMock()
    broker._report_timeline_event = AsyncMock()
    broker._start_trace_span = AsyncMock(return_value=None)
    broker._finish_trace_span = AsyncMock()
    broker._save_conversation_history = MagicMock()
    broker._channels.broadcast = AsyncMock()
    return broker


def _text(text: str) -> dict:
    return {
        "type": "assistant",
        "message": {"role": "assistant", "content": [{"type": "text", "text": text}]},
    }


def _tool(tool_id: str) -> dict:
    return {
        "type": "assistant",
        "message": {
            "role": "assistant",
            "content": [{"type": "tool_use", "id": tool_id, "name": "Bash", "input": {}}],
        },
    }


def _pane(pane: str, screen: str) -> dict:
    rows = [f"{pane}: {screen}"]
    return {"type": TERMINAL_FRAME_KIND, "pane_id": pane, "rows": rows, "text": rows[0]}


_RESULT = {"type": "result", "result": "", "stop_reason": "end_turn"}


def _user(text: str) -> dict:
    return {"type": "user", "uuid": f"u-{text}", "message": {"role": "user", "content": text}}


def _logged(broker: Broker) -> list[_Frame]:
    return [_Frame(e) for e in broker._event_log_buffer]


def _shape(turns: list[dict], *, ids: bool = True) -> list[tuple]:
    return [
        (
            t["id"] if ids else None,
            t["role"],
            t["content"],
            [p.get("type") for p in t["parts"]],
            t["metadata"].get("status"),
        )
        for t in turns
    ]


def _scrape(rows: list[str]) -> str:
    return "\n".join(rows)


async def _play(broker: Broker, events: list) -> None:
    for event in events:
        if event == "interrupt":
            await broker._interrupt_open_turn("broker_shutdown")
        else:
            await broker._handle_cli_event(dict(event))


async def test_a_run_of_repaints_logs_only_its_last_one(tmp_path):
    broker = _broker(tmp_path)
    await _play(
        broker,
        [_text("Working"), _pane("%0", "1"), _pane("%0", "2"), _pane("%0", "3"), _tool("t1")],
    )

    kinds = [e["kind"] for e in broker._event_log_buffer]
    assert kinds == ["assistant", TERMINAL_FRAME_KIND, "assistant"]
    assert broker._event_log_buffer[1]["payload"]["rows"] == ["%0: 3"]
    # Live viewers saw every repaint.
    sent = [c.args[0] for c in broker._channels.broadcast.await_args_list]
    assert [f["rows"] for f in sent if f.get("type") == TERMINAL_FRAME_KIND] == [
        ["%0: 1"],
        ["%0: 2"],
        ["%0: 3"],
    ]


async def test_each_pane_keeps_its_last_screen_and_the_newest_stays_last(tmp_path):
    broker = _broker(tmp_path)
    await _play(
        broker,
        [
            _pane("%0", "a1"),
            _pane("%1", "b1"),
            _pane("%0", "a2"),
            _pane("%1", "b2"),
            _pane("%0", "a3"),
            _RESULT,
        ],
    )

    panes = [
        e["payload"]["rows"] for e in broker._event_log_buffer if e["kind"] == TERMINAL_FRAME_KIND
    ]
    assert panes == [["%1: b2"], ["%0: a3"]]


async def test_shutdown_logs_the_held_repaints(tmp_path):
    broker = _broker(tmp_path)
    await _play(broker, [_text("Idle at the prompt"), _RESULT, _pane("%0", "prompt")])
    assert [e["kind"] for e in broker._event_log_buffer][-1] != TERMINAL_FRAME_KIND
    broker._event_log_head_known = False  # stop before the network drain

    await broker._stop_event_log()

    assert broker._event_log_buffer[-1]["payload"]["rows"] == ["%0: prompt"]


async def test_all_keeps_every_repaint(tmp_path):
    broker = _broker(tmp_path, mode="all")
    await _play(broker, [_text("Working"), _pane("%0", "1"), _pane("%0", "2"), _RESULT])

    assert [e["kind"] for e in broker._event_log_buffer].count(TERMINAL_FRAME_KIND) == 2


async def test_a_held_repaint_keeps_the_time_it_was_seen(tmp_path):
    broker = _broker(tmp_path)
    await _play(broker, [_text("Working"), _pane("%0", "1")])
    seen = broker._held_pane_frames["%0"][1]

    await _play(broker, [_RESULT])

    frame = next(e for e in broker._event_log_buffer if e["kind"] == TERMINAL_FRAME_KIND)
    assert frame["ts"] == seen.isoformat()


def _random_session(rng: random.Random) -> list:
    """A session's events: streamed text, tool calls, repaints of two panes, turn ends,
    and (LEX-1982) a broker stop mid-turn."""
    events: list = []
    tools = 0
    for _ in range(rng.randint(5, 40)):
        pick = rng.random()
        if pick < 0.45:
            events.append(_pane(rng.choice(["%0", "%1"]), str(rng.randint(0, 9))))
        elif pick < 0.65:
            events.append(_text(f"text {len(events)}"))
        elif pick < 0.8:
            tools += 1
            events.append(_tool(f"t{tools}"))
        elif pick < 0.88:
            events.append(_RESULT)
        elif pick < 0.94:
            events.append(_user(f"message {len(events)}"))
        else:
            events.append("interrupt")
    # End on a turn end: an open span at the end of a log is an interrupted turn to a
    # rebuild but still in progress to the live broker.
    events.append(_RESULT)
    return events


async def test_rebuild_equals_live_and_matches_logging_every_repaint(tmp_path):
    """The hard requirement: for any mix of repaints, content, turn ends and LEX-1982
    interrupts, the rebuild of the held log equals the live turns, and gives the same
    turns (scrape included) as a log of every repaint."""
    rng = random.Random(1984)
    for case in range(300):
        events = _random_session(rng)
        latest = _broker(tmp_path / f"latest-{case}")
        every = _broker(tmp_path / f"all-{case}", mode="all")
        await _play(latest, events)
        await _play(every, events)
        latest._log_held_pane_frames()

        live = [turn_record(t) for t in latest._conversation_turns]
        rebuilt = reduce_frames(_logged(latest)).turns
        assert _shape(rebuilt) == _shape(live), (case, events)
        # And with every repaint logged: a trailing repaint never moves a turn's id.
        every_live = [turn_record(t) for t in every._conversation_turns]
        assert _shape(reduce_frames(_logged(every)).turns) == _shape(every_live), (case, events)

        with_scrape = reduce_frames(_logged(latest), scrape=_scrape).turns
        all_scrape = reduce_frames(_logged(every), scrape=_scrape).turns
        assert _shape(with_scrape, ids=False) == _shape(all_scrape, ids=False), (case, events)
        assert [t["content"] for t in with_scrape] == [t["content"] for t in all_scrape]

        held = [e for e in latest._event_log_buffer if e["kind"] == TERMINAL_FRAME_KIND]
        logged = [e for e in every._event_log_buffer if e["kind"] == TERMINAL_FRAME_KIND]
        assert len(held) <= len(logged)


async def test_an_interrupted_span_after_repaints_rebuilds_as_saved(tmp_path):
    broker = _broker(tmp_path)
    await _play(
        broker,
        [
            _text("Watching"),
            _pane("%0", "1"),
            _pane("%0", "2"),
            "interrupt",
            _text("Back"),
            _pane("%0", "3"),
            _RESULT,
        ],
    )

    kinds = [e["kind"] for e in broker._event_log_buffer]
    assert kinds.index(TERMINAL_FRAME_KIND) < kinds.index(TURN_INTERRUPTED_KIND)
    live = [turn_record(t) for t in broker._conversation_turns]
    assert [t["content"] for t in live] == ["Watching", "Back"]
    assert _shape(reduce_frames(_logged(broker)).turns) == _shape(live)
