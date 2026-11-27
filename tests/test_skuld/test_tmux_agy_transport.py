"""TmuxAgyTransport with tmux faked and agy's screens, hooks and transcript recorded.

Screens are real agy 1.5.16 frames (fixtures/agy); hook payloads and transcript rows
have the shapes recorded on 24 Nov 2026 (docs/design/skuld-antigravity-gemini.md).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Callable
from pathlib import Path
from typing import Any

import pytest

from skuld.control_errors import ControlRecoveryError, HumanInputOnlyError
from skuld.delivery_errors import DeliveryNotAcceptedError, NativeControlPendingError
from skuld.transports import tmux_agy
from skuld.transports.session_tools import SessionTools
from skuld.transports.tmux_agy import (
    HOOK_CONFIG_KEY,
    AgyNotSignedInError,
    AgyVersionError,
    TmuxAgyTransport,
    _TmuxResult,
)

FIXTURES = Path(__file__).parent / "fixtures" / "agy"
CONV = "ff3cc161-6faf-49c1-950c-0a23cc8ee674"


def _frame(name: str) -> str:
    return (FIXTURES / f"{name}.txt").read_text(encoding="utf-8")


class FakeAgy(TmuxAgyTransport):
    """tmux answered in memory: the pane shows ``screen``; keys go to ``on_key``."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.commands: list[tuple[str, ...]] = []
        self.keys: list[str] = []
        self.pasted: list[str] = []
        self.session_exists = False
        self.binding = ""
        self.pane_dead = "0"
        self.screen = _frame("ready")
        self.on_key: Callable[[str], None] | None = None

    async def _check_version(self) -> None:
        return

    async def _run_tmux(self, *args: str, check: bool = True, env=None) -> _TmuxResult:
        self.commands.append(args)
        match args[0]:
            case "has-session":
                return _TmuxResult(0 if self.session_exists else 1)
            case "new-session":
                self.session_exists = True
                self.launch_env = env
                return _TmuxResult(0)
            case "kill-session":
                self.session_exists = False
                return _TmuxResult(0)
            case "show-options":
                return _TmuxResult(0, self.binding + "\n")
            case "set-option":
                if args[-2] == tmux_agy.HOOK_BINDING_OPTION:
                    self.binding = args[-1]
                return _TmuxResult(0)
            case "list-panes":
                return _TmuxResult(0, self.pane_dead + "\n")
            case "capture-pane":
                return _TmuxResult(0, self.screen)
            case "load-buffer":
                self.pasted.append(Path(args[-1]).read_text())
                return _TmuxResult(0)
            case "send-keys":
                self.keys.append(args[-1])
                if self.on_key:
                    self.on_key(args[-1])
                return _TmuxResult(0)
        return _TmuxResult(0)


@pytest.fixture(autouse=True)
def _tmux_on_path(monkeypatch):
    real_which = tmux_agy.shutil.which
    monkeypatch.setattr(
        tmux_agy.shutil,
        "which",
        lambda name: "/usr/bin/tmux" if name == "tmux" else real_which(name),
    )


def _config(tmp_path: Path, **overrides: Any) -> dict[str, Any]:
    return {
        "binary": "/opt/agy",
        "home": str(tmp_path / "home"),
        "expected_version": "1.5.16",
        "mode": "accept-edits",
        "socket_dir": str(tmp_path / "sock"),
        "poll_interval_s": 0.01,
        "ready_timeout_s": 0.5,
        "menu_render_wait_s": 0.3,
        "interrupt_confirm_timeout_s": 0.3,
        **overrides,
    }


def _tools(tmp_path: Path) -> SessionTools:
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    return SessionTools(
        runtime_dir=runtime,
        env={"FORGE_BROKER_TOKEN_FILE": str(runtime / "token")},
        instructions="FORGE TOOLS",
        loopback_authorization="Bearer s3cret",
    )


def _transport(tmp_path: Path, **kwargs: Any) -> FakeAgy:
    config = _config(tmp_path, **kwargs.pop("agy", {}))
    (tmp_path / "ws").mkdir(exist_ok=True)
    params: dict[str, Any] = {
        "workspace_dir": str(tmp_path / "ws"),
        "model": "gemini-4-neon",
        "reasoning_effort": "low",
        "session_id": "s1",
        "sdk_port": 8123,
        "session_tools": _tools(tmp_path),
        "agy": config,
    }
    params.update(kwargs)
    return FakeAgy(**params)


def _events(transport: TmuxAgyTransport) -> list[dict]:
    events: list[dict] = []

    async def collect(event: dict) -> None:
        if event.get("type") != "terminal_frame":
            events.append(event)

    transport.on_event(collect)
    return events


def _of(events: list[dict], kind: str) -> list[dict]:
    return [e for e in events if e.get("type") == kind]


async def _wait(predicate: Callable[[], Any], timeout: float = 2.0) -> Any:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if result := predicate():
            return result
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


class Transcript:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")

    def add(self, **row: Any) -> None:
        with self.path.open("a") as handle:
            handle.write(json.dumps({"status": "DONE", **row}) + "\n")


def _hook(transcript: Transcript, **extra: Any) -> dict[str, Any]:
    return {
        "conversationId": CONV,
        "transcriptPath": str(transcript.path),
        "workspacePaths": ["/ws"],
        "modelName": "gemini-4-neon-low",
        **extra,
    }


# ---------------------------------------------------------------- construction


@pytest.mark.parametrize(
    ("agy", "model", "message"),
    [
        ({"home": ""}, "gemini-4-neon", "agy.home is not set"),
        ({"expected_version": ""}, "gemini-4-neon", "agy.expected_version is not set"),
        ({"mode": "yolo"}, "gemini-4-neon", "agy.mode must be"),
        ({}, "claude-opus-5-5-nope", "is not an agy model"),
    ],
)
def test_a_misconfigured_session_refuses_to_build(tmp_path, agy, model, message) -> None:
    with pytest.raises(ValueError, match=message):
        _transport(tmp_path, agy=agy, model=model)


def test_launch_names_model_with_effort_mode_and_permissions(tmp_path) -> None:
    transport = _transport(tmp_path, skip_permissions=True, resume_session_id=CONV)

    assert transport._launch_argv() == [
        "/opt/agy",
        "--model",
        "gemini-4-neon-low",
        "--mode",
        "accept-edits",
        "--dangerously-skip-permissions",
        "--conversation",
        CONV,
    ]
    assert transport.session_id == CONV


def test_the_default_effort_is_the_models_own(tmp_path) -> None:
    transport = _transport(tmp_path, reasoning_effort="")

    assert transport.agy_model_id == "gemini-4-neon-high"


def test_capabilities_and_human_only_default(tmp_path) -> None:
    transport = _transport(tmp_path)

    caps = transport.capabilities
    assert caps.interrupt and caps.set_effort and caps.set_model and caps.native_controls
    assert caps.steering_mode == "native"
    assert caps.permission_requests
    assert transport.human_input_only is True


# ---------------------------------------------------------------- start


async def test_start_installs_hooks_launches_agy_and_waits_for_the_composer(tmp_path) -> None:
    transport = _transport(
        tmp_path, mcp_servers=[{"name": "forge", "command": "fm", "args": ["-x"]}]
    )
    home = Path(transport._config.home)
    hooks = home / ".gemini" / "config" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text(json.dumps({"mine": {"Stop": []}}))
    events = _events(transport)

    await transport.start()
    try:
        written = json.loads(hooks.read_text())
        assert written["mine"] == {"Stop": []}
        entry = written[HOOK_CONFIG_KEY]
        assert entry["PreToolUse"][0]["matcher"] == "*"
        assert entry["Stop"][0]["command"].endswith("agy-hook.py Stop")
        assert (home / ".gemini" / "skuld" / "agy-hook.py").stat().st_mode & 0o777 == 0o600
        mcp = json.loads((home / ".gemini" / "config" / "mcp_config.json").read_text())
        assert mcp["mcpServers"]["forge"]["args"][1].endswith("agy-mcp.py")
        env = transport.launch_env
        assert env["HOME"] == str(home)
        assert env["AGY_CLI_DISABLE_AUTO_UPDATE"] == "true"
        assert env["SKULD_AGY_HOOK_URL"] == "http://127.0.0.1:8123/api/agy/hooks"
        assert json.loads(env["SKULD_AGY_MCP_SERVERS"])["forge"]["command"] == "fm"
        assert _of(events, "system")[0]["subtype"] == "init"
        assert transport.is_alive
    finally:
        await transport.stop()
    assert not transport.is_alive


async def test_an_unchanged_home_is_not_rewritten(tmp_path) -> None:
    transport = _transport(tmp_path)
    transport._install_hooks()
    hooks = Path(transport._config.home) / ".gemini" / "config" / "hooks.json"
    before = hooks.stat().st_mtime_ns

    transport._install_hooks()

    assert hooks.stat().st_mtime_ns == before


def test_a_foreign_mcp_server_of_the_same_name_is_never_replaced(tmp_path) -> None:
    transport = _transport(tmp_path, mcp_servers=[{"name": "forge", "command": "fm"}])
    config = Path(transport._config.home) / ".gemini" / "config" / "mcp_config.json"
    config.parent.mkdir(parents=True)
    config.write_text(json.dumps({"mcpServers": {"forge": {"command": "mine", "args": []}}}))

    with pytest.raises(RuntimeError, match="that Skuld did not write"):
        transport._install_hooks()


async def test_an_http_mcp_server_is_bridged_without_its_token_in_the_shared_config(
    tmp_path,
) -> None:
    token = tmp_path / "linear-token"
    token.write_text("tok-123")
    linear = {
        "name": "linear",
        "type": "http",
        "url": "https://mcp.linear.app/mcp",
        "credential_file": str(token),
        "auth_header": "Authorization",
        "auth_prefix": "Bearer ",
    }
    transport = _transport(tmp_path, mcp_servers=[linear, {"name": "forge", "command": "fm"}])
    events = _events(transport)

    await transport.start()
    try:
        home = Path(transport._config.home)
        shared = (home / ".gemini" / "config" / "mcp_config.json").read_text()
        assert "mcp.linear.app" not in shared and "tok-123" not in shared
        assert json.loads(shared)["mcpServers"]["linear"]["args"][2] == "linear"
        described = json.loads(transport.launch_env["SKULD_AGY_MCP_SERVERS"])
        assert described["linear"] == {
            key: value for key, value in linear.items() if key not in {"name", "type"}
        }
        assert "tok-123" not in transport.launch_env["SKULD_AGY_MCP_SERVERS"]
        assert not [e for e in _of(events, "system") if e.get("subtype") == "agy_mcp_left_out"]
    finally:
        await transport.stop()


async def test_a_server_that_cannot_be_bridged_is_left_out_visibly_and_counted(tmp_path) -> None:
    servers = [
        {"name": "linear", "type": "http", "url": "https://l", "credential_file": "/nope/token"},
        {"name": "linear", "type": "http", "url": "https://l2"},
        {"name": "old", "type": "sse", "url": "https://sse"},
        {"name": "forge", "command": "fm"},
    ]
    transport = _transport(tmp_path, mcp_servers=servers)
    events = _events(transport)

    await transport.start()
    try:
        left_out = [e for e in _of(events, "system") if e.get("subtype") == "agy_mcp_left_out"]
        assert left_out == []  # at start no browser is a channel yet; it shows with the turn
        assert transport._counts["mcp_servers_left_out"] == 3
        transcript = Transcript(tmp_path / "brain" / CONV / "transcript.jsonl")
        await transport.handle_agy_hook("SessionStart", _hook(transcript))
        for _ in range(2):
            await transport.handle_agy_hook(
                "PreInvocation", _hook(transcript, invocationNum=0, lastUserInput="hi")
            )
        notices = [e for e in _of(events, "system") if e.get("subtype") == "agy_mcp_left_out"]
        assert len(notices) == 1
        line = notices[0]["content"]
        assert line.startswith("Gemini started without the linear MCP server")
        assert "access token is not on this host" in line
        assert "already has that name" in line and "legacy SSE" in line
        assert [item["name"] for item in notices[0]["metadata"]["left_out"]] == [
            "linear",
            "linear",
            "old",
        ]
        assert transport._counts["mcp_servers_left_out"] == 3
        assert transport._counts["mcp_left_out:linear"] == 2
        described = json.loads(transport.launch_env["SKULD_AGY_MCP_SERVERS"])
        assert list(described) == ["forge"]
        note = transport._session_start_answer()["injectSteps"][0]["systemMessage"]
        assert "not available to you: linear (" in note["systemMessage"]
    finally:
        await transport.stop()


@pytest.mark.parametrize(
    ("server", "environ", "reason"),
    [
        ({"name": "a"}, {}, "neither a command nor a URL"),
        ({"name": "a", "url": "https://a", "credential_env": "TOK"}, {}, "TOK is not set"),
        ({"name": "a", "url": "https://a", "bearer_token_env_var": "B"}, {}, "B is not set"),
        ({"name": "a", "url": "https://a", "bearer_token_env_var": "B"}, {"B": "x"}, ""),
        ({"name": "a", "url": "https://a", "credential_env": "TOK"}, {"TOK": "x"}, ""),
        (
            {
                "name": "a",
                "url": "https://a",
                "bearer_token_env_var": "B",
                "headers": {"authorization": "k"},
            },
            {},
            "",
        ),
    ],
)
def test_the_plan_names_why_a_server_is_left_out(server, environ, reason) -> None:
    plan = tmux_agy.plan_mcp_servers([server, {"no": "name"}], environ)

    if reason:
        assert plan.left_out[0][0] == "a" and reason in plan.left_out[0][1]
        assert plan.servers == {}
        return
    assert plan.left_out == ()
    assert plan.servers["a"]["url"] == "https://a"


def test_a_hooks_file_that_is_not_an_object_is_refused(tmp_path) -> None:
    transport = _transport(tmp_path)
    hooks = Path(transport._config.home) / ".gemini" / "config" / "hooks.json"
    hooks.parent.mkdir(parents=True)
    hooks.write_text("[]")

    with pytest.raises(RuntimeError, match="not a JSON object"):
        transport._install_hooks()


async def test_start_without_the_hook_endpoint_fails(tmp_path) -> None:
    transport = _transport(tmp_path, session_tools=None)

    with pytest.raises(RuntimeError, match="hook endpoint"):
        await transport.start()


async def test_a_signed_out_agy_fails_start_with_the_sign_in_command(tmp_path) -> None:
    transport = _transport(tmp_path, agy={"signed_out_grace_s": 0.1})
    transport.screen = _frame("signed_out")  # recorded from agy 1.5.16 with an empty home

    with pytest.raises(
        AgyNotSignedInError, match="stayed for agy.signed_out_grace_s=0.1.*HOME=.*home /opt/agy"
    ):
        await transport.start()


async def test_the_sign_in_menu_agy_shows_while_it_loads_a_login_is_waited_out(tmp_path) -> None:
    """agy draws its sign-in menu for a moment while it refreshes a stored login (27 Nov 2026)."""
    transport = _transport(tmp_path, agy={"signed_out_grace_s": 0.4})
    transport.screen = _frame("signed_out")
    seen: list[str] = []
    real_run = transport._run_tmux

    async def run(*args: str, check: bool = True, env=None):
        result = await real_run(*args, check=check, env=env)
        if args[0] == "capture-pane":
            seen.append(result.stdout)
            if len(seen) == 5:
                transport.screen = _frame("ready")
        return result

    transport._run_tmux = run  # type: ignore[method-assign]
    await transport.start()
    try:
        assert transport.is_alive
        assert sum(1 for screen in seen if "not signed in" in screen) >= 5
    finally:
        await transport.stop()


async def test_a_sign_in_menu_still_up_when_start_times_out_is_reported_as_signed_out(
    tmp_path,
) -> None:
    transport = _transport(tmp_path, agy={"signed_out_grace_s": 30, "ready_timeout_s": 0.1})
    transport.screen = _frame("signed_out")

    with pytest.raises(AgyNotSignedInError, match="not signed in"):
        await transport.start()


async def test_the_trust_menu_for_the_workspace_is_accepted(tmp_path) -> None:
    transport = _transport(tmp_path)
    transport.screen = _frame("trust").replace("/work/project", transport.workspace_dir)

    def accept(key: str) -> None:
        if key == "Enter":
            transport.screen = _frame("ready")

    transport.on_key = accept
    await transport.start()
    try:
        assert transport.keys[0] == "Enter"
    finally:
        await transport.stop()


async def test_a_trust_menu_for_another_folder_is_refused(tmp_path) -> None:
    transport = _transport(tmp_path)
    transport.screen = _frame("trust")

    with pytest.raises(DeliveryNotAcceptedError, match="not this session's workspace"):
        await transport.start()
    assert transport.keys == []


async def test_a_composer_that_never_appears_fails_start(tmp_path) -> None:
    transport = _transport(tmp_path)
    transport.screen = _frame("busy")

    with pytest.raises(DeliveryNotAcceptedError, match="agy.ready_timeout_s"):
        await transport.start()


async def test_an_agy_that_exits_during_start_fails_with_its_screen(tmp_path) -> None:
    transport = _transport(tmp_path)
    transport.screen = "Error: boom"
    transport.pane_dead = "1"

    with pytest.raises(RuntimeError, match="agy exited during start"):
        await transport.start()


async def test_a_session_left_by_another_broker_is_replaced(tmp_path) -> None:
    transport = _transport(tmp_path)
    transport.session_exists = True
    transport.binding = "someone-else"

    await transport.start()
    try:
        names = [c[0] for c in transport.commands]
        assert names.index("kill-session") < names.index("new-session")
    finally:
        await transport.stop()


async def test_a_session_this_broker_started_is_adopted(tmp_path) -> None:
    transport = _transport(tmp_path)
    transport.session_exists = True
    transport.binding = transport._hook_binding()

    await transport.start()
    try:
        assert "new-session" not in [c[0] for c in transport.commands]
    finally:
        await transport.stop()


async def test_a_first_prompt_from_an_agent_is_refused(tmp_path) -> None:
    sender = {"kind": "agent", "sub_kind": "session", "id": "x"}
    transport = _transport(tmp_path, initial_prompt="do it", initial_prompt_sender=sender)

    with pytest.raises(HumanInputOnlyError, match="typed by a person only"):
        await transport.start()
    assert transport.commands == []


async def test_a_first_prompt_from_the_user_is_typed(tmp_path) -> None:
    transport = _transport(tmp_path, initial_prompt="hello", initial_prompt_sender={"kind": "user"})

    await transport.start()
    try:
        assert transport.pasted == ["hello"]
        assert transport.keys == ["Enter"]
        assert transport.is_turn_active
    finally:
        await transport.stop()


async def test_the_version_must_be_the_tested_one(tmp_path, monkeypatch) -> None:
    transport = TmuxAgyTransport(
        workspace_dir=str(tmp_path), model="gemini-4-neon", agy=_config(tmp_path)
    )

    def run(*args: Any, **kwargs: Any) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(args, 0, stdout="1.5.17\n", stderr="")

    monkeypatch.setattr(tmux_agy.subprocess, "run", run)
    with pytest.raises(AgyVersionError, match="version '1.5.17'.*reads agy 1.5.16"):
        await transport._check_version()

    monkeypatch.setattr(
        tmux_agy.subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a, 0, stdout="1.5.16\n", stderr=""),
    )
    await transport._check_version()


# ---------------------------------------------------------------- a turn


async def test_a_turn_streams_text_tools_and_reports_agys_usage(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    transcript = Transcript(tmp_path / "brain" / CONV / "transcript.jsonl")
    await transport.start()
    try:
        await transport.send_message("Run uname -m", msg_id="m1", request_id="r1")
        answer = await transport.handle_agy_hook("SessionStart", _hook(transcript))
        assert "FORGE TOOLS" in answer["injectSteps"][0]["systemMessage"]["systemMessage"]

        transcript.add(step_index=0, type="USER_INPUT", content="Run uname -m")
        await transport.handle_agy_hook(
            "PreInvocation", _hook(transcript, invocationNum=0, lastUserInput="Run uname -m")
        )
        transcript.add(
            step_index=1,
            type="PLANNER_RESPONSE",
            model="gemini-4-neon-low",
            input_tokens=2164,
            cache_read_tokens=7534,
            output_tokens=102,
            thinking="Check the arch.",
            content="",
            tool_calls=[{"name": "run_command", "args": {"CommandLine": '"uname -m"'}}],
        )
        decision = await transport.handle_agy_hook(
            "PreToolUse",
            _hook(
                transcript,
                stepIdx=2,
                toolCall={"name": "run_command", "args": {"CommandLine": "uname -m"}},
            ),
        )
        assert decision == {"decision": "allow"}
        transcript.add(
            step_index=2,
            type="GENERIC",
            content="Created At: 2026-11-24T06:47:49Z\nCompleted At: 2026-11-24T06:48:00Z\n"
            "Command exited with code 0.\n\nCommand output:\naarch64",
        )
        await transport.handle_agy_hook(
            "PostToolUse", _hook(transcript, stepIdx=2, error="", toolCall={"name": "run_command"})
        )
        transcript.add(
            step_index=3,
            type="PLANNER_RESPONSE",
            model="gemini-4-neon-low",
            input_tokens=2237,
            cache_read_tokens=7649,
            output_tokens=37,
            content="aarch64",
        )
        stop = await transport.handle_agy_hook(
            "Stop", _hook(transcript, terminationReason="NO_TOOL_CALL", fullyIdle=True, error="")
        )
        assert stop == {"decision": "stop"}
    finally:
        await transport.stop()

    submitted = _of(events, "terminal_prompt_submitted")[0]
    assert (submitted["msg_id"], submitted["request_id"]) == ("m1", "r1")
    blocks = [b for e in _of(events, "assistant") for b in e["message"]["content"]]
    assert blocks[0] == {"type": "thinking", "thinking": "Check the arch."}
    assert blocks[1]["type"] == "tool_use" and blocks[1]["id"] == f"agy-{CONV}-2"
    assert blocks[2] == {"type": "text", "text": "aarch64"}
    tool_result = _of(events, "user")[0]["message"]["content"][0]
    assert tool_result["tool_use_id"] == f"agy-{CONV}-2"
    assert tool_result["content"] == "Command exited with code 0.\n\nCommand output:\naarch64"
    assert "is_error" not in tool_result
    result = _of(events, "result")[0]
    assert result["stop_reason"] == "end_turn" and result["is_error"] is False
    assert result["result"] == "aarch64"
    assert result["modelUsage"] == {
        "gemini-4-neon-low": {
            "inputTokens": 4401,
            "outputTokens": 139,
            "cacheReadInputTokens": 15183,
            "cacheCreationInputTokens": 0,
        }
    }
    assert result["sessionId"] == CONV


async def test_a_stop_with_background_work_keeps_the_turn_open(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    transcript = Transcript(tmp_path / "t.jsonl")
    transport._begin_turn("x")

    await transport.handle_agy_hook("Stop", _hook(transcript, fullyIdle=False))

    assert transport.is_turn_active
    assert _of(events, "system")[0]["subtype"] == "agy_background_work"


async def test_an_errored_stop_ends_the_turn_as_an_error(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    transcript = Transcript(tmp_path / "t.jsonl")
    transport._begin_turn("x")

    await transport.handle_agy_hook(
        "Stop", _hook(transcript, fullyIdle=True, terminationReason="ERROR", error="quota")
    )

    result = _of(events, "result")[0]
    assert result["is_error"] is True
    assert result["stop_reason"] == "error"
    assert result["metadata"]["error"] == "quota"


async def test_a_tool_whose_result_agy_never_recorded_is_reported_as_missing(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    transcript = Transcript(tmp_path / "t.jsonl")
    await transport.handle_agy_hook(
        "PreToolUse", _hook(transcript, stepIdx=4, toolCall={"name": "read_file", "args": {}})
    )
    await transport.handle_agy_hook(
        "PostToolUse", _hook(transcript, stepIdx=4, error="exit status 1")
    )

    await transport.handle_agy_hook(
        "Stop", _hook(transcript, fullyIdle=True, terminationReason="NO_TOOL_CALL")
    )

    block = _of(events, "user")[0]["message"]["content"][0]
    assert block["is_error"] is True
    assert block["content"] == "exit status 1"


async def test_subagent_hooks_get_agys_default_and_are_counted(tmp_path) -> None:
    transport = _transport(tmp_path)
    transcript = Transcript(tmp_path / "t.jsonl")
    transport._begin_turn("x")
    await transport.handle_agy_hook("PreInvocation", _hook(transcript, invocationNum=0))

    answer = await transport.handle_agy_hook(
        "PreToolUse", {**_hook(transcript, stepIdx=1), "conversationId": "child"}
    )

    assert answer == {"decision": "allow"}
    assert transport.list_native_controls()["counts"]["child_conversation_hooks"] == 1


async def test_a_new_conversation_between_turns_becomes_the_sessions(tmp_path) -> None:
    transport = _transport(tmp_path)
    transcript = Transcript(tmp_path / "t.jsonl")
    await transport.handle_agy_hook("SessionStart", _hook(transcript))

    await transport.handle_agy_hook("SessionStart", {**_hook(transcript), "conversationId": "new"})

    assert transport.session_id == "new"


async def test_a_hook_without_a_conversation_is_refused(tmp_path) -> None:
    transport = _transport(tmp_path)

    with pytest.raises(ValueError, match="no conversationId"):
        await transport.handle_agy_hook("Stop", {})


async def test_an_unknown_hook_event_is_refused(tmp_path) -> None:
    transport = _transport(tmp_path)

    with pytest.raises(ValueError, match="unknown agy hook event"):
        await transport.handle_agy_hook("Teleport", _hook(Transcript(tmp_path / "t.jsonl")))


async def test_a_resumed_conversation_does_not_replay_its_history(tmp_path) -> None:
    transport = _transport(tmp_path, resume_session_id=CONV)
    events = _events(transport)
    transcript = Transcript(tmp_path / "t.jsonl")
    transcript.add(step_index=1, type="PLANNER_RESPONSE", content="old answer", output_tokens=5)

    await transport.handle_agy_hook("PreInvocation", _hook(transcript, invocationNum=0))
    transcript.add(step_index=3, type="PLANNER_RESPONSE", content="new answer", output_tokens=7)
    await transport.handle_agy_hook(
        "Stop", _hook(transcript, fullyIdle=True, terminationReason="NO_TOOL_CALL")
    )

    texts = [
        b["text"] for e in _of(events, "assistant") for b in e["content"] if b["type"] == "text"
    ]
    assert texts == ["new answer"]
    assert _of(events, "result")[0]["modelUsage"]["gemini-4-neon-low"]["outputTokens"] == 7


async def test_an_unknown_transcript_row_is_counted(tmp_path) -> None:
    transport = _transport(tmp_path)
    transcript = Transcript(tmp_path / "t.jsonl")
    transcript.add(step_index=0, type="BROWSER_SCREENSHOT", content="x")

    await transport.handle_agy_hook("PostInvocation", _hook(transcript))

    assert (
        transport.list_native_controls()["counts"]["transcript_rows_unread:BROWSER_SCREENSHOT"] == 1
    )


# ---------------------------------------------------------------- questions and permissions


def _highlight(screen: str, label: str) -> str:
    lines = []
    for line in screen.splitlines():
        stripped = line.lstrip("> ").rstrip()
        if line.lstrip().startswith(("> ", "1.", "2.", "3.", "4.")) and ". " in stripped:
            marker = "> " if stripped.split(". ", 1)[1] == label else "  "
            lines.append(marker + stripped)
            continue
        lines.append(line)
    return "\n".join(lines)


async def test_a_question_is_carded_and_answered_on_its_menu(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    transcript = Transcript(tmp_path / "t.jsonl")
    await transport.start()
    try:
        questions = [
            {"is_multi_select": False, "options": ["Tea", "Coffee"], "question": "Tea or coffee?"}
        ]
        await transport.handle_agy_hook(
            "PreToolUse",
            _hook(
                transcript,
                stepIdx=5,
                toolCall={"name": "ask_question", "args": {"questions": questions}},
            ),
        )
        card = _of(events, "ask_user_question")[0]
        assert card["questions"][0]["options"] == [{"label": "Tea"}, {"label": "Coffee"}]
        assert card["metadata"]["control_kind"] == "question"

        with pytest.raises(NativeControlPendingError):
            await transport.send_message("hello?")

        transport.screen = _frame("question")

        def drive(key: str) -> None:
            if key == "Down":
                transport.screen = _highlight(_frame("question"), "Coffee")
            if key == "Enter":
                transport.screen = _frame("ready")

        transport.on_key = drive
        await transport.send_control(
            "ask_user_answer", request_id=card["request_id"], answers=[{"answer": "Coffee"}]
        )
    finally:
        await transport.stop()

    assert transport.keys == ["Down", "Enter"]
    resolved = _of(events, "ask_user_resolved")[0]
    assert resolved["decision"] == "Coffee" and resolved["accepted"] is True


async def test_a_write_in_answer_selects_write_in_and_types_it(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    transcript = Transcript(tmp_path / "t.jsonl")
    await transport.start()
    try:
        questions = [
            {"is_multi_select": False, "options": ["Tea", "Coffee"], "question": "Tea or coffee?"}
        ]
        await transport.handle_agy_hook(
            "PreToolUse",
            _hook(
                transcript,
                stepIdx=5,
                toolCall={"name": "ask_question", "args": {"questions": json.dumps(questions)}},
            ),
        )
        request_id = _of(events, "ask_user_question")[0]["request_id"]
        transport.screen = _frame("question")
        downs = 0

        def drive(key: str) -> None:
            nonlocal downs
            if key == "Down":
                downs += 1
                transport.screen = _highlight(
                    _frame("question"), ["Coffee", "Write-in..."][downs - 1]
                )
            if key == "Enter" and transport.pasted:
                transport.screen = _frame("ready")

        transport.on_key = drive
        await transport.send_control("ask_user_answer", request_id=request_id, answers=["Mate"])
    finally:
        await transport.stop()

    assert transport.keys == ["Down", "Down", "Enter", "Enter"]
    assert transport.pasted[-1] == "Mate"


async def test_a_highlight_that_does_not_move_sends_no_enter(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    transcript = Transcript(tmp_path / "t.jsonl")
    await transport.start()
    try:
        questions = [
            {"is_multi_select": False, "options": ["Tea", "Coffee"], "question": "Tea or coffee?"}
        ]
        await transport.handle_agy_hook(
            "PreToolUse",
            _hook(
                transcript,
                stepIdx=5,
                toolCall={"name": "ask_question", "args": {"questions": questions}},
            ),
        )
        transport.screen = _frame("question")
        with pytest.raises(ControlRecoveryError, match="highlight did not reach"):
            await transport.send_control(
                "ask_user_answer",
                request_id=_of(events, "ask_user_question")[0]["request_id"],
                answers=[{"answer": "Coffee"}],
            )
    finally:
        await transport.stop()

    assert "Enter" not in transport.keys


async def test_a_multi_select_question_is_answered_in_the_terminal(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    transcript = Transcript(tmp_path / "t.jsonl")
    questions = [{"is_multi_select": True, "options": ["A", "B"], "question": "Pick"}]
    await transport.handle_agy_hook(
        "PreToolUse",
        _hook(
            transcript,
            stepIdx=5,
            toolCall={"name": "ask_question", "args": {"questions": questions}},
        ),
    )

    with pytest.raises(ControlRecoveryError, match="answered in the terminal"):
        await transport.send_control(
            "ask_user_answer",
            request_id=_of(events, "ask_user_question")[0]["request_id"],
            answers=[{"answer": ["A"]}],
        )
    assert transport.keys == []


async def test_an_unknown_card_answer_is_refused(tmp_path) -> None:
    transport = _transport(tmp_path)

    with pytest.raises(ValueError, match="Unknown or already answered"):
        await transport.send_control("ask_user_answer", request_id="nope", answers=["x"])


async def test_a_permission_menu_is_carded_with_its_own_rows_and_denied(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    transcript = Transcript(tmp_path / "t.jsonl")
    await transport.start()
    try:
        transport._begin_turn("run it")
        await transport.handle_agy_hook(
            "PreToolUse",
            _hook(
                transcript,
                stepIdx=2,
                toolCall={"name": "run_command", "args": {"CommandLine": "uname -m"}},
            ),
        )
        transport.screen = _frame("permission")
        card = await _wait(lambda: next(iter(_of(events, "ask_user_question")), None))
        assert card["questions"][0]["question"] == "uname -m Run this command?"
        assert card["questions"][0]["options"][-1] == {"label": "No, cancel"}

        presses = 0

        def drive(key: str) -> None:
            nonlocal presses
            if key == "Down":
                presses += 1
                transport.screen = (
                    _highlight(_frame("permission"), "No, cancel")
                    if presses == 3
                    else transport.screen
                )
            if key == "Enter":
                transport.screen = _frame("busy")

        transport.on_key = drive
        await transport.send_control(
            "ask_user_answer", request_id=card["request_id"], answers=["deny"]
        )
    finally:
        await transport.stop()

    assert transport.keys[-4:] == ["Down", "Down", "Down", "Enter"]
    assert _of(events, "ask_user_resolved")[0]["decision"] == "deny"


async def test_a_permission_answered_in_the_terminal_closes_its_card(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    transcript = Transcript(tmp_path / "t.jsonl")
    await transport.start()
    try:
        transport._begin_turn("run it")
        await transport.handle_agy_hook(
            "PreToolUse", _hook(transcript, stepIdx=2, toolCall={"name": "run_command", "args": {}})
        )
        transport.screen = _frame("permission")
        await _wait(lambda: _of(events, "ask_user_question"))
        transport.screen = _frame("ready")
        resolved = await _wait(lambda: _of(events, "ask_user_resolved"))
    finally:
        await transport.stop()

    assert resolved[0]["decision"] == "resolved_in_terminal"


@pytest.mark.parametrize(
    ("chosen", "row"),
    [
        ("allow", "Yes, run command"),
        (
            "Allow & don't ask again",
            "Yes, and always allow in this conversation for commands that start with 'uname'",
        ),
        ("No, cancel", "No, cancel"),
    ],
)
def test_generic_permission_answers_map_onto_agys_rows(chosen, row) -> None:
    from skuld.transports import agy_screen

    menu = agy_screen.permission_menu(_frame("permission"))

    assert TmuxAgyTransport._permission_row(chosen, menu) == row


def test_an_answer_agy_does_not_offer_is_refused() -> None:
    from skuld.transports import agy_screen

    with pytest.raises(ValueError, match="not one of agy's options"):
        TmuxAgyTransport._permission_row("maybe", agy_screen.permission_menu(_frame("permission")))


async def test_an_operator_can_dismiss_a_card(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    transcript = Transcript(tmp_path / "t.jsonl")
    await transport.handle_agy_hook(
        "PreToolUse",
        _hook(transcript, stepIdx=5, toolCall={"name": "ask_question", "args": {"questions": []}}),
    )
    request_id = transport.list_native_controls()["controls"][0]["request_id"]

    await transport.dismiss_native_control(request_id, dismissed_by="op")

    assert _of(events, "ask_user_resolved")[0]["dismiss_reason"] == "dismissed_by_operator"
    with pytest.raises(ValueError):
        await transport.dismiss_native_control(request_id, dismissed_by="op")


# ---------------------------------------------------------------- interrupt, effort, caps


async def test_an_interrupt_closes_the_turn_once_agy_is_idle(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    await transport.start()
    try:
        await transport.send_message("count")
        transport.screen = _frame("busy")
        transport.on_key = lambda key: setattr(transport, "screen", _frame("ready"))

        await transport.interrupt()
    finally:
        await transport.stop()

    assert "Escape" in transport.keys
    result = _of(events, "result")[0]
    assert (result["stop_reason"], result["is_error"]) == ("interrupted", True)


async def test_an_interrupt_agy_does_not_confirm_keeps_the_turn_open(tmp_path) -> None:
    transport = _transport(tmp_path)
    await transport.start()
    try:
        await transport.send_message("count")
        transport.screen = _frame("busy")

        with pytest.raises(ControlRecoveryError, match="agy.interrupt_confirm_timeout_s"):
            await transport.interrupt()
        assert transport.is_turn_active
    finally:
        await transport.stop()


async def test_interrupting_with_no_turn_does_nothing(tmp_path) -> None:
    transport = _transport(tmp_path)

    await transport.interrupt()

    assert transport.keys == []


async def test_effort_restarts_agy_on_the_same_conversation(tmp_path) -> None:
    transport = _transport(tmp_path)
    await transport.start()
    transport._conversation_id = CONV
    try:
        transport.screen = _frame("ready")  # status line: medium
        await transport.send_control("set_effort", effort="medium")
        assert (await transport.get_effort())["current"] == "medium"
        launch = [c for c in transport.commands if c[0] == "new-session"][-1]
        assert launch[-7:] == (
            "/opt/agy",
            "--model",
            "gemini-4-neon-medium",
            "--mode",
            "accept-edits",
            "--conversation",
            CONV,
        )
    finally:
        await transport.stop()


async def test_effort_the_status_line_does_not_show_is_reported(tmp_path) -> None:
    transport = _transport(tmp_path, agy={"ready_timeout_s": 0.1})
    await transport.start()
    try:
        with pytest.raises(ControlRecoveryError, match="not effort high"):
            await transport.send_control("set_effort", effort="high")
    finally:
        await transport.stop()


async def test_effort_cannot_change_mid_turn_or_to_an_unknown_level(tmp_path) -> None:
    transport = _transport(tmp_path)
    transport._begin_turn("x")

    with pytest.raises(DeliveryNotAcceptedError, match="between turns"):
        await transport.send_control("set_effort", effort="medium")
    with pytest.raises(ValueError, match="Unsupported effort"):
        await transport.send_control("set_effort", effort="ultra")
    with pytest.raises(ValueError, match="not an agy model"):
        await transport.send_control("set_model", model="gpt-6-sol")


async def test_a_model_change_before_any_turn_is_kept_for_launch(tmp_path) -> None:
    transport = _transport(tmp_path)

    await transport.send_control("set_model", model="gemini-4-argon")

    assert transport.agy_model_id == "gemini-4-argon-low"
    assert transport.model == "gemini-4-argon"


async def test_a_turn_past_its_bound_ends_loudly_and_is_counted(tmp_path) -> None:
    transport = _transport(tmp_path, agy={"turn_max_seconds": 0.05})
    events = _events(transport)
    await transport.start()
    try:
        await transport.send_message("long")
        transport.screen = _frame("busy")
        result = await _wait(lambda: next(iter(_of(events, "result")), None))
    finally:
        await transport.stop()

    assert result["stop_reason"] == "turn_cap_reached" and result["is_error"] is True
    assert "agy.turn_max_seconds" in result["metadata"]["error"]
    assert transport.list_native_controls()["counts"]["cap_hits:agy.turn_max_seconds"] == 1


async def test_agy_exiting_mid_turn_ends_it(tmp_path) -> None:
    transport = _transport(tmp_path)
    events = _events(transport)
    await transport.start()
    try:
        await transport.send_message("work")
        transport.pane_dead = "1"
        result = await _wait(lambda: next(iter(_of(events, "result")), None))
    finally:
        await transport.stop()

    assert result["stop_reason"] == "agy_exited"


async def test_terminal_input_and_keys_reach_the_pane(tmp_path) -> None:
    transport = _transport(tmp_path)
    await transport.start()
    try:
        await transport.send_control("terminal_input", data="/help", enter=True)
        await transport.send_control("terminal_key", keys=["esc", "Up"])
        await transport.send_control("steer", content="also this", msg_id="m9")
    finally:
        await transport.stop()

    assert transport.pasted[:2] == ["/help", "also this"]
    assert transport.keys == ["Enter", "Escape", "Up", "Enter"]


async def test_an_unsupported_control_is_refused(tmp_path) -> None:
    transport = _transport(tmp_path)

    with pytest.raises(ValueError, match="do not support the 'rewind_files' control"):
        await transport.send_control("rewind_files")


async def test_a_menu_with_no_card_refuses_a_message(tmp_path) -> None:
    transport = _transport(tmp_path, skip_permissions=True)
    await transport.start()
    try:
        transport.screen = _frame("question")
        with pytest.raises(NativeControlPendingError, match="nothing was typed"):
            await transport.send_message("hi")
    finally:
        await transport.stop()
    assert transport.pasted == []
