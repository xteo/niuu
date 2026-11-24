"""Start one of a Skuld session's stdio MCP servers for the Antigravity CLI (``agy``).

``agy`` reads MCP servers from one config per home, shared by every session in
that home, but each session's servers carry their own arguments (the Forge MCP
names its broker and token file). The shared config therefore lists this script
once per server name, and the broker that launched ``agy`` puts the real
command lines in ``SKULD_AGY_MCP_SERVERS``; this script replaces itself with
the one named on its command line.

Standard-library only: it is copied into the ``agy`` home and run with the
system Python. An ``agy`` started without a broker has no such variable, and
the server then fails to start with the reason on stderr.
"""

from __future__ import annotations

import json
import os
import sys

SERVERS_ENV = "SKULD_AGY_MCP_SERVERS"


def resolve(name: str, environ: dict[str, str]) -> tuple[list[str], dict[str, str]]:
    """The argv and environment for server ``name`` from the session's variable."""
    raw = environ.get(SERVERS_ENV, "")
    if not raw:
        raise SystemExit(
            f"skuld agy mcp: {SERVERS_ENV} is not set; {name!r} runs only in a Forge session"
        )
    servers = json.loads(raw)
    spec = servers.get(name)
    if not isinstance(spec, dict):
        raise SystemExit(f"skuld agy mcp: this session has no MCP server named {name!r}")
    argv = [str(spec["command"]), *[str(arg) for arg in spec.get("args") or []]]
    env = dict(environ)
    env.update({str(k): str(v) for k, v in (spec.get("env") or {}).items()})
    return argv, env


def main(argv: list[str] | None = None) -> None:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        raise SystemExit("usage: agy-mcp <server-name>")
    command, env = resolve(args[0], dict(os.environ))
    os.execvpe(command[0], command, env)


if __name__ == "__main__":
    main()
