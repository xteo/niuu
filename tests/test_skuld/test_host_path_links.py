"""LEX-2110: a link a session wrote resolves to a file on the session's own host.

Unit cases for the decoder and the refusal rules (``skuld.host_path_links``), then the
broker route ``POST /api/files/resolve`` end to end: the record it answers, the file the
presented-file route then serves, and the rule that a link never becomes a document.
All fixtures are synthetic files under ``tmp_path``.
"""

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from skuld import host_path_links
from skuld.host_path_links import (
    LINK_CAPABILITIES,
    LinkPathError,
    LinkRoots,
    check_path,
    decode_link_path,
    display_path,
    link_file_id,
    open_link,
    resolve_link,
    secret_reason,
)

HOME = "/home/agent"
WORKSPACE = "/home/agent/ws"


def _decode(raw: str) -> list[str]:
    return decode_link_path(raw, home=HOME, workspace=WORKSPACE)


# --- decoding ---------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/home/agent/notes/a.md", "/home/agent/notes/a.md"),
        ("~/notes/a.md", "/home/agent/notes/a.md"),
        ("~", "/home/agent"),
        ("~/", "/home/agent"),
        ("$HOME/notes/a.md", "/home/agent/notes/a.md"),
        ("${HOME}/notes/a.md", "/home/agent/notes/a.md"),
        ("src/app.py", "/home/agent/ws/src/app.py"),
        ("./src/app.py", "/home/agent/ws/src/app.py"),
        ("file:///home/agent/notes/a.md", "/home/agent/notes/a.md"),
        ("file://localhost/home/agent/notes/a.md", "/home/agent/notes/a.md"),
        ("FILE:///home/agent/notes/a.md", "/home/agent/notes/a.md"),
        ("file:///home/agent/my%20notes/a%2Bb.md", "/home/agent/my notes/a+b.md"),
        ("src/app.py:12", "/home/agent/ws/src/app.py"),
        ("src/app.py:12:4", "/home/agent/ws/src/app.py"),
        ("src/app.py#L12", "/home/agent/ws/src/app.py"),
        ("src/app.py#L12-L20", "/home/agent/ws/src/app.py"),
        ("src/app.py#L12-20", "/home/agent/ws/src/app.py"),
        ("file:///home/agent/ws/src/app.py#L3", "/home/agent/ws/src/app.py"),
        ("`~/notes/a.md`", "/home/agent/notes/a.md"),
        ('"~/notes/a.md"', "/home/agent/notes/a.md"),
        ("'~/notes/a.md'", "/home/agent/notes/a.md"),
        ("(~/notes/a.md)", "/home/agent/notes/a.md"),
        ("[~/notes/a.md]", "/home/agent/notes/a.md"),
        ("<~/notes/a.md>", "/home/agent/notes/a.md"),
        ("~/notes/a.md.", "/home/agent/notes/a.md"),
        ("~/notes/a.md,", "/home/agent/notes/a.md"),
        ("~/notes/a.md).", "/home/agent/notes/a.md"),
        ("`~/notes/a.md`.", "/home/agent/notes/a.md"),
        ("(`src/app.py:12`)", "/home/agent/ws/src/app.py"),
        ("  ~/notes/a.md  ", "/home/agent/notes/a.md"),
        ("~/notes/report (1).md", "/home/agent/notes/report (1).md"),
        ("/home/agent/x/../notes/a.md", "/home/agent/notes/a.md"),
    ],
)
def test_decode_cases(raw, expected):
    assert _decode(raw)[-1] == expected


def test_a_plain_path_with_percent_escapes_tries_the_literal_spelling_first():
    assert _decode("~/a%20b.md") == ["/home/agent/a%20b.md", "/home/agent/a b.md"]


@pytest.mark.parametrize(
    "raw",
    [
        "",
        "   ",
        "``",
        "~other/x.md",
        "file://otherhost/etc/x",
        "a\0b",
        "file:///a%00b",
        "file://[bad",
    ],
)
def test_undecodable_paths_are_422(raw):
    with pytest.raises(LinkPathError) as err:
        _decode(raw)
    assert err.value.status == 422


# --- secret-shaped paths ----------------------------------------------------


@pytest.mark.parametrize(
    "path",
    [
        "/home/agent/.ssh/id_ed25519",
        "/home/agent/.ssh/config",
        "/home/agent/.SSH/known_hosts",
        "/home/agent/.gnupg/pubring.kbx",
        "/home/agent/ws/.env",
        "/home/agent/ws/.env.local",
        "/home/agent/ws/.envrc",
        "/home/agent/.config/gcloud/credentials.db",
        "/home/agent/.config/x/y/credentials",
        "/home/agent/.aws/config",
        "/home/agent/.claude/.credentials.json",
        "/home/agent/.codex/auth.json",
        "/home/agent/.netrc",
        "/home/agent/.pgpass",
        "/home/agent/certs/server.pem",
        "/home/agent/certs/server.key",
        "/home/agent/backup/id_rsa.bak",
        "/Users/agent/Library/Keychains/login.keychain-db",
        "/home/agent/.kube/config",
    ],
)
def test_secret_shaped_paths_are_named(path):
    assert secret_reason(path) is not None


@pytest.mark.parametrize(
    "path",
    [
        "/home/agent/agent/health/research.md",
        "/home/agent/ws/src/environment.py",
        "/home/agent/ws/docs/keys.md",
        "/home/agent/ws/auth.json",
    ],
)
def test_ordinary_paths_are_not_secret(path):
    assert secret_reason(path) is None


# --- resolving on a real filesystem -----------------------------------------


@pytest.fixture
def roots(tmp_path):
    home = tmp_path / "home"
    workspace = home / "ws"
    (home / "agent" / "health").mkdir(parents=True)
    workspace.mkdir()
    return home, workspace


def _roots(home, workspace, max_bytes=1 << 20) -> LinkRoots:
    return LinkRoots(home=str(home), workspace=str(workspace), host="testhost", max_bytes=max_bytes)


def _resolve(raw, home, workspace, max_bytes=1 << 20):
    return resolve_link(raw, _roots(home, workspace, max_bytes))


def test_resolve_a_home_file_by_tilde(roots):
    home, workspace = roots
    note = home / "agent" / "health" / "note.md"
    note.write_text("synthetic note")
    link = _resolve("~/agent/health/note.md", home, workspace)
    assert link.path == note.resolve()
    assert link.size == len("synthetic note")


def test_resolve_a_workspace_relative_file(roots):
    home, workspace = roots
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("print(1)\n")
    assert _resolve("src/app.py:1", home, workspace).path == (workspace / "src/app.py").resolve()


def test_the_literal_percent_spelling_wins_when_it_exists(roots):
    home, workspace = roots
    (home / "a%20b.md").write_text("literal")
    (home / "a b.md").write_text("decoded")
    assert _resolve("~/a%20b.md", home, workspace).path.name == "a%20b.md"
    (home / "a%20b.md").unlink()
    assert _resolve("~/a%20b.md", home, workspace).path.name == "a b.md"


def test_missing_file_is_404_naming_host_and_resolved_path(roots):
    home, workspace = roots
    with pytest.raises(LinkPathError) as err:
        _resolve("~/agent/missing.md", home, workspace)
    assert err.value.status == 404
    assert err.value.detail == f"no such file on testhost: {home.resolve()}/agent/missing.md"


def test_outside_the_roots_is_403(roots, tmp_path):
    home, workspace = roots
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    for raw in (str(outside), "/etc/hostname", "~/../outside.txt", "../../outside.txt"):
        with pytest.raises(LinkPathError) as err:
            _resolve(raw, home, workspace)
        assert err.value.status == 403, raw
        assert "is outside the session roots" in err.value.detail


def test_a_symlink_leaving_the_roots_is_403(roots, tmp_path):
    home, workspace = roots
    outside = tmp_path / "outside.txt"
    outside.write_text("x")
    (home / "escape.md").symlink_to(outside)
    (home / "escape-dir").symlink_to(tmp_path)
    for raw in ("~/escape.md", "~/escape-dir/outside.txt"):
        with pytest.raises(LinkPathError) as err:
            _resolve(raw, home, workspace)
        assert err.value.status == 403


def test_a_symlink_inside_the_roots_resolves_to_its_target(roots):
    home, workspace = roots
    target = home / "agent" / "real.md"
    target.write_text("real")
    (workspace / "alias.md").symlink_to(target)
    assert _resolve("alias.md", home, workspace).path == target.resolve()


def test_a_symlink_to_a_secret_is_403(roots):
    home, workspace = roots
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_text("synthetic")
    (home / "innocent.md").symlink_to(home / ".ssh" / "id_ed25519")
    with pytest.raises(LinkPathError) as err:
        _resolve("~/innocent.md", home, workspace)
    assert err.value.status == 403


def test_secret_shaped_paths_are_403_even_when_missing(roots):
    home, workspace = roots
    for raw in ("~/.ssh/id_ed25519", "~/ws/.env", "~/.config/gh/credentials.json"):
        with pytest.raises(LinkPathError) as err:
            _resolve(raw, home, workspace)
        assert err.value.status == 403, raw


def test_a_directory_is_403(roots):
    home, workspace = roots
    with pytest.raises(LinkPathError) as err:
        _resolve("~/agent", home, workspace)
    assert err.value.status == 403
    assert "directory" in err.value.detail


def test_a_device_or_fifo_is_403(roots):
    home, workspace = roots
    os.mkfifo(home / "pipe")
    with pytest.raises(LinkPathError) as err:
        _resolve("~/pipe", home, workspace)
    assert err.value.status == 403
    assert "not a regular file" in err.value.detail


def test_a_file_over_the_size_cap_is_413(roots):
    home, workspace = roots
    (home / "big.bin").write_bytes(b"x" * 100)
    with pytest.raises(LinkPathError) as err:
        _resolve("~/big.bin", home, workspace, max_bytes=8)
    assert err.value.status == 413
    assert "max_presented_file_bytes=8" in err.value.detail


def test_file_id_is_stable_for_the_same_version_and_changes_with_it(roots):
    home, workspace = roots
    note = home / "note.md"
    note.write_text("v1")
    key = b"k" * 32
    first = link_file_id(key, "s1", _resolve("~/note.md", home, workspace))
    assert first == link_file_id(key, "s1", _resolve(str(note), home, workspace))
    assert first.startswith("pf_") and len(first) == 35
    assert first != link_file_id(key, "s2", _resolve("~/note.md", home, workspace))
    # Keyed: another broker (another secret) mints another id, so ids can't be computed.
    assert first != link_file_id(b"x" * 32, "s1", _resolve("~/note.md", home, workspace))
    note.write_text("v2 longer")
    assert first != link_file_id(key, "s1", _resolve("~/note.md", home, workspace))


def test_display_path_shortens_home():
    assert display_path(Path("/home/agent/a.md"), "/home/agent") == "~/a.md"
    assert display_path(Path("/srv/a.md"), "/home/agent") == "/srv/a.md"


# --- the broker route -------------------------------------------------------


@pytest.fixture
def client(tmp_path, monkeypatch, module_broker_runtime):
    from skuld import broker as bmod
    from skuld import broker_api as api_mod

    home = tmp_path / "home"
    workspace = home / "ws"
    workspace.mkdir(parents=True)
    staging = tmp_path / "staging"
    monkeypatch.setattr(api_mod, "_presented_staging_dir", lambda: staging)
    monkeypatch.setattr(bmod.broker, "workspace_dir", str(workspace))
    monkeypatch.setattr(bmod.broker._settings, "home_dir", str(home))
    logged: list = []
    broadcast: list = []

    async def _fake_broadcast(frame):
        broadcast.append(frame)

    monkeypatch.setattr(bmod.broker, "_enqueue_event_log", logged.append)
    monkeypatch.setattr(bmod.broker, "_save_conversation_history", lambda: None)
    monkeypatch.setattr(bmod.broker._channels, "broadcast", _fake_broadcast)
    monkeypatch.setattr(bmod.broker, "_conversation_turns", [])
    api_mod._presented_registry.clear()
    api_mod._clear_link_registry()
    c = TestClient(
        bmod.app,
        headers={
            "authorization": module_broker_runtime.authorization,
            "x-niuu-room-role": "owner",
        },
    )
    c.home = home  # type: ignore[attr-defined]
    c.workspace = workspace  # type: ignore[attr-defined]
    c.staging = staging  # type: ignore[attr-defined]
    c.logged = logged  # type: ignore[attr-defined]
    c.broadcast = broadcast  # type: ignore[attr-defined]
    return c


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def test_resolve_then_open_serves_the_same_bytes(client):
    data = b"# synthetic research note\n"
    note = _write(client.home / "agent" / "health" / "research.md", data)
    r = client.post("/api/files/resolve", json={"path": "~/agent/health/research.md"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["name"] == "research.md"
    assert body["size"] == len(data)
    assert body["mime"] == "text/markdown"
    assert body["path"] == str(note.resolve())
    assert body["display_path"] == "~/agent/health/research.md"
    assert body["source"] == "link"
    assert body["host"]
    d = client.get(f"/api/files/presented/{body['file_id']}")
    assert d.status_code == 200
    assert d.content == data
    assert d.headers["cache-control"] == "private, no-store"
    assert d.headers["x-content-type-options"] == "nosniff"
    assert d.headers["content-disposition"].startswith("inline")


def test_opening_a_link_never_adds_a_document(client):
    """No turn, no broadcast, no durable upload, nothing staged."""
    from skuld import broker as bmod

    _write(client.home / "a.md", b"x")
    fid = client.post("/api/files/resolve", json={"path": "~/a.md"}).json()["file_id"]
    assert client.get(f"/api/files/presented/{fid}").status_code == 200
    assert bmod.broker._conversation_turns == []
    assert client.logged == []
    assert client.broadcast == []
    assert not client.staging.exists()
    history = client.get("/api/conversation/history").json()
    assert history["turns"] == []


def test_same_path_same_version_same_file_id(client):
    note = _write(client.home / "a.md", b"one")
    first = client.post("/api/files/resolve", json={"path": "~/a.md"}).json()["file_id"]
    again = client.post("/api/files/resolve", json={"path": f"file://{note}"}).json()
    assert again["file_id"] == first


def test_a_file_changed_after_resolving_is_410_until_resolved_again(client):
    note = _write(client.home / "a.md", b"one")
    fid = client.post("/api/files/resolve", json={"path": "~/a.md"}).json()["file_id"]
    note.write_bytes(b"two, longer")
    assert client.get(f"/api/files/presented/{fid}").status_code == 410
    fresh = client.post("/api/files/resolve", json={"path": "~/a.md"}).json()["file_id"]
    assert fresh != fid
    assert client.get(f"/api/files/presented/{fresh}").content == b"two, longer"


def test_route_answers_403_404_413_422(client, tmp_path, monkeypatch):
    from skuld import broker as bmod

    _write(client.home / ".ssh" / "id_ed25519", b"synthetic")
    _write(tmp_path / "outside.txt", b"x")
    _write(client.home / "big.bin", b"x" * 100)

    secret = client.post("/api/files/resolve", json={"path": "~/.ssh/id_ed25519"})
    assert secret.status_code == 403
    outside = client.post("/api/files/resolve", json={"path": str(tmp_path / "outside.txt")})
    assert outside.status_code == 403
    assert outside.json()["detail"].endswith("outside.txt is outside the session roots")
    missing = client.post("/api/files/resolve", json={"path": "~/nope.md"})
    assert missing.status_code == 404
    assert missing.json()["detail"].startswith("no such file on ")
    assert missing.json()["detail"].endswith("/home/nope.md")
    monkeypatch.setattr(bmod.broker._settings, "max_presented_file_bytes", 8)
    assert client.post("/api/files/resolve", json={"path": "~/big.bin"}).status_code == 413
    assert client.post("/api/files/resolve", json={"path": "~other/x"}).status_code == 422
    assert client.post("/api/files/resolve", json={}).status_code == 422


def test_a_symlink_swapped_after_resolving_is_not_served(client, tmp_path):
    """The served file is checked again: a link turned into an escape is refused."""
    _write(tmp_path / "outside.txt", b"outside")
    note = _write(client.home / "a.md", b"inside")
    fid = client.post("/api/files/resolve", json={"path": "~/a.md"}).json()["file_id"]
    note.unlink()
    note.symlink_to(tmp_path / "outside.txt")
    assert client.get(f"/api/files/presented/{fid}").status_code == 403


def test_the_route_is_owner_only(client, module_broker_runtime):
    _write(client.home / "a.md", b"x")
    viewer = TestClient(
        client.app,
        headers={
            "authorization": module_broker_runtime.authorization,
            "x-niuu-room-role": "viewer",
        },
    )
    assert viewer.post("/api/files/resolve", json={"path": "~/a.md"}).status_code == 403


def test_capabilities_advertise_resolve_path(client, monkeypatch):
    from types import SimpleNamespace

    from niuu.ports.cli.transport import TransportCapabilities
    from skuld import broker as bmod

    monkeypatch.setattr(
        bmod.broker, "_transport", SimpleNamespace(capabilities=TransportCapabilities())
    )
    payload = client.get("/api/capabilities").json()
    assert payload["resolve_path"] is True
    assert payload["present_by_path"] is True
    assert LINK_CAPABILITIES == {"resolve_path": True, "present_by_path": True}


# --- review round 1 (LEX-2110 r1) -------------------------------------------

THOR_HOME = ("/home/agent",)


@pytest.mark.parametrize(
    "path",
    [
        "/home/agent/.lexi/secrets/openai",
        "/home/agent/.config/cloudflare-lulla.env",
        "/home/agent/ws/prod.env",
        "/home/agent/.config/gh/hosts.yml",
        "/home/agent/.docker/config.json",
        "/home/agent/.claude.json",
        "/home/agent/.gbrain/config.json",
        "/home/agent/.config/fleet-secrets/a",
        "/home/agent/.config/codex-accounts/a.json",
        "/home/agent/.config/claude-nvidia/settings.json",
        "/home/agent/.local/share/keyrings/login.keyring",
        "/home/agent/.config/chromium/Default/Cookies",
        "/home/agent/.config/gcloud/application_default_credentials.json",
        "/home/agent/.bash_history",
        "/home/agent/.python_history",
        "/home/agent/Library/Cookies/Cookies.binarycookies",
        "/home/agent/Library/Application Support/Google/Chrome/Default/Cookies",
        "/home/agent/notes/github-token.txt",
        "/home/agent/notes/my-secret-plan.md",
        "/home/agent/.mozilla/firefox/x/logins.json",
        # NFKC + casefold: U+017F LATIN SMALL LETTER LONG S folds to "s".
        "/home/agent/.ſſh/id_x",
        "/home/agent/.SSH/known_hosts",
        "/home/agent/ws/.ENV",
    ],
)
def test_round1_secrets_are_refused(path):
    assert secret_reason(path, THOR_HOME) is not None, path


@pytest.mark.parametrize(
    "path",
    [
        "/home/agent/.lexi/flow/reviews/r1.md",
        "/home/agent/.lexi/flow/state.json",
        "/home/agent/agents/lexi-flow/brief.md",
        "/home/agent/repos/niuu/README.md",
        "/home/agent/tmp/report.pdf",
        "/home/agent/agent/health/research.md",
    ],
)
def test_round1_working_trees_stay_open(path):
    assert secret_reason(path, THOR_HOME) is None, path


def test_a_workspace_inside_a_denied_tree_still_works(tmp_path):
    home = tmp_path / "home"
    workspace = home / ".local" / "share" / "ws"
    workspace.mkdir(parents=True)
    _write(workspace / "a.md", b"ws")
    _write(home / ".local" / "share" / "other.md", b"x")
    assert _resolve("a.md", home, workspace).path.name == "a.md"
    with pytest.raises(LinkPathError) as err:
        _resolve("~/.local/share/other.md", home, workspace)
    assert err.value.status == 403


def test_a_hard_link_is_403(roots):
    home, workspace = roots
    secret = _write(home / ".ssh" / "id_ed25519", b"synthetic")
    os.link(secret, workspace / "notes.txt")
    with pytest.raises(LinkPathError) as err:
        _resolve("notes.txt", home, workspace)
    assert err.value.status == 403
    assert "hard links" in err.value.detail


def test_a_canonical_path_under_a_symlinked_root_opens(tmp_path):
    """r1 P2a: ~/ws -> /mnt/repo resolves to the canonical path, which must open too."""
    home = tmp_path / "home"
    home.mkdir()
    repo = tmp_path / "mnt" / "repo"
    _write(repo / "a.md", b"repo file")
    (home / "ws").symlink_to(repo)
    roots = _roots(home, home / "ws")
    link = resolve_link("a.md", roots)
    assert link.path == (repo / "a.md").resolve()
    fd = open_link(link, roots)
    try:
        assert os.read(fd, 100) == b"repo file"
    finally:
        os.close(fd)


def test_the_stored_path_is_judged_literally(roots):
    """r1 P2b: ``report%21`` decodes to ``report!``; serving must not strip the ``!``."""
    home, workspace = roots
    _write(home / "report!", b"bang")
    link = _resolve(f"file://{home}/report%21", home, workspace)
    assert link.path.name == "report!"
    fd = open_link(link, _roots(home, workspace))
    os.close(fd)


def test_a_file_swapped_for_a_symlink_between_check_and_open_is_not_served(
    roots, tmp_path, monkeypatch
):
    """r1 P1/M3: the swap lands after the path check, before the open."""
    home, workspace = roots
    outside = _write(tmp_path / "outside.txt", b"outside bytes")
    note = _write(home / "a.md", b"inside")
    link = _resolve("~/a.md", home, workspace)
    real_check = host_path_links.check_path

    def check_then_swap(path, roots_):
        result = real_check(path, roots_)
        note.unlink()
        note.symlink_to(outside)
        return result

    monkeypatch.setattr(host_path_links, "check_path", check_then_swap)
    with pytest.raises(LinkPathError) as err:
        open_link(link, _roots(home, workspace))
    assert err.value.status == 410


def test_a_file_replaced_by_another_inode_is_410(roots, tmp_path, monkeypatch):
    home, workspace = roots
    note = _write(home / "a.md", b"same")
    link = _resolve("~/a.md", home, workspace)
    replacement = _write(home / "b.md", b"same")
    os.replace(replacement, note)
    with pytest.raises(LinkPathError) as err:
        open_link(link, _roots(home, workspace))
    assert err.value.status == 410


def test_the_descriptor_path_is_judged_too(roots, monkeypatch):
    """Whatever the kernel says the open file is must pass the rules as well."""
    home, workspace = roots
    _write(home / "a.md", b"x")
    link = _resolve("~/a.md", home, workspace)
    monkeypatch.setattr(host_path_links, "_descriptor_path", lambda fd: "/etc/hostname")
    with pytest.raises(LinkPathError) as err:
        open_link(link, _roots(home, workspace))
    assert err.value.status == 403


def test_check_path_does_not_decode(roots):
    home, workspace = roots
    _write(home / "a.md", b"x")
    with pytest.raises(LinkPathError) as err:
        check_path(f"`{home}/a.md`.", _roots(home, workspace))
    assert err.value.status in (403, 404)


def test_a_viewer_cannot_open_a_linked_file(client, module_broker_runtime):
    _write(client.home / "a.md", b"owner only")
    fid = client.post("/api/files/resolve", json={"path": "~/a.md"}).json()["file_id"]
    viewer = TestClient(
        client.app,
        headers={
            "authorization": module_broker_runtime.authorization,
            "x-niuu-room-role": "viewer",
        },
    )
    refused = viewer.get(f"/api/files/presented/{fid}")
    assert refused.status_code == 403
    assert "owner" in refused.json()["detail"]
    assert client.get(f"/api/files/presented/{fid}").content == b"owner only"


def test_one_id_per_path(client):
    from skuld import broker_api as api_mod

    note = _write(client.home / "a.md", b"one")
    first = client.post("/api/files/resolve", json={"path": "~/a.md"}).json()["file_id"]
    note.write_bytes(b"two, longer")
    second = client.post("/api/files/resolve", json={"path": "~/a.md"}).json()["file_id"]
    assert second != first
    assert first not in api_mod._linked_registry
    assert list(api_mod._linked_ids_by_path.values()) == [second]
    assert client.get(f"/api/files/presented/{first}").status_code == 404


def test_the_route_serves_a_canonical_path_under_a_symlinked_workspace(
    client, tmp_path, monkeypatch
):
    from skuld import broker as bmod

    repo = tmp_path / "mnt" / "repo"
    _write(repo / "a.md", b"via symlinked root")
    (client.home / "link-ws").symlink_to(repo)
    monkeypatch.setattr(bmod.broker, "workspace_dir", str(client.home / "link-ws"))
    fid = client.post("/api/files/resolve", json={"path": "a.md"}).json()["file_id"]
    assert client.get(f"/api/files/presented/{fid}").content == b"via symlinked root"


def test_a_malformed_file_url_is_422_at_the_route(client):
    assert client.post("/api/files/resolve", json={"path": "file://[bad"}).status_code == 422


# --- review round 2 (LEX-2110 r2) -------------------------------------------

R2_HOME = "/home/agent"
R2_WS = "/home/agent/.niuu/workspaces/s1"


def _r2(path, home=R2_HOME, workspace=R2_WS):
    roots = LinkRoots(home=home, workspace=workspace, host="h", max_bytes=1)
    return secret_reason(
        path, roots.homes(), roots.workspaces(), workspace_exempt=roots.workspace_exempt()
    )


@pytest.mark.parametrize(
    "path",
    [
        # r2 item 2: every dot-entry directly under ~ outside the allow-list.
        "/home/agent/.elevenlabs_key",
        "/home/agent/.github-pat",
        "/home/agent/.linear-api-key",
        "/home/agent/.codex-work/auth.json",
        "/home/agent/.codex-work/history.jsonl",
        "/home/agent/.gbrain-owner-1.json",
        "/home/agent/.gstack/chromium-profile/Default/Cookies",
        "/home/agent/.openclaw/agents/x.json",
        "/home/agent/keys/AuthKey_ABC.p8",
        "/home/agent/.claude/settings.json",
        "/home/agent/.claude/projects/-home-agent-x/abc.jsonl",
        "/home/agent/.lexi/state.json",
        # Every rule still applies inside the allow-list.
        "/home/agent/.lexi/flow/secrets/x",
        "/home/agent/.lexi/secrets/x",
        "/home/agent/.lexi/flow/.env",
        "/home/agent/.lexi/config/api.key",
        "/home/agent/Library/Cookies/x",
    ],
)
def test_r2_home_dot_entries_are_refused(path):
    assert _r2(path) is not None, path


@pytest.mark.parametrize(
    "path",
    [
        "/home/agent/.lexi/flow/reviews/r2.md",
        "/home/agent/.lexi/config/markdown-autolinks.json",
        "/home/agent/.lexi/notes/a.md",
        "/home/agent/.claude/CLAUDE.md",
        "/home/agent/.claude/skills/present-file/SKILL.md",
        "/home/agent/.claude/projects/-home-agent-repos-x/memory/MEMORY.md",
        "/home/agent/agents/lexi-flow/brief.md",
        "/home/agent/repos/niuu/README.md",
        "/home/agent/tmp/report.pdf",
        # Files in an exempt workspace (below ~/.niuu) skip the home-area rule.
        "/home/agent/.niuu/workspaces/s1/notes.txt",
    ],
)
def test_r2_allow_listed_and_working_trees_stay_open(path):
    assert _r2(path) is None, path


def test_r2_only_components_below_the_matched_root_are_judged():
    """r2 item 3: a root path holding a keyword does not refuse everything under it."""
    assert _r2("/Users/secretary/notes/a.txt", home="/Users/secretary") is None
    ws = "/home/agent/repos/worktrees/niuu-session-token-fix"
    assert _r2(f"{ws}/README.txt", workspace=ws) is None
    assert _r2(f"{ws}/tokens.txt", workspace=ws) is not None


@pytest.mark.parametrize(
    ("path", "refused"),
    [
        # Code names skip the substring rule; inside the workspace so do the
        # non-dot directories above them.
        (f"{R2_WS}/src/niuu/token_scope.py", False),
        (f"{R2_WS}/src/token_store/__init__.py", False),
        (f"{R2_WS}/web/src/secretBox.tsx", False),
        ("/home/agent/repos/x/forge_session_token.py", False),
        ("/home/agent/repos/token-tools/a.py", True),  # outside the workspace: dirs judged
        (f"{R2_WS}/src/token_store/notes.txt", True),  # not code: dirs judged
        (f"{R2_WS}/.token/a.py", True),  # dot components always
        (f"{R2_WS}/src/.secret.py", True),  # a dot name never skips
        # .md and .rst skip only inside the workspace.
        (f"{R2_WS}/docs/secret-handling.md", False),
        (f"{R2_WS}/docs/token-flow.rst", False),
        ("/home/agent/notes/secret-plan.md", True),
        # Data, config and extensionless names keep it.
        (f"{R2_WS}/config/tokens.json", True),
        (f"{R2_WS}/token", True),
        (f"{R2_WS}/secrets.yaml", True),
        # Exact, prefix and suffix rules apply to code names too.
        (f"{R2_WS}/src/id_rsa.py", True),
        (f"{R2_WS}/src/app.py.pem", True),
    ],
)
def test_r2_the_substring_rule_is_narrowed_for_code(path, refused):
    assert (_r2(path) is not None) is refused, path


def test_r2_a_workspace_at_a_denied_tree_is_not_exempt(tmp_path):
    """r2 item 5: a workspace of ~/.config (or a symlink to it) serves nothing in it."""
    home = tmp_path / "home"
    config = home / ".config"
    _write(config / "gh" / "hosts.yml", b"synthetic")
    with pytest.raises(LinkPathError) as err:
        _resolve("gh/hosts.yml", home, config)
    assert err.value.status == 403
    (home / "ws-link").symlink_to(config)
    with pytest.raises(LinkPathError) as err:
        _resolve("gh/hosts.yml", home, home / "ws-link")
    assert err.value.status == 403
    deeper = config / "ws"
    _write(deeper / "a.md", b"ok")
    assert _resolve("a.md", home, deeper).path.name == "a.md"
    assert not LinkRoots(str(home), str(home), "h", 1).workspace_exempt()


def _open_fds() -> int:
    return len(os.listdir("/proc/self/fd"))


async def test_r2_the_descriptor_closes_when_the_client_leaves_before_the_body(roots):
    """r2 item 6: the client is gone before the first chunk; the fd still closes."""
    from skuld.broker_api import _DescriptorResponse

    home, workspace = roots
    _write(home / "a.md", b"x" * 10)
    link = _resolve("~/a.md", home, workspace)
    before = _open_fds()
    fd = open_link(link, _roots(home, workspace))
    response = _DescriptorResponse(fd, link.size, media_type="text/markdown")

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        raise OSError("client went away")

    from starlette.requests import ClientDisconnect

    scope = {"type": "http", "asgi": {"spec_version": "2.4"}, "method": "GET", "headers": []}
    with pytest.raises(ClientDisconnect):
        await response(scope, receive, send)
    assert _open_fds() == before
    with pytest.raises(OSError):
        os.fstat(fd)


async def test_r2_the_descriptor_closes_when_the_disconnect_cancels_the_stream(roots):
    from skuld.broker_api import _DescriptorResponse

    home, workspace = roots
    _write(home / "a.md", b"x" * 10)
    link = _resolve("~/a.md", home, workspace)
    before = _open_fds()
    fd = open_link(link, _roots(home, workspace))
    response = _DescriptorResponse(fd, link.size, media_type="text/markdown")
    sent: list = []

    async def receive():
        return {"type": "http.disconnect"}

    async def send(message):
        sent.append(message["type"])

    scope = {"type": "http", "asgi": {"spec_version": "2.0"}, "method": "GET", "headers": []}
    await response(scope, receive, send)
    assert _open_fds() == before


async def test_r2_at_most_link_size_bytes_are_read(tmp_path):
    """r2 item 7: never more than content-length, and a short file is loud."""
    from skuld.broker_api import LinkedFileShrankError, _read_descriptor

    path = _write(tmp_path / "f.bin", b"0123456789")
    fd = os.open(path, os.O_RDONLY)
    try:
        chunks = [c async for c in _read_descriptor(fd, 4)]
        assert b"".join(chunks) == b"0123"
        os.lseek(fd, 0, os.SEEK_SET)
        with pytest.raises(LinkedFileShrankError):
            _ = [c async for c in _read_descriptor(fd, 20)]
    finally:
        os.close(fd)


def test_r2_the_route_still_streams_the_whole_file(client):
    data = bytes(range(256)) * 1024
    _write(client.home / "big.bin", data)
    fid = client.post("/api/files/resolve", json={"path": "~/big.bin"}).json()["file_id"]
    response = client.get(f"/api/files/presented/{fid}")
    assert response.content == data
    assert response.headers["content-length"] == str(len(data))
