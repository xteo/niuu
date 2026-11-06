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
    request = urllib.request.Request(url, method=method, data=b"" if method == "POST" else None)
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return response.status, json.load(response)
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")[:300]
        return exc.code, body


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


def restart_one(base: str, row: dict, *, start_timeout_s: float, poll_s: float) -> str:
    """Stop and start one session; ``restarted`` or why it did not count."""
    api, sid = _api(base), row["id"]
    resume_id = row.get("cli_session_id")
    if not resume_id:
        return "not restarted: no recorded conversation id to resume"
    code, body = _call("POST", f"{api}/sessions/{sid}/stop")
    if code != 200:
        return f"stop failed: HTTP {code} {body}"
    deadline = time.monotonic() + start_timeout_s
    if not _wait(lambda: _session(api, sid).get("status") == "stopped", deadline, poll_s):
        return f"did not stop within {start_timeout_s:g}s; start it by hand"
    code, body = _call("POST", f"{api}/sessions/{sid}/start")
    if code != 200:
        return f"STOPPED, start failed: HTTP {code} {body}; start it by hand"
    deadline = time.monotonic() + start_timeout_s

    def contract_met():
        code, version = _call("GET", f"{api}/sessions/{sid}/runtime-version")
        return code == 200 and (version.get("contract") or {}).get("state") == "met"

    if not _wait(contract_met, deadline, poll_s):
        return f"started, but its broker did not meet the contract within {start_timeout_s:g}s"
    after = _session(api, sid).get("cli_session_id")
    if after != resume_id:
        return f"started on a new conversation ({after}), not {resume_id}: context not resumed"
    return "restarted"


def restart(args: argparse.Namespace) -> int:
    until = datetime.fromisoformat(args.until.replace("Z", "+00:00"))
    held, only = set(args.hold), set(args.only)
    outcome: dict[str, dict] = {}

    def record():
        if args.evidence:
            Path(args.evidence).write_text(json.dumps(outcome, indent=2) + "\n")

    while True:
        rows = audit(args.base)
        below = [r for r in rows if r["contract"] != "met" and r["id"] not in outcome]
        if only:
            below = [r for r in below if r["id"] in only]
        todo = [r for r in below if r["name"] not in held or r["id"] in only]
        for r in below:
            if r not in todo:
                outcome[r["id"]] = {**r, "result": "held: restart only on its owner's GO"}
        todo = [r for r in todo if r["contract"] == "below"]
        busy = {}
        if todo:
            verdict = forge_lane_busy.judge(args.base, args.tmux, [r["id"] for r in todo])
            busy = {b["id"]: b["reasons"] for b in verdict["busy"]}
        for r in todo:
            if r["id"] in busy:
                continue
            result = restart_one(args.base, r, start_timeout_s=args.start_timeout, poll_s=args.poll)
            outcome[r["id"]] = {**r, "result": result}
            print(f"{datetime.now(UTC):%FT%TZ} {r['name']} ({r['id']}): {result}")
            record()
        waiting = [r for r in todo if r["id"] in busy]
        if not waiting or datetime.now(UTC) >= until:
            break
        time.sleep(args.retry)
    for r in waiting:
        outcome[r["id"]] = {**r, "result": "busy until the deadline: " + "; ".join(busy[r["id"]])}
    for r in audit(args.base):
        if r["contract"] not in ("met", "below") and r["id"] not in outcome:
            outcome[r["id"]] = {**r, "result": f"broker not probed ({r['contract']})"}
    record()
    print(json.dumps(outcome, indent=2))
    left = [o for o in outcome.values() if o["result"] != "restarted" and o["name"] not in held]
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
    r.add_argument("--hold", action="append", default=[], help="session name never restarted")
    r.add_argument("--only", action="append", default=[], help="restrict to this session id")
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
