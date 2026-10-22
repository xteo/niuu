"""F8: the parity probe reads production safely and calls two hosts on par only when they are."""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts import forge_parity_probe as fpp
from scripts.forge_parity_probe import Client, Response, compare, health_check, probe

FORGE = "/api/v1/forge"


def _openapi(routes=fpp.REQUIRED_ROUTES, drop=()):
    paths: dict[str, dict] = {}
    for method, path in routes:
        if (method, path) in drop:
            continue
        paths.setdefault(path, {})[method.lower()] = {}
    return {"paths": paths}


class FakeForge:
    """A Forge as the probe sees it: a route table plus enough state for the write checks."""

    def __init__(self, *, current=True, failed_plugins=(), replay_new_id=False):
        self.current = current
        self.failed_plugins = list(failed_plugins)
        self.replay_new_id = replay_new_id
        self.calls: list[tuple[str, str, object]] = []
        self.sessions: dict[str, dict] = {}
        self.dispatch: dict[str, str] = {}
        self.revision = 0

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        route = path.split("?")[0]
        if route == "/openapi.json":
            drop = () if self.current else {("GET", f"{FORGE}/instance")}
            return Response(200, _openapi(drop=drop))
        if route == "/health":
            code = 503 if self.failed_plugins else 200
            return Response(code, {"status": "ok", "failed_plugins": self.failed_plugins})
        if route == f"{FORGE}/instance":
            if not self.current:
                return Response(404, {"detail": "Not Found"})
            return Response(200, {"host": "h", "tailnet_name": "t", "instance_id": "i"})
        if route in (f"{FORGE}/artifact-publications", f"{FORGE}/artifact-bursts"):
            return Response(200, {"host": "h", "items": [], "next_after": 0, "has_more": False})
        if route == f"{FORGE}/notifications":
            return Response(200, {"items": [], "head_seq": 0, "revision": 0})
        if route == f"{FORGE}/projects" and method == "POST":
            return Response(201, {"id": body["id"]})
        if route == f"{FORGE}/sessions" and method == "POST":
            key = body.get("dispatch_id")
            if key in self.dispatch and not self.replay_new_id:
                return Response(201, self.sessions[self.dispatch[key]])
            sid = str(uuid.uuid4())
            self.sessions[sid] = {"id": sid, "coordination": body.get("coordination")}
            self.dispatch[key] = sid
            return Response(201, self.sessions[sid])
        if route.endswith("/project"):
            if method == "PUT":
                self.revision += 1
            return Response(200, {"revision": self.revision})
        if route.endswith(("/description", "/attachments", "/notifications")):
            return Response(200, {})
        if method == "DELETE":
            return Response(204, "")
        return Response(404, {"detail": "Not Found"})


def _check(report, name):
    return next(c for c in report["checks"] if c["name"] == name)


def test_a_current_host_passes_every_read_check():
    report = probe("http://forge:8080", "thor", write=False, client=FakeForge())

    assert report["on_par_ready"] is True
    assert report["mode"] == "read"


def test_the_read_probe_only_ever_sends_gets_and_no_per_session_reads():
    forge = FakeForge()
    probe("http://forge:8080", "thor", write=False, client=forge)

    assert {method for method, _, _ in forge.calls} == {"GET"}
    # Per-session reads reach the owner lookup, which writes (prod-read-allowlist.json).
    assert not any("/sessions" in path for _, path, _ in forge.calls)


def test_an_old_host_fails_where_it_lacks_the_route():
    report = probe("http://m6:8080", "m6", write=False, client=FakeForge(current=False))

    assert report["on_par_ready"] is False
    assert _check(report, f"route GET {FORGE}/instance")["ok"] is False
    assert _check(report, "GET instance")["status"] == 404


def test_write_checks_cover_dispatch_attach_and_description():
    forge = FakeForge()
    report = probe("http://127.0.0.1:41180", "m6", write=True, client=forge)

    assert report["on_par_ready"] is True, [c for c in report["checks"] if not c["ok"]]
    names = {c["name"] for c in report["checks"]}
    assert {
        "POST sessions coordination+dispatch_id",
        "dispatch_id replay returns the same session",
        "PUT session project detach",
        "PUT session project attach",
        "PUT session description",
    } <= names
    assert any(method == "DELETE" for method, _, _ in forge.calls)  # cleans up its session


def test_a_dispatch_replay_that_makes_a_second_session_fails():
    forge = FakeForge(replay_new_id=True)
    report = probe("http://127.0.0.1:41180", "m6", write=True, client=forge)

    assert _check(report, "dispatch_id replay returns the same session")["ok"] is False


@pytest.mark.parametrize("base", ["http://100.92.69.62:8080", "http://mini-m6:8080"])
def test_write_mode_refuses_a_host_that_is_not_loopback(base):
    with pytest.raises(PermissionError):
        probe(base, "m6", write=True, client=FakeForge())


def test_health_judges_only_the_forge_plugins():
    other = health_check(Response(503, {"status": "degraded", "failed_plugins": ["ting"]}))
    forge = health_check(Response(503, {"status": "degraded", "failed_plugins": ["volundr"]}))
    blank = health_check(Response(502, "bad gateway"))

    assert other.ok is True and "ting" in other.detail
    assert forge.ok is False
    assert blank.ok is False


def test_compare_is_on_par_only_when_every_host_passes_with_the_same_status():
    thor = probe("http://a", "thor", write=False, client=FakeForge(failed_plugins=["ting"]))
    m6 = probe("http://b", "m6", write=False, client=FakeForge())
    old = probe("http://c", "m6-old", write=False, client=FakeForge(current=False))

    on_par, lines = compare([thor, m6])
    assert on_par is True, lines  # /health 503 (ting) vs 200 is not a Forge difference
    on_par, lines = compare([thor, old])
    assert on_par is False
    assert any("FAIL" in line and "instance" in line for line in lines)


def test_compare_flags_a_check_one_host_did_not_run():
    read = probe("http://a", "thor", write=False, client=FakeForge())
    write = probe("http://127.0.0.1:1", "m6", write=True, client=FakeForge())

    on_par, lines = compare([read, write])
    assert on_par is False
    assert any("not run" in line for line in lines)


def test_compare_flags_the_same_check_with_different_statuses():
    a = {"label": "a", "checks": [{"name": "x", "ok": True, "status": 200, "detail": ""}]}
    b = {"label": "b", "checks": [{"name": "x", "ok": True, "status": 201, "detail": ""}]}

    assert compare([a, b])[0] is False


class _Handler(BaseHTTPRequestHandler):
    seen: list[dict] = []

    def do_GET(self):  # noqa: N802
        _Handler.seen.append(dict(self.headers))
        if self.path == "/redirect":
            self.send_response(302)
            self.send_header("Location", "/health")
            self.end_headers()
            return
        body = json.dumps({"status": "ok", "failed_plugins": []}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


@pytest.fixture
def server():
    httpd = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    _Handler.seen.clear()
    yield f"http://127.0.0.1:{httpd.server_address[1]}"
    httpd.shutdown()


def test_the_client_sends_no_identity_and_never_follows_a_redirect(server):
    client = Client(server, write=False, sleep=lambda _: None)

    assert client.request("GET", "/health").status == 200
    assert client.request("GET", "/redirect").status == 302
    assert len(_Handler.seen) == 2  # the 302 was not followed
    for headers in _Handler.seen:
        lowered = {k.lower() for k in headers}
        assert not lowered & {"authorization", "cookie"}
        assert not any(k.startswith("x-auth-") for k in lowered)


def test_the_read_client_refuses_any_other_method(server):
    client = Client(server, write=False, sleep=lambda _: None)

    with pytest.raises(PermissionError):
        client.request("PUT", "/x", {})


def test_the_client_paces_its_requests(server):
    waits: list[float] = []
    client = Client(server, write=False, sleep=waits.append)

    client.request("GET", "/health")
    client.request("GET", "/health")

    assert waits and 0 < waits[0] <= fpp.MIN_INTERVAL_S


def test_main_probe_and_compare_round_trip(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(fpp, "Client", lambda base, write: FakeForge())
    a, b = tmp_path / "a.json", tmp_path / "b.json"

    assert fpp.main(["probe", "--base", "http://a", "--label", "a", "--out", str(a)]) == 0
    assert fpp.main(["probe", "--base", "http://b", "--label", "b", "--out", str(b)]) == 0
    assert fpp.main(["compare", str(a), str(b)]) == 0
    assert capsys.readouterr().out.strip().endswith("ON PAR")


def test_main_prints_the_report_and_fails_an_old_host(monkeypatch, capsys):
    monkeypatch.setattr(fpp, "Client", lambda base, write: FakeForge(current=False))

    assert fpp.main(["probe", "--base", "http://m6", "--label", "m6"]) == 1
    assert json.loads(capsys.readouterr().out)["on_par_ready"] is False
