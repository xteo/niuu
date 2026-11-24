"""Bridge one Antigravity CLI (``agy``) command hook to its Skuld broker.

``agy`` only runs command hooks: it starts this script once per event with the
event's JSON on stdin and reads the hook's JSON answer from stdout. The script
posts the event to the broker that launched this ``agy`` and prints the
broker's answer, so a ``PreToolUse`` decision comes from Skuld.

It must stay standard-library only: ``TmuxAgyTransport`` copies it into the
``agy`` home and runs it with the system Python, outside Skuld's environment.

The hook config lives in the ``agy`` home and is shared by every ``agy`` that
home runs, including one a person starts by hand. That ``agy`` was not launched
by a broker and has no ``SKULD_AGY_HOOK_URL``: the script then answers what
``agy`` would do with no hook at all (allow the tool, let the turn stop). An
``agy`` that a broker launched always has the URL, and any failure to reach
that broker exits non-zero with the reason on stderr.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

HOOK_URL_ENV = "SKULD_AGY_HOOK_URL"
HOOK_TIMEOUT_ENV = "SKULD_AGY_HOOK_TIMEOUT_S"
BROKER_TOKEN_FILE_ENV = "FORGE_BROKER_TOKEN_FILE"
EXIT_BROKER_UNREACHABLE = 2
EXIT_BAD_INPUT = 3

#: The answer ``agy`` gets for a hook it would otherwise not have run.
NO_BROKER_ANSWERS = {
    "PreToolUse": {"decision": "allow"},
    "Stop": {"decision": "stop"},
}


def _fail(message: str, code: int) -> None:
    sys.stderr.write(f"skuld agy hook: {message}\n")
    sys.exit(code)


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        _fail("usage: agy-hook <EventName>", EXIT_BAD_INPUT)
    event = args[0]
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except json.JSONDecodeError as exc:
        _fail(f"{event} payload is not JSON: {exc}", EXIT_BAD_INPUT)
    url = os.environ.get(HOOK_URL_ENV, "")
    if not url:
        sys.stdout.write(json.dumps(NO_BROKER_ANSWERS.get(event, {})))
        return
    token_file = os.environ.get(BROKER_TOKEN_FILE_ENV, "")
    if not token_file:
        _fail(f"{BROKER_TOKEN_FILE_ENV} is not set; the broker launched agy without it", 1)
    try:
        with open(token_file, encoding="utf-8") as handle:
            token = handle.read().strip()
    except OSError as exc:
        _fail(f"cannot read the broker token: {exc}", EXIT_BROKER_UNREACHABLE)
    timeout = float(os.environ.get(HOOK_TIMEOUT_ENV) or 0) or None
    request = urllib.request.Request(
        url,
        data=json.dumps({"event": event, "payload": payload}).encode(),
        headers={"content-type": "application/json", "authorization": f"Bearer {token}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode()
    except urllib.error.HTTPError as exc:
        _fail(f"broker refused {event}: {exc.code} {exc.read().decode()}", EXIT_BROKER_UNREACHABLE)
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        _fail(f"broker unreachable for {event}: {exc}", EXIT_BROKER_UNREACHABLE)
    sys.stdout.write(body)


if __name__ == "__main__":
    main()
