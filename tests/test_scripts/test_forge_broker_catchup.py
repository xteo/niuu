"""LEX-1957: a promotion brings every running session's broker up to the broker contract."""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
_spec = importlib.util.spec_from_file_location(
    "forge_broker_catchup", ROOT / "scripts/forge_broker_catchup.py"
)
catchup = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(catchup)

BASE = "http://forge"
API = f"{BASE}/api/v1/forge"
UNTIL = "2099-01-01T00:00:00Z"


class FakeForge:
    """Running sessions whose brokers report a contract state; stop/start relaunches current."""

    def __init__(self, *sessions: tuple[str, str, str]):
        self.rows = {
            sid: {
                "id": sid,
                "name": name,
                "status": "running",
                "activity_state": "idle",
                "cli_session_id": f"conv-{sid}",
            }
            for sid, name, _ in sessions
        }
        self.contract = {sid: state for sid, _, state in sessions}
        self.calls: list[str] = []
        self.start_new_conversation: set[str] = set()
        self.start_fails: set[str] = set()
        self.no_transcript: set[str] = set()
        self.judged: list[list[str]] = []

    def __call__(self, method: str, url: str):
        path = url.removeprefix(API)
        self.calls.append(f"{method} {path}")
        if path == "/sessions?scope=local":
            return 200, list(self.rows.values())
        sid, _, action = path.removeprefix("/sessions/").partition("/")
        row = self.rows[sid]
        if action == "":
            return 200, row
        if action == "runtime-version":
            if row["status"] != "running":
                return 200, {"contract": {"state": "not_running"}}
            return 200, {
                "current": {"build": "old" if self.contract[sid] != "met" else "new"},
                "contract": {"required": 1, "state": self.contract[sid]},
            }
        if action == "stop":
            row["status"] = "stopped"
            return 200, row
        if action == "start":
            if sid in self.start_fails:
                return 503, "no capacity"
            row["status"] = "running"
            self.contract[sid] = "met"
            if sid in self.start_new_conversation:
                row["cli_session_id"] = "fresh"
            return 200, row
        raise AssertionError(url)


@pytest.fixture
def forge(monkeypatch):
    def install(*sessions, busy=None):
        fake = FakeForge(*sessions)
        monkeypatch.setattr(catchup, "_call", fake)
        monkeypatch.setattr(catchup.time, "sleep", lambda _s: None)
        monkeypatch.setattr(catchup, "transcript_exists", lambda cid: cid not in fake.no_transcript)
        monkeypatch.setattr(
            catchup,
            "live_resume",
            lambda tmux, sock, sid: (
                (f"pid-{sid}", fake.rows[sid]["cli_session_id"])
                if fake.rows[sid]["status"] == "running" and sid not in fake.start_new_conversation
                else (f"pid-{sid}", "fresh")
                if fake.rows[sid]["status"] == "running"
                else None
            ),
        )
        fake.busy = busy if busy is not None else {}

        def judge(base, tmux, only, background=False, socket_dir=None):
            assert base == BASE and background
            fake.judged.append(list(only))
            running = [sid for sid in only if fake.rows[sid]["status"] == "running"]
            return {
                "lanes": len(running),
                "judged": running,
                "busy": [
                    {"id": sid, "reasons": fake.busy[sid]} for sid in running if sid in fake.busy
                ],
                "host_trains": [],
            }

        monkeypatch.setattr(catchup.forge_lane_busy, "judge", judge)
        return fake

    return install


def _run(capsys, *extra: str) -> tuple[int, dict]:
    code = catchup.main(["restart", BASE, "tmux", "--until", UNTIL, *extra])
    out = capsys.readouterr().out
    return code, json.loads(out[out.index("\n{") + 1 :] if "\n{" in out else out)


def _soon() -> str:
    return (
        catchup.datetime.now(catchup.UTC) + __import__("datetime").timedelta(seconds=1)
    ).isoformat()


def test_audit_lists_each_running_broker_and_fails_while_one_is_below(forge, capsys):
    forge(("a", "lexi", "below"), ("b", "nepal", "met"))
    assert catchup.main(["audit", BASE]) == 1
    rows = json.loads(capsys.readouterr().out)
    assert [(r["name"], r["contract"], r["build"]) for r in rows] == [
        ("lexi", "below", "old"),
        ("nepal", "met", "new"),
    ]


def test_sessions_below_the_contract_restart_and_resume_their_conversation(forge, capsys):
    fake = forge(("a", "lexi", "below"), ("b", "nepal", "met"))
    code, outcome = _run(capsys)
    assert code == 0
    assert outcome == {"a": {**outcome["a"], "result": "restarted"}}
    assert "POST /sessions/a/stop" in fake.calls and "POST /sessions/a/start" in fake.calls
    assert not any(c.startswith("POST /sessions/b") for c in fake.calls)


def test_a_held_session_is_listed_and_never_restarted(forge, capsys):
    fake = forge(("a", "ios-coordinator", "below"), ("b", "lexi", "below"))
    code, outcome = _run(capsys, "--hold", "ios-coordinator")
    assert code == 0
    assert outcome["a"]["result"] == "held: restart only on its owner's GO"
    assert outcome["b"]["result"] == "restarted"
    assert not any(c.startswith("POST /sessions/a") for c in fake.calls)


def test_only_restarts_a_held_session_its_owner_gave_a_go_for(forge, capsys):
    fake = forge(("a", "ios-coordinator", "below"), ("b", "lexi", "below"))
    code, outcome = _run(capsys, "--hold", "b", "--only", "a")
    assert code == 0
    assert list(outcome) == ["a"] and outcome["a"]["result"] == "restarted"
    assert not any(c.startswith("POST /sessions/b") for c in fake.calls)


def test_a_busy_session_is_reported_at_the_deadline_not_restarted(forge, capsys):
    fake = forge(("a", "lexi", "below"), busy={"a": ["train: pid 9, 01:00: xcodebuild"]})
    code = catchup.main(["restart", BASE, "tmux", "--until", _soon(), "--retry", "0"])
    out = capsys.readouterr().out
    assert code == 1
    assert json.loads(out)["a"]["result"] == (
        "busy until the deadline: train: pid 9, 01:00: xcodebuild"
    )
    assert not any(c.startswith("POST") for c in fake.calls)


def test_a_restart_that_loses_the_conversation_does_not_count(forge, capsys):
    fake = forge(("a", "lexi", "below"))
    fake.start_new_conversation.add("a")
    code, outcome = _run(capsys)
    assert code == 1
    assert outcome["a"]["result"] == (
        "started, but its Claude is not resuming conv-a (live: ('pid-a', 'fresh')): context lost?"
    )


def test_a_session_with_no_conversation_id_is_never_stopped(forge, capsys):
    fake = forge(("a", "lexi", "below"))
    fake.rows["a"]["cli_session_id"] = None
    code, outcome = _run(capsys)
    assert code == 1
    assert outcome["a"]["result"] == "not restarted: no recorded conversation id to resume"
    assert not any(c.startswith("POST") for c in fake.calls)


def test_a_failed_start_says_the_session_is_stopped(forge, capsys):
    fake = forge(("a", "lexi", "below"))
    fake.start_fails.add("a")
    code, outcome = _run(capsys)
    assert code == 1
    assert outcome["a"]["result"] == (
        "STOPPED, start failed: HTTP 503 no capacity; start it by hand"
    )
    assert fake.calls.count("POST /sessions/a/start") == 1


def test_an_unprobed_broker_is_reported_not_restarted(forge, capsys):
    fake = forge(("a", "lexi", "unknown"))
    code, outcome = _run(capsys)
    assert code == 1
    assert outcome["a"]["result"] == "broker not probed (unknown)"
    assert not any(c.startswith("POST") for c in fake.calls)


def test_evidence_records_each_outcome(forge, capsys, tmp_path):
    forge(("a", "lexi", "below"))
    evidence = tmp_path / "catchup.json"
    _run(capsys, "--evidence", str(evidence))
    assert json.loads(evidence.read_text())["a"]["result"] == "restarted"


def test_the_busy_gate_runs_again_right_before_each_stop(forge, capsys):
    fake = forge(("a", "lexi-todo", "below"), ("b", "trickle", "below"))
    _run(capsys)
    assert fake.judged == [["a"], ["b"]]


def test_a_session_that_stopped_meanwhile_is_not_started_again(forge, capsys, monkeypatch):
    fake = forge(("a", "lexi-todo", "below"))
    real_audit = catchup.audit

    audits = []

    def audit_then_owner_stops(base):
        rows = real_audit(base)
        audits.append(base)
        if len(audits) == 2:  # the owner stops it between the loop's audit and the gate
            fake.rows["a"]["status"] = "stopped"
        return rows

    monkeypatch.setattr(catchup, "audit", audit_then_owner_stops)
    code = catchup.main(["restart", BASE, "tmux", "--until", UNTIL])
    capsys.readouterr()
    assert code == 1
    assert "POST /sessions/a/start" not in fake.calls


def test_the_canary_fails_unless_it_really_restarted(forge, capsys):
    """Review: --only exited 0 when its session was busy until the deadline or already met."""
    forge(("a", "trickle", "met"))
    code, outcome = _run(capsys, "--only", "a")
    assert code == 1
    assert outcome["a"]["result"] == "not restarted: already meets the contract"


def test_a_busy_canary_fails_at_the_deadline(forge, capsys):
    forge(("a", "trickle", "below"), busy={"a": ["background agent x1 running"]})
    code = catchup.main(["restart", BASE, "tmux", "--until", "2000-01-01T00:00:00Z", "--only", "a"])
    out = capsys.readouterr().out
    assert code == 1
    assert json.loads(out)["a"]["result"] == "deadline passed before it was tried"


def test_only_refuses_a_session_that_is_not_running_here(forge):
    forge(("a", "trickle", "below"))
    with pytest.raises(SystemExit, match="not running here"):
        catchup.main(["restart", BASE, "tmux", "--until", UNTIL, "--only", "zz"])


def test_hold_must_name_exactly_one_running_session(forge):
    forge(("a", "lexi", "below"), ("b", "lexi", "below"))
    with pytest.raises(SystemExit, match="names 2 running sessions"):
        catchup.main(["restart", BASE, "tmux", "--until", UNTIL, "--hold", "lexi"])


def test_a_deadline_without_a_timezone_is_refused(forge):
    forge(("a", "lexi", "below"))
    with pytest.raises(SystemExit, match="timezone"):
        catchup.main(["restart", BASE, "tmux", "--until", "2026-11-06T23:00:00"])


def test_no_transcript_means_no_stop(forge, capsys):
    fake = forge(("a", "lexi", "below"))
    fake.no_transcript.add("conv-a")
    code, outcome = _run(capsys)
    assert code == 1
    assert outcome["a"]["result"].startswith("not restarted: no transcript")
    assert not any(c.startswith("POST") for c in fake.calls)


def test_an_error_after_the_stop_is_reported_as_possibly_stopped(forge, capsys):
    fake = forge(("a", "lexi", "below"), ("b", "trickle", "below"))
    real = fake.__call__

    def flaky(method, url):
        if url.endswith("/sessions/a") and fake.rows["a"]["status"] == "stopped":
            return 502, "bad gateway"
        return real(method, url)

    catchup._call = flaky
    try:
        code, outcome = _run(capsys)
    finally:
        catchup._call = fake
    assert code == 1
    assert outcome["a"]["result"].startswith("STOPPED? RuntimeError after the stop")
    assert outcome["b"]["result"] == "restarted"
