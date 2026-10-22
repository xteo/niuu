"""F8: the parity probe reads statically zero-write routes, writes only to a named scratch
Forge, and calls hosts on par only when they serve the same Forge routes and behaviour."""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts import forge_parity_probe as fpp
from scripts.forge_parity_probe import (
    Client,
    Response,
    compare,
    guard_write,
    health_check,
    openapi_report,
    probe,
)

FORGE = "/api/v1/forge"
SCRATCH = "http://127.0.0.1:41180"


def _openapi(drop=(), extra=()):
    paths: dict[str, dict] = {}
    for method, path in (*fpp.REQUIRED_ROUTES, *extra):
        if (method, path) in drop:
            continue
        paths.setdefault(path, {})[method.lower()] = {}
    paths["/health"] = {"get": {}}  # outside the Forge prefix: never compared
    return {"paths": paths}


class FakeForge:
    """A Forge as the probe sees it: a route table plus enough state for the write checks."""

    def __init__(
        self,
        *,
        instance_id="inst-a",
        host="thor",
        current=True,
        failed_plugins=(),
        replay_new_id=False,
        extra_routes=(),
        feed_instance=None,
        cleanup_status=200,
    ):
        self.instance_id = instance_id
        self.host = host
        self.current = current
        self.failed_plugins = list(failed_plugins)
        self.replay_new_id = replay_new_id
        self.extra_routes = extra_routes
        self.feed_instance = feed_instance or instance_id
        self.cleanup_status = cleanup_status
        self.calls: list[tuple[str, str, object]] = []
        self.sessions: dict[str, dict] = {}
        self.dispatch: dict[str, str] = {}
        self.revision = 0

    def _feed(self, **body):
        return Response(200, {"instance_id": self.feed_instance, **body})

    def request(self, method, path, body=None):  # noqa: C901 - a route table
        self.calls.append((method, path, body))
        route = path.split("?")[0]
        if route == "/openapi.json":
            drop = () if self.current else {("GET", f"{FORGE}/instance")}
            return Response(200, _openapi(drop=drop, extra=self.extra_routes))
        if route == "/health":
            code = 503 if self.failed_plugins else 200
            return Response(code, {"status": "ok", "failed_plugins": self.failed_plugins})
        if route == f"{FORGE}/instance":
            if not self.current:
                return Response(404, {"detail": "Not Found"})
            body = {"host": self.host, "tailnet_name": "t", "instance_id": self.instance_id}
            return Response(200, body)
        if route in (f"{FORGE}/artifact-publications", f"{FORGE}/artifact-bursts"):
            return self._feed(host=self.host, items=[], next_after=0, has_more=False)
        if route == f"{FORGE}/notifications":
            return self._feed(items=[], head_seq=0, revision=0)
        if route == f"{FORGE}/projects" and method == "POST":
            return Response(201, {"id": body["id"]})
        if route.startswith(f"{FORGE}/projects/") and method == "GET":
            return Response(200, {"revision": 3})
        if route.startswith(f"{FORGE}/projects/") and method == "PATCH":
            if body.get("revision") != 3:
                return Response(422, {"detail": "revision required"})
            return Response(self.cleanup_status, {"status": body["status"]})
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
        if route.endswith(("/stop", "/archive")):
            return Response(self.cleanup_status, {})
        if route.endswith(("/description", "/attachments", "/notifications")):
            return Response(200, {})
        return Response(404, {"detail": "Not Found"})


def _check(report, name):
    return next(c for c in report["checks"] if c["name"] == name)


def _write(forge, base=SCRATCH):
    return probe(base, "scratch", write=True, expect_instance_id=forge.instance_id, client=forge)


def test_a_current_host_passes_every_read_check():
    report = probe("http://forge:8080", "thor", write=False, client=FakeForge())

    assert report["on_par_ready"] is True
    assert report["mode"] == "read"
    assert report["instance"] == {"host": "thor", "tailnet_name": "t", "instance_id": "inst-a"}


def test_the_read_probe_only_sends_gets_pinned_to_the_host_and_no_session_reads():
    forge = FakeForge()
    probe("http://forge:8080", "thor", write=False, client=forge)

    assert {method for method, _, _ in forge.calls} == {"GET"}
    # Per-session reads reach the owner lookup, which writes (prod-read-allowlist.json).
    assert not any("/sessions" in path for _, path, _ in forge.calls)
    feeds = [p for _, p, _ in forge.calls if "artifact-" in p or "/notifications" in p]
    assert len(feeds) == 3 and all("scope=local" in p for p in feeds)
    assert sum(path == "/health" for _, path, _ in forge.calls) == 1


def test_a_feed_answered_by_another_instance_fails():
    report = probe("http://forge:8080", "thor", write=False, client=FakeForge(feed_instance="x"))

    assert _check(report, "GET notifications")["ok"] is False
    assert "answered by instance x" in _check(report, "GET artifact-bursts")["detail"]


def test_an_old_host_fails_where_it_lacks_the_route():
    report = probe("http://m6:8080", "m6", write=False, client=FakeForge(current=False))

    assert report["on_par_ready"] is False
    assert _check(report, f"route GET {FORGE}/instance")["ok"] is False
    assert _check(report, "GET instance")["status"] == 404


def test_write_checks_cover_dispatch_attach_description_and_cleanup():
    forge = FakeForge()
    report = _write(forge)

    assert report["on_par_ready"] is True, [c for c in report["checks"] if not c["ok"]]
    names = {c["name"] for c in report["checks"]}
    assert {
        "POST sessions coordination+dispatch_id",
        "dispatch_id replay returns the same session",
        "PUT session project detach",
        "PUT session project attach",
        "PUT session description",
        "cleanup stop session",
        "cleanup archive session",
        "cleanup archive project",
    } <= names


def test_a_failed_cleanup_is_a_failed_check():
    report = _write(FakeForge(cleanup_status=500))

    assert report["on_par_ready"] is False
    assert _check(report, "cleanup archive project")["ok"] is False


def test_a_dispatch_replay_that_makes_a_second_session_fails():
    report = _write(FakeForge(replay_new_id=True))

    assert _check(report, "dispatch_id replay returns the same session")["ok"] is False


@pytest.mark.parametrize(
    "base",
    [
        "http://100.92.69.62:41180",  # not loopback
        "http://mini-m6:41180",
        "http://127.0.0.1:8080",  # production Forge listens on 0.0.0.0
        "http://localhost:8280",
        "http://127.0.0.1:8380",
        "http://127.0.0.1:9500",
        "http://127.0.0.1:9600",
    ],
)
def test_write_mode_refuses_anything_but_a_scratch_port(base):
    with pytest.raises(PermissionError):
        guard_write(base, "inst-a")


def test_write_mode_needs_the_expected_instance_id():
    with pytest.raises(PermissionError, match="expect-instance-id"):
        probe(SCRATCH, "s", write=True, client=FakeForge())
    with pytest.raises(PermissionError, match="not the expected"):
        probe(SCRATCH, "s", write=True, expect_instance_id="other", client=FakeForge())


def test_write_mode_writes_nothing_before_the_instance_is_confirmed():
    forge = FakeForge()
    with pytest.raises(PermissionError):
        probe(SCRATCH, "s", write=True, expect_instance_id="other", client=forge)

    assert {method for method, _, _ in forge.calls} == {"GET"}


def test_health_judges_only_the_forge_plugins():
    other = health_check(Response(503, {"status": "degraded", "failed_plugins": ["ting"]}))
    forge = health_check(Response(503, {"status": "degraded", "failed_plugins": ["volundr"]}))
    blank = health_check(Response(502, "bad gateway"))

    assert other.ok is True and "ting" in other.detail
    assert forge.ok is False
    assert blank.ok is False


def _two(**m6):
    thor = probe("http://a", "thor", write=False, client=FakeForge(failed_plugins=["ting"]))
    other = probe(
        "http://b", "m6", write=False, client=FakeForge(instance_id="inst-b", host="m6", **m6)
    )
    return thor, other


def test_compare_is_on_par_only_when_every_host_passes_with_the_same_status():
    on_par, lines = compare(_two())
    assert on_par is True, lines  # /health 503 (ting) vs 200 is not a Forge difference

    thor, _ = _two()
    old = probe("http://c", "old", write=False, client=FakeForge(instance_id="c", current=False))
    on_par, lines = compare([thor, old])
    assert on_par is False
    assert any("FAIL" in line and "instance" in line for line in lines)


def test_compare_diffs_every_forge_route_not_only_the_required_ones():
    image = ("GET", f"{FORGE}/notifications/{{notification_id}}/image")
    thor, m6 = _two()
    thor_with_image = probe(
        "http://a", "thor", write=False, client=FakeForge(extra_routes=(image,))
    )

    on_par, lines = compare([thor_with_image, m6])
    assert on_par is False
    assert any("notifications/{notification_id}/image" in line for line in lines)
    assert compare([thor_with_image, m6], excluded=[" ".join(image)])[0] is True
    assert compare([thor, m6])[0] is True


def test_compare_needs_distinct_instances():
    thor, _ = _two()
    twin = probe("http://b", "twin", write=False, client=FakeForge())

    on_par, lines = compare([thor, twin])
    assert on_par is False
    assert any("not distinct" in line for line in lines)


def test_compare_flags_a_check_one_host_did_not_run():
    read = probe("http://a", "thor", write=False, client=FakeForge())
    write = _write(FakeForge(instance_id="inst-b", host="m6"))

    on_par, lines = compare([read, write])
    assert on_par is False
    assert any("not run" in line for line in lines)


def test_compare_flags_the_same_check_with_different_statuses():
    a = {"label": "a", "checks": [{"name": "x", "ok": True, "status": 200, "detail": ""}]}
    b = {"label": "b", "checks": [{"name": "x", "ok": True, "status": 201, "detail": ""}]}

    assert compare([a, b])[0] is False


def test_compare_refuses_a_single_report():
    with pytest.raises(ValueError):
        compare([probe("http://a", "thor", write=False, client=FakeForge())])


def test_an_openapi_report_compares_routes_without_calling_a_host():
    recorded = _openapi(drop={("GET", f"{FORGE}/instance")})
    report = openapi_report(recorded, "m6-live", "openapi-m6.json")

    assert report["mode"] == "openapi"
    assert _check(report, f"route GET {FORGE}/instance")["ok"] is False
    assert "GET /health" not in report["forge_routes"]
    thor, _ = _two()
    on_par, lines = compare([thor, report])
    assert on_par is False
    assert any(f"served route GET {FORGE}/instance" in line for line in lines)


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


def test_the_client_ignores_proxy_settings(server, monkeypatch):
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")

    assert Client(server, write=False, sleep=lambda _: None).request("GET", "/h").status == 200


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


def test_main_probe_openapi_and_compare_round_trip(tmp_path, monkeypatch, capsys):
    forges = iter([FakeForge(), FakeForge(instance_id="inst-b", host="m6")])
    monkeypatch.setattr(fpp, "Client", lambda base, write: next(forges))
    a, b, o = tmp_path / "a.json", tmp_path / "b.json", tmp_path / "o.json"
    recorded = tmp_path / "openapi.json"
    recorded.write_text(json.dumps(_openapi()))

    assert fpp.main(["probe", "--base", "http://a", "--label", "a", "--out", str(a)]) == 0
    assert fpp.main(["probe", "--base", "http://b", "--label", "b", "--out", str(b)]) == 0
    assert fpp.main(["openapi", str(recorded), "--label", "o", "--out", str(o)]) == 0
    assert fpp.main(["compare", str(a), str(b), str(o)]) == 0
    assert capsys.readouterr().out.strip().endswith("ON PAR")
    with pytest.raises(SystemExit):
        fpp.main(["compare", str(a)])


def test_main_prints_the_report_and_fails_an_old_host(monkeypatch, capsys):
    monkeypatch.setattr(fpp, "Client", lambda base, write: FakeForge(current=False))

    assert fpp.main(["probe", "--base", "http://m6", "--label", "m6"]) == 1
    assert json.loads(capsys.readouterr().out)["on_par_ready"] is False
