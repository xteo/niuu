"""LEX-1858: stopping a session ends what its broker leaves behind, each survivor logged."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import AsyncMock
from uuid import uuid4

from volundr.adapters.outbound import local_process
from volundr.adapters.outbound.local_process import (
    LocalProcessPodManager,
    ProcessInfo,
    ProcessState,
    session_runtime_pids,
)
from volundr.domain.models import GitSource, Session

SID = "39bd5e2b-1d56-48ae-816b-f78a3aea9c51"


def fake_proc(root: Path, processes: dict[int, list[str]]) -> Path:
    for pid, argv in processes.items():
        entry = root / str(pid)
        entry.mkdir(parents=True)
        (entry / "cmdline").write_bytes(b"\0".join(arg.encode() for arg in argv) + b"\0")
    return root


def test_runtime_processes_are_found_by_their_session_scoped_names(tmp_path) -> None:
    uid = os.getuid()
    proc = fake_proc(
        tmp_path,
        {
            101: ["tmux", "-S", f"/tmp/skuld-tmux-{uid}/skuld-{SID}.sock", "new-session"],
            102: ["claude", "--settings", f"/tmp/skuld-{uid}/{SID}/settings.json"],
            103: ["sh", "-c", f"cat >> /w/.skuld/tmux-interactive/skuld-{SID}/panes/0.log"],
            104: ["curl", f"http://127.0.0.1:8080/api/v1/forge/sessions/{SID}"],  # bare id
            105: ["claude", "--settings", f"/tmp/skuld-{uid}/{uuid4()}/settings.json"],
            os.getpid(): ["python", f"skuld-{SID}"],  # never this process
        },
    )

    found = session_runtime_pids(SID, proc=proc)

    assert sorted(found) == [101, 102, 103]


def test_a_long_command_is_cut_and_says_so(tmp_path) -> None:
    proc = fake_proc(tmp_path, {201: ["claude", f"skuld-{SID}", "x" * 500]})

    (command,) = session_runtime_pids(SID, proc=proc).values()

    full = f"claude skuld-{SID} " + "x" * 500
    assert command == f"{full[:200]}… ({len(full)} chars)"


def manager(tmp_path: Path) -> LocalProcessPodManager:
    return LocalProcessPodManager(
        workspaces_dir=str(tmp_path / "w"),
        state_file=str(tmp_path / "state.json"),
        tmux_socket_dir=str(tmp_path / "tmux"),
    )


def session() -> Session:
    return Session(
        id=uuid4(), name="s", source=GitSource(repo="https://example.com/r.git", branch="main")
    )


async def test_stopping_ends_the_runtime_and_logs_each_survivor(tmp_path, monkeypatch, caplog):
    caplog.set_level("WARNING", logger="volundr.adapters.outbound.local_process")
    pods = manager(tmp_path)
    s = session()
    pods._processes[str(s.id)] = ProcessInfo(
        session_id=str(s.id), pid=4242, state=ProcessState.RUNNING
    )
    terminated: list[int] = []
    pods._terminate_process = AsyncMock(side_effect=lambda pid: terminated.append(pid))
    monkeypatch.setattr(
        local_process, "session_runtime_pids", lambda sid: {777: f"claude skuld-{sid}"}
    )

    assert await pods.stop(s) is True

    assert terminated == [4242, 777]  # the broker, then what it left running
    assert f"Session {s.id}: stopping a process its broker left running: pid=777" in caplog.text


async def test_a_stopped_session_still_has_its_leftover_runtime_ended(tmp_path, monkeypatch):
    pods = manager(tmp_path)
    s = session()
    pods._processes[str(s.id)] = ProcessInfo(session_id=str(s.id), state=ProcessState.STOPPED)
    terminated: list[int] = []
    pods._terminate_process = AsyncMock(side_effect=lambda pid: terminated.append(pid))
    monkeypatch.setattr(local_process, "session_runtime_pids", lambda sid: {888: "tmux"})

    assert await pods.stop(s) is True

    assert terminated == [888]


async def test_slots_are_listed_for_live_running_sessions(tmp_path, monkeypatch):
    pods = manager(tmp_path)
    pods._processes["a"] = ProcessInfo(session_id="a", pid=os.getpid(), state=ProcessState.RUNNING)
    pods._processes["b"] = ProcessInfo(session_id="b", state=ProcessState.STOPPED)

    assert await pods.active_session_ids() == ["a"]
