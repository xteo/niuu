#!/usr/bin/env python3
"""F8: prove two Forge hosts serve the same attach, artifact and notification routes.

Run the same probes against each host, then compare the reports:

    forge_parity_probe.py probe --base http://thor:8080 --label thor --out thor.json
    forge_parity_probe.py probe --base http://mini-m6:8080 --label mini-m6 --out m6.json
    forge_parity_probe.py compare thor.json m6.json

``openapi`` builds a routes-only report from a recorded OpenAPI document (for example the
judge's live captures), so a host's route set can be compared without calling it.

**Read mode (default)** is statically zero-write, but it is **not** on the control plane's
production allow-list (``exp/prod-read-allowlist.json`` is profile boot-0: no routes).
Running it against a production host needs that allow-list's owner to add these routes
first. It sends GET only, with no identity headers, no ``dev*`` params and no proxy, never
follows a redirect, waits 10 s at most and makes at most one request per 200 ms (the
allow-list's transport block). It reads the OpenAPI document, ``/health``, ``/instance``
and the two artifact feeds and the notification feed, each pinned to the probed host with
``scope=local``. It makes no per-session reads, because they reach the owner lookup
(``GET /sessions/{id}`` reconciles with an UPDATE).

**Write mode (``--write``)** runs the behaviour checks against a scratch Forge only:
- register a project;
- create a session with ``coordination`` and a ``dispatch_id``, then replay the create;
- read, detach and re-attach the session's project;
- set a description;
- read attachments and the session's notifications;
- clean up: stop and archive the session, archive the project.

It refuses a host that isn't loopback, any production port, and any Forge whose
``/instance`` id doesn't equal ``--expect-instance-id``.

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
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from typing import Any

FORGE = "/api/v1/forge"
# exp/prod-read-allowlist.json "transport": timeout_s 10, min_interval_ms 200 (serial here).
TIMEOUT_S = 10.0
MIN_INTERVAL_S = 0.2
# Production and exp ports on Thor (Forge 8080, Work 8280, exp Forge 8380, agent service
# 9500, exp CP 9600). Write mode never runs against them, even on loopback.
PRODUCTION_PORTS = frozenset({8080, 8280, 8380, 9500, 9600})

# F8 (lanes.md: attach, artifacts, notifications), the session description and attachments
# index m6 lacks (design §V12), and the Forge-side routes the control plane reads (§V12/§V13).
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
    ("GET", f"{FORGE}/sessions/{{session_id}}"),
    ("GET", f"{FORGE}/sessions/{{session_id}}/log"),
    ("GET", f"{FORGE}/sessions/{{session_id}}/log/head"),
    ("GET", f"{FORGE}/sessions/{{session_id}}/conversation/turns/{{turn_id}}"),
    ("POST", f"{FORGE}/sessions/{{session_id}}/messages"),
    ("GET", f"{FORGE}/sessions/{{session_id}}/message-deliveries/{{request_id}}"),
    ("GET", f"{FORGE}/feature-flags"),
)

FEED_KEYS = ("host", "items", "next_after", "has_more", "instance_id")
INSTANCE_KEYS = ("host", "tailnet_name", "instance_id")

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
    """A paced HTTP client: no identity, no proxy, never follows a redirect."""

    def __init__(self, base: str, *, write: bool, sleep: Callable[[float], None] = time.sleep):
        self.base = base.rstrip("/")
        self.write = write
        self._sleep = sleep
        self._last = 0.0
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())

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


def _expect(
    name: str, resp: Response, status: int | tuple[int, ...], keys: Iterable[str] = ()
) -> Check:
    wanted = (status,) if isinstance(status, int) else status
    present = _keys(resp.body)
    missing = [k for k in keys if k not in present]
    if resp.status not in wanted:
        detail = resp.body.get("detail") if isinstance(resp.body, dict) else resp.body
        return Check(name, False, resp.status, f"expected {wanted}: {detail}", present)
    if missing:
        return Check(name, False, resp.status, f"missing keys {missing}", present)
    return Check(name, True, resp.status, "", list(keys))


def forge_routes(openapi: Any) -> list[str]:
    """Every ``METHOD path`` under the Forge prefix that the host serves."""
    paths = openapi.get("paths", {}) if isinstance(openapi, dict) else {}
    methods = ("get", "post", "put", "patch", "delete")
    return sorted(
        f"{method.upper()} {path}"
        for path, item in paths.items()
        if path.startswith(FORGE)
        for method in item
        if method in methods
    )


def route_checks(routes: list[str]) -> list[Check]:
    served = set(routes)
    checks = []
    for method, path in REQUIRED_ROUTES:
        ok = f"{method} {path}" in served
        checks.append(Check(f"route {method} {path}", ok, None, "" if ok else "absent"))
    return checks


def health_check(resp: Response) -> Check:
    """Forge is healthy when no Forge plugin failed; other plugins are reported only."""
    name = "GET /health (forge plugins)"
    body = resp.body if isinstance(resp.body, dict) else {}
    if "failed_plugins" not in body:
        return Check(name, False, resp.status, f"no health body: {body}")
    failed = sorted(body.get("failed_plugins") or [])
    forge_failed = [plugin for plugin in failed if plugin in FORGE_PLUGINS]
    if forge_failed:
        return Check(name, False, resp.status, f"failed: {forge_failed}")
    others = [plugin for plugin in failed if plugin not in FORGE_PLUGINS]
    return Check(name, True, resp.status, f"other plugins failed: {others}" if others else "")


def _pinned(name: str, resp: Response, keys: Iterable[str], instance: dict[str, Any]) -> Check:
    """A feed answered by the probed host itself (judge R11-5: scope=local, identity checked)."""
    check = _expect(name, resp, 200, keys)
    if not check.ok:
        return check
    want = instance.get("instance_id")
    got = resp.body.get("instance_id")
    if want != got:
        return Check(name, False, resp.status, f"answered by instance {got}, not {want}")
    host = resp.body.get("host")
    if "host" in resp.body and host != instance.get("host"):
        return Check(name, False, resp.status, f"answered for host {host}")
    return check


def safe_checks(client: Client, health: Response) -> tuple[list[Check], dict[str, Any]]:
    checks = [health_check(health)]
    resp = client.request("GET", f"{FORGE}/instance")
    checks.append(_expect("GET instance", resp, 200, INSTANCE_KEYS))
    instance = resp.body if checks[-1].ok else {}
    for feed in ("artifact-publications", "artifact-bursts"):
        resp = client.request("GET", f"{FORGE}/{feed}?scope=local&limit=1")
        checks.append(_pinned(f"GET {feed}", resp, FEED_KEYS, instance))
    resp = client.request("GET", f"{FORGE}/notifications?scope=local&limit=1")
    keys = ("items", "head_seq", "revision", "instance_id")
    checks.append(_pinned("GET notifications", resp, keys, instance))
    return checks, {k: instance.get(k) for k in INSTANCE_KEYS}


def _same_session(name: str, replay: Response, session_id: str) -> Check:
    body = replay.body if isinstance(replay.body, dict) else {}
    if replay.status not in (200, 201):
        return Check(name, False, replay.status, f"expected 200/201: {body.get('detail')}")
    if body.get("id") != session_id:
        return Check(name, False, replay.status, f"made {body.get('id')}, not {session_id}")
    return Check(name, True, replay.status)


def _attach_cycle(client: Client, session: str, project_id: str) -> list[Check]:
    """Read the session's project, detach it (the Undo call), then attach it again."""
    current = client.request("GET", f"{session}/project")
    checks = [_expect("GET session project", current, 200, ("revision",))]
    if not checks[-1].ok:
        return checks
    body = {"project_id": None, "expected_revision": current.body["revision"]}
    detach = client.request("PUT", f"{session}/project", body)
    checks.append(_expect("PUT session project detach", detach, 200, ("revision",)))
    if not checks[-1].ok:
        return checks
    body = {"project_id": project_id, "expected_revision": detach.body["revision"]}
    attach = client.request("PUT", f"{session}/project", body)
    checks.append(_expect("PUT session project attach", attach, 200, ("revision",)))
    return checks


def _cleanup(client: Client, session: str | None, project_id: str) -> list[Check]:
    """Stop and archive what the probe made. Archive, not delete: a mini host refuses to
    delete a locally mounted workspace, and an archived row is the honest trace."""
    checks = []
    if session is not None:
        stop = client.request("POST", f"{session}/stop")
        checks.append(_expect("cleanup stop session", stop, (200, 202, 204, 409)))
        archive = client.request("PATCH", f"{session}/archive")
        checks.append(_expect("cleanup archive session", archive, (200, 204)))
    path = f"{FORGE}/projects/{project_id}"
    current = client.request("GET", path)
    revision = current.body.get("revision") if isinstance(current.body, dict) else None
    project = client.request("PATCH", path, {"status": "archived", "revision": revision})
    checks.append(_expect("cleanup archive project", project, 200))
    return checks


def write_checks(client: Client) -> list[Check]:
    """The behaviour F8 must show: a coordinated create, attach, description."""
    tag = uuid.uuid4().hex[:8]
    project_id = str(uuid.uuid4())
    resp = client.request(
        "POST", f"{FORGE}/projects", {"id": project_id, "name": f"f8-parity-{tag}"}
    )
    checks = [_expect("POST projects", resp, (200, 201), ("id",))]
    if not checks[-1].ok:
        return checks

    create = {
        "name": f"f8-parity-{tag}",
        "coordination": {"project_id": project_id, "role": "worker"},
        "dispatch_id": str(uuid.uuid4()),
    }
    first = client.request("POST", f"{FORGE}/sessions", create)
    checks.append(_expect("POST sessions coordination+dispatch_id", first, (200, 201), ("id",)))
    if not checks[-1].ok:
        return checks + _cleanup(client, None, project_id)
    session_id = first.body["id"]
    kept = (first.body.get("coordination") or {}).get("project_id") == project_id
    detail = "" if kept else f"got {first.body.get('coordination')}"
    checks.append(Check("create keeps coordination", kept, first.status, detail))
    replay = client.request("POST", f"{FORGE}/sessions", create)
    checks.append(_same_session("dispatch_id replay returns the same session", replay, session_id))

    session = f"{FORGE}/sessions/{session_id}"
    checks += _attach_cycle(client, session, project_id)
    described = client.request(
        "PUT", f"{session}/description", {"short_description": f"f8 parity {tag}"}
    )
    checks.append(_expect("PUT session description", described, 200))
    checks.append(_expect("GET attachments", client.request("GET", f"{session}/attachments"), 200))
    notices = client.request("GET", f"{session}/notifications")
    checks.append(_expect("GET session notifications", notices, 200))
    return checks + _cleanup(client, session, project_id)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def guard_write(base: str, expect_instance_id: str | None) -> None:
    """Write mode runs only on a named scratch Forge: loopback, not a production port."""
    url = urllib.parse.urlsplit(base)
    if not _is_loopback(url.hostname or ""):
        raise PermissionError("--write creates rows; it runs only against a loopback Forge")
    port = url.port or (443 if url.scheme == "https" else 80)
    if port in PRODUCTION_PORTS:
        raise PermissionError(f"--write refuses port {port}: a production or exp service")
    if not expect_instance_id:
        raise PermissionError("--write needs --expect-instance-id (the scratch Forge's id)")


def probe(
    base: str,
    label: str,
    *,
    write: bool,
    expect_instance_id: str | None = None,
    client: Client | None = None,
) -> dict[str, Any]:
    if write:
        guard_write(base, expect_instance_id)
    client = client or Client(base, write=write)
    health = client.request("GET", "/health")
    routes = forge_routes(client.request("GET", "/openapi.json").body)
    read, instance = safe_checks(client, health)
    checks = route_checks(routes) + read
    if write and instance.get("instance_id") != expect_instance_id:
        raise PermissionError(
            f"--write: {base} is instance {instance.get('instance_id')}, "
            f"not the expected {expect_instance_id}"
        )
    if write:
        checks += write_checks(client)
    body = health.body if isinstance(health.body, dict) else {}
    return {
        "label": label,
        "base": base,
        "mode": "write" if write else "read",
        "build": body.get("build"),
        "revision": body.get("revision"),
        "instance": instance,
        "forge_routes": routes,
        "checks": [c.as_dict() for c in checks],
        "on_par_ready": all(c.ok for c in checks),
    }


def openapi_report(openapi: Any, label: str, source: str) -> dict[str, Any]:
    """A routes-only report from a recorded OpenAPI document (no host is called)."""
    routes = forge_routes(openapi)
    checks = route_checks(routes)
    return {
        "label": label,
        "base": source,
        "mode": "openapi",
        "build": None,
        "revision": None,
        "instance": {},
        "forge_routes": routes,
        "checks": [c.as_dict() for c in checks],
        "on_par_ready": all(c.ok for c in checks),
    }


def _check_rows(reports: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    names: list[str] = []
    for report in reports:
        names += [c["name"] for c in report["checks"] if c["name"] not in names]
    on_par, lines = True, []
    for name in names:
        cells, statuses = [], set()
        for report in reports:
            found = next((c for c in report["checks"] if c["name"] == name), None)
            if found is None and report.get("mode") == "openapi" and not name.startswith("route "):
                cells.append("n/a")  # a recorded OpenAPI document carries routes only
                continue
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
        on_par = on_par and len(statuses) <= 1
        lines.append(f"{name} | " + " | ".join(cells))
    return on_par, lines


def _route_rows(reports: list[dict[str, Any]], excluded: set[str]) -> tuple[bool, list[str]]:
    """Every Forge route any host serves must be served by all of them."""
    sets = [set(r.get("forge_routes") or []) - excluded for r in reports]
    union = set().union(*sets)
    lines = []
    for route in sorted(union):
        cells = ["ok" if route in s else "ABSENT" for s in sets]
        if "ABSENT" in cells:
            lines.append(f"served route {route} | " + " | ".join(cells))
    return not lines, lines


def _identity_rows(reports: list[dict[str, Any]]) -> tuple[bool, list[str]]:
    """F7: live hosts answer with distinct instance ids and hosts."""
    live = [r for r in reports if r.get("mode") != "openapi"]
    ids = [r.get("instance", {}).get("instance_id") for r in live]
    hosts = [r.get("instance", {}).get("host") for r in live]
    if len(set(ids)) == len(ids) and len(set(hosts)) == len(hosts):
        return True, []
    return False, [f"instance identity not distinct | ids {ids} | hosts {hosts}"]


def compare(reports: list[dict[str, Any]], excluded: Iterable[str] = ()) -> tuple[bool, list[str]]:
    """On par when every host passes every check the others ran, with the same status, and
    serves every Forge route the others serve (minus ``excluded``)."""
    if len(reports) < 2:
        raise ValueError("compare needs at least two reports")
    lines = ["check | " + " | ".join(r["label"] for r in reports)]
    checks_ok, rows = _check_rows(reports)
    routes_ok, route_lines = _route_rows(reports, set(excluded))
    identity_ok, identity_lines = _identity_rows(reports)
    return checks_ok and routes_ok and identity_ok, lines + rows + route_lines + identity_lines


def _write(report: dict[str, Any], out: str | None) -> None:
    text = json.dumps(report, indent=2)
    if not out:
        print(text)
        return
    with open(out, "w") as fh:
        fh.write(text + "\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("probe", help="run the probes against one Forge")
    p.add_argument("--base", required=True, help="the Forge root URL, e.g. http://host:8080")
    p.add_argument("--label", required=True)
    p.add_argument("--write", action="store_true", help="scratch Forge only: behaviour checks")
    p.add_argument("--expect-instance-id", help="with --write: the scratch Forge's instance id")
    p.add_argument("--out", help="write the JSON report here (default: stdout)")
    o = sub.add_parser("openapi", help="a routes-only report from a recorded OpenAPI file")
    o.add_argument("file")
    o.add_argument("--label", required=True)
    o.add_argument("--out")
    c = sub.add_parser("compare", help="compare two or more reports")
    c.add_argument("reports", nargs="+")
    c.add_argument("--exclude", action="append", default=[], help="'METHOD path' to ignore")
    args = parser.parse_args(argv)

    if args.cmd == "probe":
        report = probe(
            args.base, args.label, write=args.write, expect_instance_id=args.expect_instance_id
        )
        _write(report, args.out)
        return 0 if report["on_par_ready"] else 1
    if args.cmd == "openapi":
        with open(args.file) as fh:
            report = openapi_report(json.load(fh), args.label, args.file)
        _write(report, args.out)
        return 0 if report["on_par_ready"] else 1

    if len(args.reports) < 2:
        parser.error("compare needs at least two reports")
    reports = []
    for path in args.reports:
        with open(path) as fh:
            reports.append(json.load(fh))
    on_par, lines = compare(reports, args.exclude)
    print("\n".join(lines))
    print("ON PAR" if on_par else "NOT ON PAR")
    return 0 if on_par else 1


if __name__ == "__main__":
    sys.exit(main())
