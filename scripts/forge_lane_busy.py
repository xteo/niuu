"""Which Forge lanes on this host are busy (LEX-1706). Runs on the host itself (python3 >= 3.9).

usage: forge_lane_busy.py FORGE_BASE TMUX_BIN [SESSION_ID ...]
A running lane is busy when (1) Forge does not report it idle at the prompt, or (2) its tmux
pane runs a tool command (a shell the agent spawned, e.g. a background release train in a
lane Forge reports idle) or a train process. A lane restart or a host switch ends the lane's
tmux server and with it everything in the pane's process tree; launchd and cron jobs (a
nightly qa.sh) and other detached processes are not in it and survive, so they are reported
as ``host_trains`` but do not make the host busy. With SESSION_IDs only those lanes are
judged. Prints JSON; exit 1 when a lane is busy.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import urllib.request

TRAIN = re.compile(r"release\.py|qa\.sh|xcodebuild|journey|\barchive\b|fastlane|altool|notarytool")
# A tool command (Claude's Bash tool, a background task, a Codex exec) runs in a shell the
# agent spawned; MCP servers and other long-lived helpers are direct executables.
TOOL_SHELL = re.compile(r"^(\S*/)?(ba|z|da)?sh\s+(-\S*c\S*\s|-c\s)")


def parse_ps(text: str) -> dict[str, tuple[str, str, str]]:
    """``ps -o pid=,ppid=,etime=,command=`` output as {pid: (ppid, etime, command)}."""
    procs = {}
    for line in text.splitlines():
        parts = line.split(None, 3)
        if len(parts) == 4:
            procs[parts[0]] = (parts[1], parts[2], parts[3])
    return procs


def lane_reasons(
    state: str | None, pane_pid: str | None, procs: dict[str, tuple[str, str, str]]
) -> tuple[list[str], set[str]]:
    """Why one lane is busy (empty when it may be restarted), and the pids in its pane."""
    kids: dict[str, list[str]] = {}
    for pid, (ppid, _, _) in procs.items():
        kids.setdefault(ppid, []).append(pid)

    def tree(pid: str) -> list[str]:
        out = []
        for kid in kids.get(pid, []):
            out += [kid, *tree(kid)]
        return out

    reasons = [] if state == "idle" else [f"forge activity {state}"]
    pids: set[str] = set()
    for child in kids.get(pane_pid, []) if pane_pid else []:
        cmd, sub = procs[child][2], tree(child)
        pids.update([child, *sub])
        trains = [procs[p][2][:80] for p in [child, *sub] if TRAIN.search(procs[p][2])]
        if trains:
            reasons.append("train: " + trains[0])
        elif TOOL_SHELL.search(cmd):
            reasons.append(f"tool command running {procs[child][1]}: {cmd[:80]}")
    return reasons, pids


def host_trains(procs: dict[str, tuple[str, str, str]], lane_pids: set[str]) -> list[str]:
    """Train processes outside every lane's pane (launchd or cron jobs, detached runs)."""
    return [
        f"{pid} {etime} {cmd[:100]}"
        for pid, (_, etime, cmd) in procs.items()
        if TRAIN.search(cmd) and pid not in lane_pids and "lane_busy.py" not in cmd
    ]


def main(argv: list[str]) -> int:
    base, tmux, *only = argv
    url = f"{base}/api/v1/forge/sessions?scope=local"
    rows = json.load(urllib.request.urlopen(url, timeout=20))
    lanes = [r for r in rows if r.get("status") == "running" and (not only or r["id"] in only)]
    ps = subprocess.run(
        ["ps", "-axo", "pid=,ppid=,etime=,command="], capture_output=True, text=True, check=True
    )
    procs = parse_ps(ps.stdout)
    busy, lane_pids = [], set()
    for lane in lanes:
        sid = lane["id"]
        sock = f"/tmp/skuld-tmux-{os.getuid()}/skuld-{sid}.sock"
        pane = subprocess.run(
            [tmux, "-S", sock, "list-panes", "-t", f"skuld-{sid}", "-F", "#{pane_pid}"],
            capture_output=True,
            text=True,
        ).stdout.split()
        reasons, pids = lane_reasons(lane.get("activity_state"), pane[0] if pane else None, procs)
        lane_pids |= pids
        if reasons:
            busy.append({"id": sid, "name": lane.get("name"), "reasons": reasons})
    host = host_trains(procs, lane_pids)
    print(json.dumps({"lanes": len(lanes), "busy": busy, "host_trains": host}))
    return 1 if busy else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
