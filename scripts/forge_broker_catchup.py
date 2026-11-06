#!/usr/bin/env python3
"""Bring running sessions' brokers up to this release's broker contract (LEX-1957).

An API-only promotion restarts Forge, not the session brokers, which keep the release
they were launched from. When a release raises the broker contract
(``niuu.domain.broker_contract``), every session still on an older broker silently
misses it: a broker without LEX-1910 stores no sender, so the apps show "Unknown sender".
Runs on the Forge host itself (python3 >= 3.9).

usage:
  forge_broker_catchup.py audit FORGE_BASE
      Every running local session's broker and contract state, as JSON. Exit 1 when a
      broker is below the contract or could not be probed.
  forge_broker_catchup.py restart FORGE_BASE TMUX_BIN --until ISO8601 [--hold NAME ...]
                          [--only SESSION_ID ...] [--evidence PATH]
      Restart each session below the contract onto this release, resuming its
      conversation, once it is idle at its prompt with no tool command or train in its
      pane (``forge_lane_busy``). A session named in ``--hold`` is listed, never
      restarted; ``--only`` restricts the run to the named sessions (an owner's GO), and
      a held session named there is restarted. Busy sessions are retried until
      ``--until``. Exit 0 when no session outside ``--hold`` is left below the contract.

A restart is stop then start. The broker resumes the engine's own conversation id, so a
session keeps its context; a session with no recorded conversation id cannot resume and
is never restarted. A restart counts only when the session runs again, its broker meets
the contract, and it kept the same conversation id. A failure is reported, never retried.
Run it detached (``systemd-run``): a run started from inside a session's pane is busy
work in that pane, so the gate never restarts the session that runs it.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import forge_lane_busy  # noqa: E402

# datetime.UTC needs 3.11; the host's python3 may be 3.9 (mini-m6).
UTC = timezone.utc  # noqa: UP017

REQUEST_TIMEOUT_S = 60


def _call(method: str, url: str) -> tuple[int, object]:
    """(HTTP status, body); status 0 when Forge could not be reached at all."""
    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        return exc.code, body
    except (OSError, ValueError) as exc:
        return 0, f"{type(exc).__name__}: {exc}"


def _api(base: str) -> str:
    return f"{base.rstrip('/')}/api/v1/forge"


def audit(base: str) -> list[dict]:
    """One row per running local session: its broker build and contract state."""
    api = _api(base)
    code, rows = _call("GET", f"{api}/sessions?scope=local")
    if code != 200 or not isinstance(rows, list):
        raise RuntimeError(f"listing sessions failed: HTTP {code} {rows}")
    out = []
    for row in rows:
        if row.get("status") != "running":
            continue
        code, version = _call("GET", f"{api}/sessions/{row['id']}/runtime-version")
        version = version if code == 200 and isinstance(version, dict) else {}
        current = version.get("current") or {}
        out.append(
            {
                "id": row["id"],
                "name": row.get("name"),
                "activity": row.get("activity_state"),
                "cli_session_id": row.get("cli_session_id"),
                "build": current.get("build"),
                "revision": current.get("revision"),
                "contract": (version.get("contract") or {}).get("state", f"probe HTTP {code}"),
            }
        )
    return out


def _session(api: str, sid: str) -> dict:
    code, row = _call("GET", f"{api}/sessions/{sid}")
    if code != 200 or not isinstance(row, dict):
        raise RuntimeError(f"reading session {sid} failed: HTTP {code} {row}")
    return row


def _wait(check, deadline: float, poll_s: float):
    while True:
        value = check()
        if value:
            return value
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll_s)


def transcript_exists(cli_session_id: str) -> bool:
    """Claude Code keeps a conversation as ~/.claude/projects/<cwd>/<id>.jsonl."""
    return any(Path.home().glob(f".claude/projects/*/{cli_session_id}.jsonl"))


def live_resume(tmux: str, socket_dir: str, sid: str) -> tuple[str, str] | None:
    """(pid, conversation id) of the Claude process in the session's first pane, if resuming."""
    panes = subprocess.run(
        [tmux, "-S", f"{socket_dir}/skuld-{sid}.sock", "list-panes", "-s", "-t", f"skuld-{sid}"]
        + ["-F", "#{pane_pid}"],
        capture_output=True,
        text=True,
    )
    pids = panes.stdout.split()
    if panes.returncode != 0 or not pids:
        return None
    ps = subprocess.run(["ps", "-o", "command=", "-p", pids[0]], capture_output=True, text=True)
    match = re.search(r"--resume\s+(\S+)", ps.stdout)
    return (pids[0], match.group(1)) if match else None


def restart_one(base: str, row: dict, args: argparse.Namespace) -> str:
    """Re-check, stop and start one session; ``restarted`` or why it did not count.

    The busy gate runs again right before the stop, so the decision is seconds old. After
    the stop every failure says the session may be left STOPPED.
    """
    api, sid = _api(base), row["id"]
    resume_id = row.get("cli_session_id")
    if not resume_id:
        return "not restarted: no recorded conversation id to resume"
    if not transcript_exists(resume_id):
        return f"not restarted: no transcript for conversation {resume_id}, so it cannot resume"
    verdict = forge_lane_busy.judge(
        base, args.tmux, [sid], background=True, socket_dir=args.socket_dir
    )
    if sid not in verdict["judged"]:
        return "not restarted: no longer running"
    if verdict["busy"]:
        return "busy: " + "; ".join(verdict["busy"][0]["reasons"])
    code, body = _call("POST", f"{api}/sessions/{sid}/stop")
    if code != 200:
        return f"stop failed: HTTP {code} {body}"
    try:
        return _start_again(api, sid, resume_id, args)
    except Exception as exc:  # noqa: BLE001 - a stopped session must be reported, never lost
        return f"STOPPED? {type(exc).__name__} after the stop: {exc}; check it and start it by hand"


def _start_again(api: str, sid: str, resume_id: str, args: argparse.Namespace) -> str:
    deadline = time.monotonic() + args.start_timeout
    if not _wait(lambda: _session(api, sid).get("status") == "stopped", deadline, args.poll):
        return f"did not stop within {args.start_timeout:g}s; check it and start it by hand"
    code, body = _call("POST", f"{api}/sessions/{sid}/start")
    if code != 200:
        return f"STOPPED, start failed: HTTP {code} {body}; start it by hand"
    deadline = time.monotonic() + args.start_timeout

    def contract_met():
        code, version = _call("GET", f"{api}/sessions/{sid}/runtime-version")
        return (
            code == 200
            and isinstance(version, dict)
            and (version.get("contract") or {}).get("state") == "met"
        )

    if not _wait(contract_met, deadline, args.poll):
        return f"started, but its broker did not meet the contract within {args.start_timeout:g}s"
    if not _wait(lambda: _session(api, sid).get("status") == "running", deadline, args.poll):
        return f"started, but it was not running within {args.start_timeout:g}s"
    live = _wait(lambda: live_resume(args.tmux, args.socket_dir, sid), deadline, args.poll)
    if live is None or live[1] != resume_id:
        return f"started, but its Claude is not resuming {resume_id} (live: {live}): context lost?"
    time.sleep(args.settle)
    again = live_resume(args.tmux, args.socket_dir, sid)
    if again != live:
        return f"started, but its resumed Claude (pid {live[0]}) exited within {args.settle:g}s"
    return "restarted"


def _resolve_holds(rows: list[dict], holds: list[str]) -> set[str]:
    """Session ids for ``--hold`` values (an id, or a name naming exactly one running session)."""
    ids = set()
    for hold in holds:
        matches = [r["id"] for r in rows if hold in (r["id"], r["name"])]
        if len(matches) != 1:
            raise SystemExit(f"--hold {hold} names {len(matches)} running sessions; give its id")
        ids.update(matches)
    return ids


def restart(args: argparse.Namespace) -> int:
    until = datetime.fromisoformat(args.until.replace("Z", "+00:00"))
    if until.tzinfo is None:
        raise SystemExit("--until needs a timezone (e.g. 2026-11-06T23:00:00Z)")
    rows = audit(args.base)
    running = {r["id"] for r in rows}
    held, only = _resolve_holds(rows, args.hold), set(args.only)
    unknown = only - running
    if unknown:
        raise SystemExit(f"--only names sessions that are not running here: {sorted(unknown)}")
    outcome: dict[str, dict] = {}
    for r in rows:  # with --only, only the named sessions are in scope, and naming one is its GO
        if r["id"] in held and not only and r["contract"] != "met":
            outcome[r["id"]] = {**r, "result": "held: restart only on its owner's GO"}
    for r in rows:
        if r["id"] in only and r["contract"] == "met" and r["id"] not in outcome:
            outcome[r["id"]] = {**r, "result": "not restarted: already meets the contract"}

    def record():
        if args.evidence:
            Path(args.evidence).write_text(json.dumps(outcome, indent=2) + "\n")

    waiting: dict[str, dict] = {}
    while True:
        last, waiting = waiting, {}
        for r in audit(args.base):
            if r["id"] in outcome or r["contract"] != "below" or (only and r["id"] not in only):
                continue
            if datetime.now(UTC) >= until:
                waiting[r["id"]] = last.get(r["id"]) or {
                    **r,
                    "result": "deadline passed before it was tried",
                }
                continue
            result = restart_one(args.base, r, args)
            if result.startswith("busy: "):
                waiting[r["id"]] = {**r, "result": "busy until the deadline: " + result[6:]}
                continue
            outcome[r["id"]] = {**r, "result": result}
            print(f"{datetime.now(UTC):%FT%TZ} {r['name']} ({r['id']}): {result}", flush=True)
            record()
        left_s = (until - datetime.now(UTC)).total_seconds()
        if not waiting or left_s <= 0:
            break
        time.sleep(min(args.retry, left_s))
    outcome.update(waiting)
    for r in audit(args.base):
        if r["contract"] not in ("met", "below") and r["id"] not in outcome:
            if not only or r["id"] in only:
                outcome[r["id"]] = {**r, "result": f"broker not probed ({r['contract']})"}
    for sid in only - set(outcome):
        outcome[sid] = {"id": sid, "name": None, "result": "not restarted: left the run"}
    record()
    print(json.dumps(outcome, indent=2))
    if only:
        return 0 if all(outcome[sid]["result"] == "restarted" for sid in only) else 1
    left = [o for sid, o in outcome.items() if o["result"] != "restarted" and sid not in held]
    return 1 if left else 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="command", required=True)
    a = sub.add_parser("audit")
    a.add_argument("base")
    r = sub.add_parser("restart")
    r.add_argument("base")
    r.add_argument("tmux")
    r.add_argument("--until", required=True, help="stop retrying busy sessions at (ISO 8601)")
    r.add_argument(
        "--hold", action="append", default=[], help="session id or unique name never restarted"
    )
    r.add_argument(
        "--only", action="append", default=[], help="restrict to this session id (all must restart)"
    )
    r.add_argument(
        "--socket-dir",
        default=f"/tmp/skuld-tmux-{os.getuid()}",
        help="the brokers' tmux socket directory (skuld tmux_socket_dir)",
    )
    r.add_argument(
        "--settle", type=float, default=15.0, help="seconds a resumed Claude must stay up"
    )
    r.add_argument("--evidence", help="write the per-session outcome here as it goes")
    r.add_argument("--poll", type=float, default=3.0, help="seconds between state checks")
    r.add_argument("--retry", type=float, default=60.0, help="seconds between busy re-checks")
    r.add_argument(
        "--start-timeout", type=float, default=180.0, help="seconds to stop or start a session"
    )
    args = parser.parse_args(argv)
    if args.command == "audit":
        rows = audit(args.base)
        print(json.dumps(rows, indent=2))
        return 0 if all(r["contract"] == "met" for r in rows) else 1
    return restart(args)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
