#!/usr/bin/env python3
"""F8: prove two Forge hosts serve the same attach, artifact and notification routes.

Run the same probes against each host, then compare the reports:

    forge_parity_probe.py probe --base http://thor:8080 --label thor --out thor.json
    forge_parity_probe.py probe --base http://mini-m6:8080 --label mini-m6 --out m6.json
    forge_parity_probe.py compare thor.json m6.json

``probe`` is safe against a production host by default. It follows the control plane's
production read rules (``exp/prod-read-allowlist.json``): GET only, no identity headers
or ``dev*`` params, no redirects, a 10 s timeout, at most one request per 200 ms. It
only reads routes that never write: the OpenAPI document, ``/health``, ``/instance``,
the two artifact feeds and the notification feed. Per-session reads are left out,
because they reach the owner lookup (``GET /sessions/{id}`` reconciles with an UPDATE).

``--write`` adds the behaviour checks: register a project, create a session with
``coordination`` and a ``dispatch_id`` (and replay the create), read, detach and
re-attach the session's project, set a description, and read attachments and the
session's notifications. It creates rows, so it refuses any host that is not loopback.

Standard library only, so it runs from any host (m6's system python included).
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

FORGE = "/api/v1/forge"
TIMEOUT_S = 10.0
MIN_INTERVAL_S = 0.2

# The routes F8 brings to m6 (lanes.md F8: attach, artifacts, notifications; design §V12
# also names the session description and the attachments index m6 lacks).
REQUIRED_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", f"{FORGE}/instance"),
    ("GET", f"{FORGE}/artifact-publications"),
    ("GET", f"{FORGE}/artifact-bursts"),
    ("GET", f"{FORGE}/artifact-bursts/{{burst_id}}"),
    ("GET", f"{FORGE}/notifications"),
    ("GET", f"{FORGE}/sessions/{{session_id}}/notifications"),
    ("GET", f"{FORGE}/sessions/{{session_id}}/project"),
    ("PUT", f"{FORGE}/sessions/{{session_id}}/project"),
    ("PUT", f"{FORGE}/sessions/{{session_id}}/description"),
    ("GET", f"{FORGE}/sessions/{{session_id}}/attachments"),
    ("GET", f"{FORGE}/projects"),
    ("POST", f"{FORGE}/projects"),
    ("GET", f"{FORGE}/sessions"),
    ("POST", f"{FORGE}/sessions"),
)

FEED_KEYS = ("host", "items", "next_after", "has_more")

# The plugins that serve the Forge routes. Hosts differ on purpose elsewhere (m6 runs
# without ting, mimir, ravn and observatory), so only these decide /health here.
FORGE_PLUGINS = frozenset({"volundr", "guild", "niuu"})


@dataclass
class Check:
    name: str
    ok: bool
    status: int | None = None
    detail: str = ""
    keys: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "keys": self.keys,
        }


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:  # noqa: D102
        return None


@dataclass
class Response:
    status: int
    body: Any


class Client:
    """A paced HTTP client that sends no identity and never follows a redirect."""

    def __init__(self, base: str, *, write: bool, sleep: Callable[[float], None] = time.sleep):
        self.base = base.rstrip("/")
        self.write = write
        self._sleep = sleep
        self._last = 0.0
        self._opener = urllib.request.build_opener(_NoRedirect())

    def request(self, method: str, path: str, body: Any = None) -> Response:
        if method != "GET" and not self.write:
            raise PermissionError(f"{method} {path}: read-only probe")
        wait = self._last + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            self._sleep(wait)
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with self._opener.open(req, timeout=TIMEOUT_S) as resp:
                return Response(resp.status, _decode(resp.read()))
        except urllib.error.HTTPError as exc:
            return Response(exc.code, _decode(exc.read()))
        finally:
            self._last = time.monotonic()


def _decode(raw: bytes) -> Any:
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode(errors="replace")[:200]


def _keys(body: Any) -> list[str]:
    return sorted(body) if isinstance(body, dict) else []


def _expect(name: str, resp: Response, status: int | tuple[int, ...], keys=()) -> Check:
    wanted = (status,) if isinstance(status, int) else status
    present = _keys(resp.body)
    missing = [k for k in keys if k not in present]
    if resp.status not in wanted:
        detail = resp.body.get("detail") if isinstance(resp.body, dict) else resp.body
        return Check(name, False, resp.status, f"expected {wanted}: {detail}", present)
    if missing:
        return Check(name, False, resp.status, f"missing keys {missing}", present)
    return Check(name, True, resp.status, "", [k for k in keys])


def route_checks(openapi: Any) -> list[Check]:
    paths = openapi.get("paths", {}) if isinstance(openapi, dict) else {}
    checks = []
    for method, path in REQUIRED_ROUTES:
        served = method.lower() in paths.get(path, {})
        checks.append(Check(f"route {method} {path}", served, None, "" if served else "absent"))
    return checks


def health_check(resp: Response) -> Check:
    """Forge is healthy when no Forge plugin failed; other plugins are reported only."""
    body = resp.body if isinstance(resp.body, dict) else {}
    if "failed_plugins" not in body:
        return Check("GET /health (forge plugins)", False, resp.status, f"no health body: {body}")
    failed = sorted(body.get("failed_plugins") or [])
    forge_failed = [name for name in failed if name in FORGE_PLUGINS]
    others = [name for name in failed if name not in FORGE_PLUGINS]
    detail = f"other plugins failed: {others}" if others else ""
    if forge_failed:
        return Check("GET /health (forge plugins)", False, resp.status, f"failed: {forge_failed}")
    return Check("GET /health (forge plugins)", True, resp.status, detail)


def safe_checks(client: Client) -> list[Check]:
    checks = [health_check(client.request("GET", "/health"))]
    instance = client.request("GET", f"{FORGE}/instance")
    checks.append(_expect("GET instance", instance, 200, ("host", "tailnet_name", "instance_id")))
    for feed in ("artifact-publications", "artifact-bursts"):
        resp = client.request("GET", f"{FORGE}/{feed}?limit=1")
        checks.append(_expect(f"GET {feed}", resp, 200, FEED_KEYS))
    resp = client.request("GET", f"{FORGE}/notifications?limit=1")
    checks.append(_expect("GET notifications", resp, 200, ("items", "head_seq", "revision")))
    return checks


def write_checks(client: Client) -> list[Check]:
    """The behaviour F8 must show: a coordinated create, attach, description."""
    tag = uuid.uuid4().hex[:8]
    project_id = str(uuid.uuid4())
    dispatch_id = str(uuid.uuid4())
    checks: list[Check] = []

    resp = client.request(
        "POST", f"{FORGE}/projects", {"id": project_id, "name": f"f8-parity-{tag}"}
    )
    checks.append(_expect("POST projects", resp, (200, 201), ("id",)))
    if not checks[-1].ok:
        return checks

    create = {
        "name": f"f8-parity-{tag}",
        "coordination": {"project_id": project_id, "role": "worker"},
        "dispatch_id": dispatch_id,
    }
    first = client.request("POST", f"{FORGE}/sessions", create)
    checks.append(_expect("POST sessions coordination+dispatch_id", first, (200, 201), ("id",)))
    if not checks[-1].ok:
        return checks
    session_id = first.body["id"]
    coordination = first.body.get("coordination") or {}
    checks.append(
        Check(
            "create keeps coordination",
            coordination.get("project_id") == project_id,
            first.status,
            "" if coordination.get("project_id") == project_id else f"got {coordination}",
        )
    )
    replay = client.request("POST", f"{FORGE}/sessions", create)
    same = isinstance(replay.body, dict) and replay.body.get("id") == session_id
    checks.append(
        Check(
            "dispatch_id replay returns the same session",
            replay.status in (200, 201) and same,
            replay.status,
            "" if same else f"got {replay.body if not same else ''}",
        )
    )

    session = f"{FORGE}/sessions/{session_id}"
    current = client.request("GET", f"{session}/project")
    checks.append(_expect("GET session project", current, 200, ("revision",)))
    if checks[-1].ok:
        revision = current.body["revision"]
        detach = client.request(
            "PUT", f"{session}/project", {"project_id": None, "expected_revision": revision}
        )
        checks.append(_expect("PUT session project detach", detach, 200, ("revision",)))
        if checks[-1].ok:
            attach = client.request(
                "PUT",
                f"{session}/project",
                {"project_id": project_id, "expected_revision": detach.body["revision"]},
            )
            checks.append(_expect("PUT session project attach", attach, 200, ("revision",)))

    described = client.request(
        "PUT", f"{session}/description", {"short_description": f"f8 parity {tag}"}
    )
    checks.append(_expect("PUT session description", described, 200))
    checks.append(_expect("GET attachments", client.request("GET", f"{session}/attachments"), 200))
    checks.append(
        _expect("GET session notifications", client.request("GET", f"{session}/notifications"), 200)
    )
    client.request("DELETE", session)
    return checks


def _is_loopback(base: str) -> bool:
    host = urllib.parse.urlsplit(base).hostname or ""
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def probe(base: str, label: str, *, write: bool, client: Client | None = None) -> dict[str, Any]:
    if write and not _is_loopback(base):
        raise PermissionError("--write creates rows; it runs only against a loopback Forge")
    client = client or Client(base, write=write)
    openapi = client.request("GET", "/openapi.json")
    checks = route_checks(openapi.body) + safe_checks(client)
    if write:
        checks += write_checks(client)
    health = client.request("GET", "/health").body
    health = health if isinstance(health, dict) else {}
    return {
        "label": label,
        "base": base,
        "mode": "write" if write else "read",
        "build": health.get("build"),
        "revision": health.get("revision"),
        "checks": [c.as_dict() for c in checks],
        "on_par_ready": all(c.ok for c in checks),
    }


def compare(reports: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """On par when every host passes every check the others ran, with the same status."""
    names: list[str] = []
    for report in reports:
        for check in report["checks"]:
            if check["name"] not in names:
                names.append(check["name"])
    lines = ["check | " + " | ".join(r["label"] for r in reports)]
    on_par = True
    for name in names:
        cells, statuses = [], set()
        for report in reports:
            found = next((c for c in report["checks"] if c["name"] == name), None)
            if found is None:
                cells.append("not run")
                on_par = False
                continue
            mark = "ok" if found["ok"] else f"FAIL {found['detail']}"
            cells.append(f"{mark} ({found['status']})" if found["status"] else mark)
            # /health's code follows plugins the hosts disable on purpose; judge it by ok only.
            status = None if name.startswith("GET /health") else found["status"]
            statuses.add((found["ok"], status))
            on_par = on_par and found["ok"]
        if len(statuses) > 1:
            on_par = False
        lines.append(f"{name} | " + " | ".join(cells))
    return on_par, lines


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="run the probes against one Forge")
    p.add_argument("--base", required=True, help="the Forge root URL, e.g. http://host:8080")
    p.add_argument("--label", required=True)
    p.add_argument("--write", action="store_true", help="loopback only: behaviour checks")
    p.add_argument("--out", help="write the JSON report here (default: stdout)")
    c = sub.add_parser("compare", help="compare two or more probe reports")
    c.add_argument("reports", nargs="+")
    args = parser.parse_args(argv)

    if args.cmd == "probe":
        report = probe(args.base, args.label, write=args.write)
        text = json.dumps(report, indent=2)
        if args.out:
            with open(args.out, "w") as fh:
                fh.write(text + "\n")
        else:
            print(text)
        return 0 if report["on_par_ready"] else 1

    reports = []
    for path in args.reports:
        with open(path) as fh:
            reports.append(json.load(fh))
    on_par, lines = compare(reports)
    print("\n".join(lines))
    print("ON PAR" if on_par else "NOT ON PAR")
    return 0 if on_par else 1


if __name__ == "__main__":
    sys.exit(main())
