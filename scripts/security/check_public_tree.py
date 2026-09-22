"""Reject machine-local state and identifiable network configuration in tracked files.

Run alongside a credential scanner. This is a privacy guard, not a replacement
for secret scanning or human review of screenshots and captured conversations.
"""

import re
import subprocess
from pathlib import Path

FORBIDDEN_PATH = re.compile(
    r"(^|/)(?:\.git|\.ssh|\.skuld|\.niuu|\.openclaw|node_modules|\.venv)/"
    r"|(^|/)(?:\.env(?:\..+)?|auth\.json|\.netrc)$"
    r"|\.(?:pem|p12|pfx|sqlite3?|db|log)$"
)
PRIVATE_TAILNET = re.compile(rb"\b[\w.-]+\.tail[a-z0-9]+\.ts\.net\b", re.I)
PRIVATE_PEM = re.compile(rb"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----\s+[A-Za-z0-9+/=\s]{64,}")


def main() -> None:
    paths = subprocess.check_output(["git", "ls-files", "-z"]).decode().split("\0")
    failed = []
    for name in filter(None, paths):
        path = Path(name)
        if not path.is_file():  # staged/working-tree deletion
            continue
        if FORBIDDEN_PATH.search(name) and not name.endswith((".example", ".sample")):
            failed.append((name, "machine-local state or raw capture"))
        data = path.read_bytes()
        if PRIVATE_TAILNET.search(data):
            failed.append((name, "personal tailnet hostname; use runtime configuration"))
        if PRIVATE_PEM.search(data):
            failed.append((name, "private key material; generate test keys at runtime"))
    for name, reason in failed:
        print(f"{name}: {reason}")  # Never print matching secret values.
    if failed:
        raise SystemExit(1)
    print("Public-tree privacy guard passed")


if __name__ == "__main__":
    main()
