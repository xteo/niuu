"""TmuxAgyTransport — the Antigravity CLI (``agy``) driven in tmux.

The real ``agy`` TUI runs in a private tmux server, as Claude Code does in
``TmuxInteractiveTransport``. Three channels carry a session:

* **Hooks.** ``agy`` runs command hooks only. ``agy_hook_bridge`` posts each one
  to the broker (``POST /api/agy/hooks``), which calls :meth:`handle_agy_hook`
  and returns its answer to ``agy``. Hooks give the turn's start and end, every
  tool call, and the conversation's transcript path.
* **Transcript.** ``transcript.jsonl`` holds each finished step: the model's
  text, thinking and token counts, and every tool's output. It is read on each
  hook, so usage is ``agy``'s own count, never an estimate.
* **Screen.** Live text, permission and question menus, the workspace trust
  menu and an interrupt exist only on screen; it is polled and sent to clients
  as terminal frames.

Recorded behaviour this relies on (``agy`` 1.5.15, see
docs/design/skuld-antigravity-gemini.md): ``PreInvocation`` with
``invocationNum == 0`` starts a turn and ``Stop`` with ``fullyIdle`` ends it;
an interrupt (Esc) fires no hook at all, so Skuld closes that turn itself once
the screen shows it stopped; ``agy`` updates itself, so any other version
refuses to start.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import tempfile
import time
import uuid
from collections import Counter, deque
from collections.abc import Mapping
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from niuu.build_info import build_info
from niuu.domain.message_sender import is_human
from niuu.domain.reasoning import AGY_MODELS, MODEL_EFFORTS, preferred_effort, validate_effort
from niuu.ports.cli import CLITransport, TransportCapabilities
from skuld.config import AgyRuntimeConfig
from skuld.control_errors import ControlRecoveryError, HumanInputOnlyError
from skuld.delivery_errors import DeliveryNotAcceptedError, NativeControlPendingError
from skuld.session_runtime import write_private_file
from skuld.transports import agy_hook_bridge, agy_mcp_launcher, agy_screen
from skuld.transports.session_env import session_process_env
from skuld.transports.session_tools import PRESENT_FILE_INSTRUCTION, SessionTools
from skuld.transports.tool_shims import ensure_codex_tool_shims

logger = logging.getLogger("skuld.transport.agy")

HOOK_PATH = "/api/agy/hooks"
#: agy reads only the value "true" here ("1" still updates; checked on 1.5.16).
AUTO_UPDATE_OFF_ENV = "AGY_CLI_DISABLE_AUTO_UPDATE"
#: Name of Skuld's entry in the agy home's hooks.json (other entries are left alone).
HOOK_CONFIG_KEY = "skuld-forge-bridge"
HOOK_EVENTS = (
    "SessionStart",
    "PreInvocation",
    "PostInvocation",
    "PreToolUse",
    "PostToolUse",
    "Stop",
)
TOOL_HOOK_EVENTS = frozenset({"PreToolUse", "PostToolUse"})
#: tmux session option recording which broker (hook URL + secret) this agy reports to.
HOOK_BINDING_OPTION = "@skuld_agy_hook_binding"
QUESTION_TOOL = "ask_question"
AGENT_ENDED_REASON = "no_tool_call"
INTERRUPTED_REASON = "interrupted"
TURN_CAP_SUBTYPE = "turn_cap_reached"
#: Generic permission answers any client may send (Telegram buttons, early answers).
GENERIC_ALLOW = "allow"
GENERIC_ALLOW_ALWAYS = "allow & don't ask again"
GENERIC_DENY = "deny"
# Generic answers mapped onto agy's permission rows, as 1.5.15 words them.
_ALLOW_ROW_RE = re.compile(r"^Yes, (run|allow|proceed|apply|write|edit|read)\b", re.IGNORECASE)
_ALLOW_ALWAYS_ROW_RE = re.compile(r"^Yes, and always allow in this conversation\b", re.IGNORECASE)
_DENY_ROW_RE = re.compile(r"^No\b", re.IGNORECASE)
_TRANSCRIPT_STAMP_RE = re.compile(r"^(Created|Completed) At: \S+$")
_QUESTION_ANSWER_RE = re.compile(r"^A(\d+): (.*)$")
_STEER_NOTICE = """\
This session supports additional input while work is in progress: a message sent while you work \
is queued by the CLI and starts the next turn."""

_KEY_ALIASES = {
    "enter": "Enter",
    "escape": "Escape",
    "esc": "Escape",
    "tab": "Tab",
    "backspace": "BSpace",
    "up": "Up",
    "down": "Down",
    "left": "Left",
    "right": "Right",
    "ctrl-c": "C-c",
    "ctrl+c": "C-c",
}


class AgyNotSignedInError(RuntimeError):
    """agy has no login in the configured home."""


class AgyVersionError(RuntimeError):
    """agy is not the version this Skuld reads."""


@dataclass
class _TmuxResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


class TmuxCommandError(RuntimeError):
    """A required tmux command failed."""


@dataclass
class _Tool:
    tool_id: str
    name: str
    args: dict[str, Any]
    step: int
    started_at: str
    error: str = ""
    finished: bool = False
    result_emitted: bool = False


@dataclass
class _Card:
    request_id: str
    kind: str  # "permission" | "question"
    tool_id: str
    questions: list[dict[str, Any]]
    opened_at: str
    live_labels: tuple[str, ...] = ()
    shown: bool = False
    answer_in_flight: bool = False
    decision: str = ""


@dataclass
class _Turn:
    started: float
    prompt: str = ""
    model: str = ""
    input_tokens: int = 0
    cache_read_tokens: int = 0
    output_tokens: int = 0
    last_text: str = ""
    texts: list[str] = field(default_factory=list)


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _normalize(text: str) -> str:
    return " ".join(text.split())


def _tool_output(content: str) -> str:
    """A transcript tool row's output, without agy's Created/Completed stamps."""
    lines = content.splitlines()
    while lines and _TRANSCRIPT_STAMP_RE.match(lines[0].strip()):
        lines.pop(0)
    return "\n".join(lines).strip()


def _permission_detail(tool_name: str, args: Mapping[str, Any]) -> str:
    command = str(args.get("CommandLine") or "").strip()
    if command:
        return f"Run `{command}`?"
    for key in ("Path", "TargetFile", "AbsolutePath", "DirectoryPath", "Url"):
        value = str(args.get(key) or "").strip()
        if value:
            return f"Allow {tool_name} on {value}?"
    return f"Allow {tool_name}?"


class TmuxAgyTransport(CLITransport):
    """Drive an interactive ``agy`` through tmux, with hooks for structure."""

    def __init__(
        self,
        workspace_dir: str,
        model: str = "",
        session_id: str = "",
        skip_permissions: bool = False,
        system_prompt: str = "",
        initial_prompt: str = "",
        mcp_servers: list[dict] | None = None,
        sdk_port: int | None = None,
        resume_session_id: str = "",
        reasoning_effort: str = "",
        session_tools: SessionTools | None = None,
        agy: Mapping[str, Any] | None = None,
        initial_prompt_sender: dict[str, Any] | None = None,
    ) -> None:
        super().__init__()
        self._config = AgyRuntimeConfig(**dict(agy or {}))
        if not self._config.home:
            raise ValueError("agy.home is not set: point it at the HOME where agy is signed in")
        if not self._config.expected_version:
            raise ValueError(
                "agy.expected_version is not set: set it to the `agy --version` Skuld was "
                "tested with"
            )
        if self._config.mode not in {"auto", "accept-edits", "plan"}:
            raise ValueError(
                f"agy.mode must be auto, accept-edits or plan, not {self._config.mode!r}"
            )
        levels = MODEL_EFFORTS.get(model)
        if model not in AGY_MODELS or not levels:
            raise ValueError(f"{model!r} is not an agy model ({', '.join(AGY_MODELS)})")
        self.workspace_dir = workspace_dir
        self._model = model
        self._levels = levels
        self._effort = (
            validate_effort(reasoning_effort, levels)
            if reasoning_effort
            else preferred_effort(levels, model)
        )
        self._skip_permissions = skip_permissions
        self._system_prompt = system_prompt
        self._initial_prompt = initial_prompt
        self._initial_prompt_sender = initial_prompt_sender
        self._initial_prompt_sent = False
        self._raw_mcp_servers = list(mcp_servers or [])
        self._sdk_port = sdk_port
        self._session_tools = session_tools
        self._conversation_id: str | None = (resume_session_id or "").strip() or None
        self._resume_on_launch = self._conversation_id is not None
        #: Conversations whose earlier steps an earlier launch already reported.
        self._reported_conversations: set[str] = (
            {self._conversation_id} if self._conversation_id else set()
        )
        self._home = Path(self._config.home).expanduser()
        self._session_name = re.sub(r"[^A-Za-z0-9_.-]+", "-", f"skuld-agy-{session_id or 'x'}")[:80]
        socket_dir = self._config.socket_dir or str(
            Path(tempfile.gettempdir()) / f"skuld-agy-{os.getuid()}"
        )
        self._socket_path = Path(socket_dir) / f"{self._session_name}.sock"
        self._runtime_dir = (
            session_tools.runtime_dir / "agy"
            if session_tools is not None
            else Path(workspace_dir) / ".skuld" / "agy" / self._session_name
        )

        self._alive = False
        self._lifecycle_lock = asyncio.Lock()
        self._send_lock = asyncio.Lock()
        self._answer_lock = asyncio.Lock()
        self._hook_lock = asyncio.Lock()
        self._poll_task: asyncio.Task[None] | None = None
        self._turn: _Turn | None = None
        self._last_result: dict | None = None
        self._transcript_path: Path | None = None
        self._transcript_offset = 0
        self._transcript_partial = b""
        self._tools: dict[str, _Tool] = {}
        self._cards: dict[str, _Card] = {}
        self._card_seq = 0
        self._correlations: deque[tuple[str | None, str | None, str]] = deque()
        self._last_frame = ""
        self._frame_seq = 0
        self._interrupting = False
        # Counted, served by list_native_controls: refused child hooks, unread rows, caps.
        self._counts: Counter[str] = Counter()

    # ------------------------------------------------------------------ properties

    @property
    def capabilities(self) -> TransportCapabilities:
        return TransportCapabilities(
            interrupt=True,
            set_effort=True,
            set_model=True,
            steer=True,
            steering_mode="native",
            permission_requests=not self._skip_permissions,
            terminal_output=True,
            terminal_input=True,
            terminal_keys=True,
            native_controls=True,
            session_resume=True,
        )

    @property
    def session_id(self) -> str | None:
        return self._conversation_id

    @property
    def last_result(self) -> dict | None:
        return self._last_result

    @property
    def is_alive(self) -> bool:
        return self._alive

    @property
    def is_turn_active(self) -> bool:
        return self._turn is not None

    @property
    def human_input_only(self) -> bool:
        """The broker refuses every non-human sender when this is set (agy.human_input_only)."""
        return self._config.human_input_only

    @property
    def model(self) -> str:
        return self._model

    @property
    def agy_model_id(self) -> str:
        """agy names a model and its thinking level together, e.g. gemini-4-neon-high."""
        return f"{self._model}-{self._effort}"

    # ------------------------------------------------------------------ lifecycle

    async def start(self) -> None:
        if (
            self._initial_prompt
            and self.human_input_only
            and not is_human(self._initial_prompt_sender)
        ):
            raise HumanInputOnlyError(
                "This session's first prompt is from "
                f"{self._initial_prompt_sender or 'an unknown sender'}, but its Google "
                "login accepts prompts typed by a person only (agy.human_input_only). "
                "Start it without a prompt and type the task yourself."
            )
        await self._ensure_started()
        if self._initial_prompt and not self._initial_prompt_sent:
            self._initial_prompt_sent = True
            try:
                await self.send_message(self._initial_prompt)
            except BaseException:
                self._initial_prompt_sent = False
                raise

    async def _ensure_started(self) -> None:
        async with self._lifecycle_lock:
            if self._alive:
                return
            if shutil.which(self._config.tmux_bin) is None:
                raise RuntimeError(f"tmux ({self._config.tmux_bin}) is required for agy sessions")
            if not self._sdk_port or self._session_tools is None:
                raise RuntimeError(
                    "agy sessions need the broker's hook endpoint (its port and loopback "
                    "secret): agy reports turns, tools and stops only through hooks"
                )
            self._runtime_dir.mkdir(parents=True, exist_ok=True)
            self._socket_path.parent.mkdir(parents=True, exist_ok=True)
            await self._check_version()
            self._install_hooks()
            if await self._has_session() and not await self._adoptable_session():
                await self._run_tmux("kill-session", "-t", self._session_name, check=False)
            if not await self._has_session():
                await self._create_session()
            await self._wait_until_ready()
            self._alive = True
            await self._emit_system_init()
            if self._poll_task is None or self._poll_task.done():
                self._poll_task = asyncio.create_task(
                    self._poll_screen(), name=f"agy-poll-{self._session_name}"
                )

    async def stop(self) -> None:
        async with self._lifecycle_lock:
            self._alive = False
            if self._conversation_id:
                self._reported_conversations.add(self._conversation_id)
            self._transcript_path = None
            self._transcript_partial = b""
            if self._poll_task is not None:
                self._poll_task.cancel()
                with suppress(asyncio.CancelledError):
                    await self._poll_task
                self._poll_task = None
            if self._turn is not None:
                await self._finish_turn(reason="session_stopped", is_error=True)
            await self._run_tmux("kill-session", "-t", self._session_name, check=False)
            with suppress(OSError):
                self._socket_path.unlink(missing_ok=True)
            self._correlations.clear()

    def _agy_env(self) -> dict[str, str]:
        """agy's process env: the session env with agy's home, hook route and tool shims."""
        env = session_process_env()
        env["HOME"] = str(self._home)
        env["TERM"] = env.get("TERM") or "xterm-256color"
        env[AUTO_UPDATE_OFF_ENV] = "true"
        env[agy_hook_bridge.HOOK_TIMEOUT_ENV] = str(self._config.hook_timeout_s)
        env[agy_hook_bridge.HOOK_URL_ENV] = f"http://127.0.0.1:{self._sdk_port}{HOOK_PATH}"
        env[agy_mcp_launcher.SERVERS_ENV] = json.dumps(
            {
                str(server["name"]): {
                    "command": server["command"],
                    "args": list(server.get("args") or []),
                    "env": dict(server.get("env") or {}),
                }
                for server in self._raw_mcp_servers
            }
        )
        _, shim_env = ensure_codex_tool_shims(
            self.workspace_dir,
            mcp_servers=self._raw_mcp_servers,
            session_env=self._session_tools.env if self._session_tools else None,
        )
        env.update(shim_env)
        return env

    async def _check_version(self) -> None:
        env = {
            "HOME": str(self._home),
            "PATH": os.environ.get("PATH", ""),
            AUTO_UPDATE_OFF_ENV: "true",
        }
        try:
            completed = await asyncio.wait_for(
                asyncio.to_thread(
                    subprocess.run,
                    [self._config.binary, "--version"],
                    capture_output=True,
                    text=True,
                    env=env,
                    cwd=self.workspace_dir,
                    check=False,
                ),
                timeout=self._config.version_check_timeout_s,
            )
        except TimeoutError as exc:
            raise AgyVersionError(
                f"`{self._config.binary} --version` did not answer within "
                f"agy.version_check_timeout_s={self._config.version_check_timeout_s}"
            ) from exc
        version = completed.stdout.strip()
        if completed.returncode != 0 or version != self._config.expected_version:
            raise AgyVersionError(
                f"agy at {self._config.binary} is version {version or completed.stderr.strip()!r}, "
                f"but Skuld reads agy {self._config.expected_version} (agy.expected_version). "
                "Re-test Skuld against the new agy and update agy.expected_version, or reinstall "
                "the tested version."
            )

    def _install_hooks(self) -> None:
        """Copy the bridge into the agy home and register it in that home's hooks.json.

        Merged into the file: entries other than Skuld's own are kept. Every session
        sharing the home writes the same entry, so concurrent starts agree.
        """
        bridge_dir = self._home / ".gemini" / "skuld"
        bridge_dir.mkdir(parents=True, exist_ok=True)
        bridge = bridge_dir / "agy-hook.py"
        source = Path(agy_hook_bridge.__file__).read_text(encoding="utf-8")
        if not bridge.exists() or bridge.read_text(encoding="utf-8") != source:
            write_private_file(bridge, source)
        python = shutil.which("python3")
        if python is None:
            raise RuntimeError("python3 is required on PATH to run the agy hook bridge")

        def handler(event: str) -> dict[str, Any]:
            return {
                "type": "command",
                "command": f"{shlex.quote(python)} -I {shlex.quote(str(bridge))} {event}",
                "timeout": int(self._config.hook_timeout_s),
            }

        entry: dict[str, Any] = {}
        for event in HOOK_EVENTS:
            if event in TOOL_HOOK_EVENTS:
                entry[event] = [{"matcher": "*", "hooks": [handler(event)]}]
                continue
            entry[event] = [handler(event)]
        path = self._home / ".gemini" / "config" / "hooks.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        current: dict[str, Any] = {}
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8") or "{}")
            if not isinstance(current, dict):
                raise RuntimeError(f"{path} is not a JSON object; fix or remove it")
        if current.get(HOOK_CONFIG_KEY) != entry:
            current[HOOK_CONFIG_KEY] = entry
            write_private_file(path, json.dumps(current, indent=2) + "\n")
        self._install_mcp_servers(python, bridge_dir)

    def _install_mcp_servers(self, python: str, bridge_dir: Path) -> None:
        """List this session's stdio MCP servers in the home's shared mcp_config.json.

        Each entry runs ``agy_mcp_launcher``, which starts the server described in the
        session's own environment, so sessions sharing the home never see each other's
        broker or token. An entry of the same name that is not Skuld's is never replaced.
        """
        if not self._raw_mcp_servers:
            return
        remote = [s.get("name") for s in self._raw_mcp_servers if not s.get("command")]
        if remote:
            raise ValueError(f"agy sessions run only stdio MCP servers; remove or replace {remote}")
        launcher = bridge_dir / "agy-mcp.py"
        source = Path(agy_mcp_launcher.__file__).read_text(encoding="utf-8")
        if not launcher.exists() or launcher.read_text(encoding="utf-8") != source:
            write_private_file(launcher, source)
        path = self._home / ".gemini" / "config" / "mcp_config.json"
        current: dict[str, Any] = {}
        if path.exists():
            current = json.loads(path.read_text(encoding="utf-8") or "{}")
            if not isinstance(current, dict):
                raise RuntimeError(f"{path} is not a JSON object; fix or remove it")
        servers = current.setdefault("mcpServers", {})
        changed = False
        for server in self._raw_mcp_servers:
            name = str(server["name"])
            wanted = {"command": python, "args": ["-I", str(launcher), name]}
            existing = servers.get(name)
            if existing == wanted:
                continue
            if existing is not None and existing.get("args", [None, None])[1:2] != [str(launcher)]:
                raise RuntimeError(
                    f"{path} already has an MCP server named {name!r} that Skuld did not write; "
                    "rename one of them"
                )
            servers[name] = wanted
            changed = True
        if changed:
            write_private_file(path, json.dumps(current, indent=2) + "\n")

    def _launch_argv(self) -> list[str]:
        argv = [self._config.binary, "--model", self.agy_model_id, "--mode", self._config.mode]
        if self._skip_permissions:
            argv.append("--dangerously-skip-permissions")
        if self._resume_on_launch and self._conversation_id:
            argv.extend(["--conversation", self._conversation_id])
        return argv

    def _hook_binding(self) -> str:
        authorization = self._session_tools.loopback_authorization if self._session_tools else ""
        route = f"{self._sdk_port}{HOOK_PATH}\n{authorization}\n{self.agy_model_id}"
        return hashlib.sha256(route.encode()).hexdigest()[:32]

    async def _adoptable_session(self) -> bool:
        """A running agy may be adopted only if it reports to this broker and still runs."""
        option = await self._run_tmux(
            "show-options", "-v", "-t", self._session_name, HOOK_BINDING_OPTION, check=False
        )
        if option.returncode != 0 or option.stdout.strip() != self._hook_binding():
            return False
        dead = await self._run_tmux(
            "list-panes", "-t", f"{self._session_name}:main.0", "-F", "#{pane_dead}", check=False
        )
        return dead.returncode == 0 and dead.stdout.strip() == "0"

    async def _create_session(self) -> None:
        env = self._agy_env()
        logger.info("agy: starting %s (%s)", self._session_name, " ".join(self._launch_argv()))
        await self._run_tmux(
            "new-session",
            "-d",
            "-s",
            self._session_name,
            "-c",
            self.workspace_dir,
            "-n",
            "main",
            "-x",
            str(self._config.pane_cols),
            "-y",
            str(self._config.pane_rows),
            "--",
            *self._launch_argv(),
            env=env,
        )
        for option in (("status", "off"), ("remain-on-exit", "on")):
            await self._run_tmux("set-option", "-t", self._session_name, *option, check=False)
        await self._run_tmux(
            "set-option", "-t", self._session_name, HOOK_BINDING_OPTION, self._hook_binding()
        )
        self._resume_on_launch = False

    async def _wait_until_ready(self) -> None:
        """Answer the launch workspace's trust menu, then wait for the composer."""
        deadline = time.monotonic() + self._config.ready_timeout_s
        while time.monotonic() < deadline:
            screen = await self._capture()
            if agy_screen.signed_out(screen):
                raise AgyNotSignedInError(
                    f"agy is not signed in for HOME={self._home}. Sign in once with: "
                    f"HOME={shlex.quote(str(self._home))} {shlex.quote(self._config.binary)}"
                )
            trust = agy_screen.trust_prompt(screen)
            if trust is not None:
                await self._answer_trust(trust)
            elif agy_screen.composer_ready(screen):
                return
            if await self._pane_dead():
                raise RuntimeError(f"agy exited during start; last screen:\n{screen.strip()}")
            await asyncio.sleep(self._config.poll_interval_s)
        raise DeliveryNotAcceptedError(
            f"agy did not show its composer within agy.ready_timeout_s="
            f"{self._config.ready_timeout_s}; check the terminal"
        )

    async def _answer_trust(self, trust: agy_screen.TrustPrompt) -> None:
        if Path(trust.workspace).resolve() != Path(self.workspace_dir).resolve():
            raise DeliveryNotAcceptedError(
                f"agy asks to trust {trust.workspace}, not this session's workspace; "
                "answer it in the terminal"
            )
        await self._send_key("Enter" if trust.yes_highlighted else "Up")

    async def _emit_system_init(self) -> None:
        await self._emit(
            {
                "type": "system",
                "subtype": "init",
                "session_id": self._session_name,
                "message": {"model": self._model},
                "build": build_info(),
                "slash_commands": [],
                "terminal": {
                    "transport": "tmux_agy",
                    "session_name": self._session_name,
                    "socket_path": str(self._socket_path),
                    "hook_endpoint": (
                        f"http://127.0.0.1:{self._sdk_port}{HOOK_PATH}" if self._sdk_port else None
                    ),
                },
                "metadata": {"source": "tmux_agy", "agy_model": self.agy_model_id},
            }
        )

    # ------------------------------------------------------------------ input

    async def send_message(
        self, content: str, *, msg_id: str | None = None, request_id: str | None = None
    ) -> None:
        """Type a message into agy. Mid-turn, agy queues it and it starts the next turn."""
        async with self._send_lock:
            if not self._alive:
                await self._ensure_started()
            open_card = next(iter(self._cards.values()), None)
            if open_card is not None:
                raise NativeControlPendingError(
                    f"agy is waiting on a {open_card.kind}; answer it before sending a message "
                    "(nothing was typed)",
                    control_request_id=open_card.request_id,
                    control_kind=open_card.kind,
                    opened_at=open_card.opened_at,
                    menu_state="live",
                )
            screen = await self._capture()
            if agy_screen.permission_menu(screen) or agy_screen.question_menu(screen):
                raise NativeControlPendingError(
                    "An agy menu is on screen; answer it in the terminal first (nothing was typed)",
                    control_request_id="",
                    control_kind="unknown",
                    opened_at=None,
                    menu_state="live",
                )
            if self._turn is None:
                self._begin_turn(content)
            await self._paste(content)
            self._correlations.append((msg_id, request_id, _normalize(content)))

    def _begin_turn(self, prompt: str) -> None:
        self._turn = _Turn(started=time.monotonic(), prompt=prompt)

    async def _paste(self, text: str) -> None:
        buffer = f"skuld-{uuid.uuid4().hex}"
        path = self._runtime_dir / f"input-{uuid.uuid4().hex}.txt"
        write_private_file(path, text.replace("\r", ""))
        try:
            await self._run_tmux("load-buffer", "-b", buffer, str(path))
            await self._run_tmux("paste-buffer", "-p", "-t", self._pane, "-b", buffer)
        finally:
            await self._run_tmux("delete-buffer", "-b", buffer, check=False)
            path.unlink(missing_ok=True)
        await self._send_key("Enter")
        await self._emit(
            {
                "type": "terminal_input_sent",
                "event_type": "terminal.input",
                "pane_id": self._pane,
                "bytes": len(text.encode()),
                "enter": True,
            }
        )

    @property
    def _pane(self) -> str:
        return f"{self._session_name}:main.0"

    async def interrupt(self) -> None:
        await self.send_control("interrupt")

    async def send_control(self, subtype: str, **kwargs: object) -> None:
        if subtype == "interrupt":
            await self._interrupt()
            return
        if subtype == "set_effort":
            await self._relaunch(model=self._model, effort=str(kwargs.get("effort") or ""))
            return
        if subtype == "set_model":
            await self._relaunch(model=str(kwargs.get("model") or ""), effort=self._effort)
            return
        if subtype == "ask_user_answer":
            await self._answer_card(
                str(kwargs.get("request_id") or ""), kwargs.get("answers"), kwargs.get("skipped")
            )
            return
        if subtype in {"redirect", "steer"}:
            content = str(kwargs.get("content") or "")
            if content:
                await self.send_message(
                    content,
                    msg_id=str(kwargs.get("msg_id") or "") or None,
                    request_id=str(kwargs.get("request_id") or "") or None,
                )
            return
        if subtype in {"terminal_input", "input"}:
            text = str(kwargs.get("data") or kwargs.get("text") or "")
            if text:
                async with self._send_lock:
                    await self._paste_raw(text, enter=bool(kwargs.get("enter", False)))
            return
        if subtype in {"terminal_key", "key"}:
            raw = kwargs.get("keys")
            keys = (
                [str(k) for k in raw]
                if isinstance(raw, list) and raw
                else [str(kwargs.get("key") or "")]
            )
            async with self._send_lock:
                for key in filter(None, keys):
                    await self._send_key(_KEY_ALIASES.get(key.strip().lower(), key.strip()))
            return
        raise ValueError(f"agy sessions do not support the {subtype!r} control")

    async def _paste_raw(self, text: str, *, enter: bool) -> None:
        buffer = f"skuld-{uuid.uuid4().hex}"
        path = self._runtime_dir / f"input-{uuid.uuid4().hex}.txt"
        write_private_file(path, text.replace("\r", ""))
        try:
            await self._run_tmux("load-buffer", "-b", buffer, str(path))
            await self._run_tmux("paste-buffer", "-p", "-t", self._pane, "-b", buffer)
        finally:
            await self._run_tmux("delete-buffer", "-b", buffer, check=False)
            path.unlink(missing_ok=True)
        if enter:
            await self._send_key("Enter")

    async def _interrupt(self) -> None:
        """Esc, then close the turn once the screen shows agy stopped (it fires no hook)."""
        if self._turn is None:
            return
        self._interrupting = True
        try:
            async with self._send_lock:
                await self._send_key("Escape")
            deadline = time.monotonic() + self._config.interrupt_confirm_timeout_s
            while time.monotonic() < deadline:
                if self._turn is None:
                    return  # A Stop hook closed it first.
                # Back at an idle composer. An "Interrupted" line is no proof by itself:
                # a resumed conversation shows the ones from its history.
                if agy_screen.composer_ready(await self._capture()):
                    await self._finish_turn(reason=INTERRUPTED_REASON, is_error=True)
                    return
                await asyncio.sleep(self._config.poll_interval_s)
        finally:
            self._interrupting = False
        raise ControlRecoveryError(
            "agy did not show that it stopped within agy.interrupt_confirm_timeout_s="
            f"{self._config.interrupt_confirm_timeout_s}; the turn is still open, "
            "check the terminal"
        )

    async def _relaunch(self, *, model: str, effort: str) -> None:
        """Change model or effort: restart agy on the same conversation (idle only)."""
        levels = MODEL_EFFORTS.get(model)
        if model not in AGY_MODELS or not levels:
            raise ValueError(f"{model!r} is not an agy model ({', '.join(AGY_MODELS)})")
        effort = validate_effort(effort, levels)
        if self._turn is not None:
            raise DeliveryNotAcceptedError(
                "agy is working; change the model or effort between turns"
            )
        self._model, self._levels, self._effort = model, levels, effort
        self._resume_on_launch = self._conversation_id is not None
        if self._alive:
            await self._restart()

    async def _restart(self) -> None:
        async with self._send_lock:
            await self.stop()
            await self._ensure_started()
        # The status line is drawn a moment after the composer.
        deadline = time.monotonic() + self._config.ready_timeout_s
        while True:
            shown = agy_screen.status(await self._capture())
            if shown is not None and shown[2] == self._effort:
                return
            if time.monotonic() >= deadline:
                raise ControlRecoveryError(
                    f"agy restarted but its status line shows {shown!r}, not effort "
                    f"{self._effort}, after agy.ready_timeout_s={self._config.ready_timeout_s}; "
                    "check the terminal"
                )
            await asyncio.sleep(self._config.poll_interval_s)

    async def get_effort(self) -> dict:
        return {
            "current": self._effort,
            "levels": list(self._levels),
            "mutable": True,
            "applies_to": "next_turn",
        }

    # ------------------------------------------------------------------ hooks

    async def handle_agy_hook(self, event: str, payload: dict[str, Any]) -> dict[str, Any]:
        """Turn one agy hook into Skuld events and return agy's answer to it."""
        conversation = str(payload.get("conversationId") or "")
        async with self._hook_lock:
            if not self._is_main_conversation(event, conversation):
                self._counts["child_conversation_hooks"] += 1
                return agy_hook_bridge.NO_BROKER_ANSWERS.get(event, {})
            self._note_transcript(payload)
            await self._read_transcript()
            match event:
                case "SessionStart":
                    return self._session_start_answer()
                case "PreInvocation":
                    if int(payload.get("invocationNum") or 0) == 0:
                        await self._turn_started(payload)
                    return {}
                case "PostInvocation":
                    return {}
                case "PreToolUse":
                    await self._tool_started(payload)
                    return {"decision": "allow"}
                case "PostToolUse":
                    await self._tool_finished(payload)
                    return {}
                case "Stop":
                    await self._stopped(payload)
                    return {"decision": "stop"}
        raise ValueError(f"unknown agy hook event {event!r}")

    def _is_main_conversation(self, event: str, conversation: str) -> bool:
        """The session's own conversation; subagents run their own and are not followed.

        The first conversation seen is the session's. A new conversation that starts
        while no turn runs (``/clear`` in the terminal) replaces it.
        """
        if not conversation:
            raise ValueError("agy hook payload has no conversationId")
        if self._conversation_id is None or (
            conversation != self._conversation_id and event == "SessionStart" and self._turn is None
        ):
            if conversation != self._conversation_id:
                self._transcript_path = None
                self._transcript_offset = 0
                self._transcript_partial = b""
            self._conversation_id = conversation
        return conversation == self._conversation_id

    def _note_transcript(self, payload: dict[str, Any]) -> None:
        path = str(payload.get("transcriptPath") or "")
        if not path or self._transcript_path is not None:
            return
        transcript = Path(path).expanduser()
        if not transcript.is_absolute():
            transcript = self._home / transcript
        self._transcript_path = transcript
        # A resumed conversation's earlier steps were reported by an earlier launch; its
        # new turn starts with the user's row, which Skuld does not read either way.
        resumed = self._conversation_id in self._reported_conversations
        self._transcript_offset = (
            transcript.stat().st_size if resumed and transcript.exists() else 0
        )

    def _session_start_answer(self) -> dict[str, Any]:
        parts = [_STEER_NOTICE]
        parts.append(
            self._session_tools.instructions if self._session_tools else PRESENT_FILE_INSTRUCTION
        )
        if self._system_prompt:
            parts.append(self._system_prompt)
        text = "\n\n".join(part for part in parts if part)
        return {"injectSteps": [{"systemMessage": {"systemMessage": text}}]}

    async def _turn_started(self, payload: dict[str, Any]) -> None:
        prompt = str(payload.get("lastUserInput") or "")
        if self._turn is None:
            self._begin_turn(prompt)  # Typed in the terminal, or a queued message.
        assert self._turn is not None
        self._turn.model = str(payload.get("modelName") or self._turn.model)
        msg_id = request_id = None
        target = _normalize(prompt)
        for index, (cid, rid, text) in enumerate(self._correlations):
            if text == target:
                msg_id, request_id = cid, rid
                del self._correlations[index]
                break
        event: dict[str, Any] = {
            "type": "terminal_prompt_submitted",
            "event_type": "agy.prompt.submitted",
            "prompt": prompt,
            "agy_conversation_id": self._conversation_id,
            "metadata": {"source": "agy_hook"},
        }
        if msg_id:
            event["msg_id"] = msg_id
        if request_id:
            event["request_id"] = request_id
        await self._emit(event)

    async def _tool_started(self, payload: dict[str, Any]) -> None:
        call = payload.get("toolCall") or {}
        name = str(call.get("name") or "")
        args = call.get("args") if isinstance(call.get("args"), dict) else {}
        step = int(payload["stepIdx"])
        tool_id = f"agy-{self._conversation_id}-{step}"
        if self._turn is None:
            self._begin_turn(str(payload.get("lastUserInput") or ""))
        self._tools[tool_id] = _Tool(tool_id, name, args, step, _now_iso())
        await self._emit(
            self._assistant([{"type": "tool_use", "id": tool_id, "name": name, "input": args}])
        )
        if name == QUESTION_TOOL:
            await self._open_question_card(tool_id, args)

    async def _tool_finished(self, payload: dict[str, Any]) -> None:
        step = int(payload["stepIdx"])
        tool_id = f"agy-{self._conversation_id}-{step}"
        tool = self._tools.get(tool_id)
        if tool is None:
            self._counts["unmatched_post_tool_use"] += 1
            logger.error("agy PostToolUse for step %s without its PreToolUse", step)
            return
        tool.finished = True
        tool.error = str(payload.get("error") or "")
        for card in [c for c in self._cards.values() if c.tool_id == tool_id]:
            if card.answer_in_flight:
                continue  # Forge's answer is being entered; that path resolves the card.
            await self._resolve_card(card, card.decision or "resolved_in_terminal")

    async def _stopped(self, payload: dict[str, Any]) -> None:
        if not payload.get("fullyIdle"):
            # Background work still runs; agy fires Stop again when it is idle.
            await self._emit(
                {
                    "type": "system",
                    "subtype": "agy_background_work",
                    "metadata": {"source": "agy_hook"},
                }
            )
            return
        reason = str(payload.get("terminationReason") or "").lower()
        error = str(payload.get("error") or "")
        await self._finish_turn(
            reason="end_turn" if reason == AGENT_ENDED_REASON else reason or "stop",
            is_error=bool(error) or reason not in {AGENT_ENDED_REASON, ""},
            error=error,
        )

    # ------------------------------------------------------------------ transcript

    async def _read_transcript(self) -> None:
        path = self._transcript_path
        if path is None or not path.exists():
            return
        with path.open("rb") as handle:
            handle.seek(self._transcript_offset)
            chunk = handle.read()
        self._transcript_offset += len(chunk)
        data = self._transcript_partial + chunk
        lines = data.split(b"\n")
        self._transcript_partial = lines.pop()
        for line in lines:
            if line.strip():
                await self._transcript_row(json.loads(line))

    async def _transcript_row(self, row: dict[str, Any]) -> None:
        kind = row.get("type")
        if kind == "PLANNER_RESPONSE":
            await self._planner_row(row)
            return
        if kind == "GENERIC":
            await self._tool_row(row)
            return
        if kind in {"USER_INPUT", "SYSTEM_MESSAGE"}:
            return
        self._counts[f"transcript_rows_unread:{kind}"] += 1
        logger.warning("agy transcript row of type %r is not read by Skuld", kind)

    async def _planner_row(self, row: dict[str, Any]) -> None:
        turn = self._turn
        if turn is None:
            self._begin_turn("")
            turn = self._turn
        assert turn is not None
        turn.model = str(row.get("model") or turn.model)
        turn.input_tokens += int(row.get("input_tokens") or 0)
        turn.cache_read_tokens += int(row.get("cache_read_tokens") or 0)
        turn.output_tokens += int(row.get("output_tokens") or 0)
        blocks: list[dict[str, Any]] = []
        thinking = str(row.get("thinking") or "").strip()
        if thinking:
            blocks.append({"type": "thinking", "thinking": thinking})
        text = str(row.get("content") or "").strip()
        if text:
            blocks.append({"type": "text", "text": text})
            turn.last_text = text
            turn.texts.append(text)
        if blocks:
            await self._emit(self._assistant(blocks, model=turn.model))

    async def _tool_row(self, row: dict[str, Any]) -> None:
        tool_id = f"agy-{self._conversation_id}-{int(row['step_index'])}"
        tool = self._tools.get(tool_id)
        if tool is None:
            self._counts["tool_rows_without_hook"] += 1
            logger.warning("agy transcript tool row %s has no PreToolUse hook", tool_id)
            return
        await self._emit_tool_result(tool, _tool_output(str(row.get("content") or "")))

    async def _emit_tool_result(self, tool: _Tool, output: str) -> None:
        if tool.result_emitted:
            return
        tool.result_emitted = True
        block: dict[str, Any] = {
            "type": "tool_result",
            "tool_use_id": tool.tool_id,
            "content": output,
            "started_at": tool.started_at,
            "ended_at": _now_iso(),
        }
        if tool.error:
            block["is_error"] = True
        await self._emit({"type": "user", "message": {"content": [block]}, "content": [block]})

    def _assistant(self, blocks: list[dict[str, Any]], *, model: str = "") -> dict[str, Any]:
        return {
            "type": "assistant",
            "message": {"model": model or self.agy_model_id, "content": blocks},
            "content": blocks,
            "metadata": {"source": "agy"},
        }

    async def _finish_turn(self, *, reason: str, is_error: bool, error: str = "") -> None:
        turn = self._turn
        if turn is None:
            return
        await self._read_transcript()
        for tool in self._tools.values():
            if not tool.result_emitted:
                tool.error = tool.error or "agy recorded no result for this tool call"
                await self._emit_tool_result(tool, tool.error)
        self._tools.clear()
        for card in list(self._cards.values()):
            await self._resolve_card(card, "turn_ended", dismissed=True)
        usage: dict[str, Any] = {}
        if turn.input_tokens or turn.output_tokens or turn.cache_read_tokens:
            usage[turn.model or self.agy_model_id] = {
                "inputTokens": turn.input_tokens,
                "outputTokens": turn.output_tokens,
                "cacheReadInputTokens": turn.cache_read_tokens,
                "cacheCreationInputTokens": 0,
            }
        result = {
            "type": "result",
            "result": turn.last_text,
            "is_error": is_error,
            "stop_reason": reason,
            "modelUsage": usage,
            "sessionId": self._conversation_id,
            "metadata": {"source": "agy", "error": error} if error else {"source": "agy"},
        }
        self._turn = None
        self._last_result = result
        await self._emit(result)

    # ------------------------------------------------------------------ cards

    def _next_request_id(self, kind: str) -> str:
        self._card_seq += 1
        return f"agy-{kind}-{self._card_seq}"

    async def _open_question_card(self, tool_id: str, args: dict[str, Any]) -> None:
        raw = args.get("questions")
        if isinstance(raw, str):
            raw = json.loads(raw)
        questions = [
            {
                "header": f"Question {index + 1}",
                "question": str(q.get("question") or ""),
                "options": [{"label": str(option)} for option in q.get("options") or []],
                "multiSelect": bool(q.get("is_multi_select")),
            }
            for index, q in enumerate(raw or [])
        ]
        card = _Card(self._next_request_id("question"), "question", tool_id, questions, _now_iso())
        self._cards[card.request_id] = card
        await self._emit_card(card)

    async def _open_permission_card(self, menu: agy_screen.Menu) -> None:
        tool = next(
            (
                t
                for t in reversed(self._tools.values())
                if not t.finished and t.name != QUESTION_TOOL
            ),
            None,
        )
        tool_name = tool.name if tool else "agy"
        detail = " ".join(menu.detail) or (_permission_detail(tool_name, tool.args) if tool else "")
        question = {
            "header": tool_name,
            "question": detail,
            "options": [{"label": label} for label in menu.labels],
            "multiSelect": False,
        }
        card = _Card(
            self._next_request_id("permission"),
            "permission",
            tool.tool_id if tool else "",
            [question],
            _now_iso(),
            live_labels=menu.labels,
        )
        self._cards[card.request_id] = card
        await self._emit_card(card)

    async def _emit_card(self, card: _Card) -> None:
        card.shown = True
        await self._emit(
            {
                "type": "ask_user_question",
                "event_type": "ask_user_question",
                "request_id": card.request_id,
                "tool_use_id": card.tool_id or card.request_id,
                "questions": card.questions,
                "metadata": {
                    "source": "agy_tty_bridge",
                    "control_kind": card.kind,
                    "requires_all_answers": True,
                    "opened_at": card.opened_at,
                },
            }
        )

    async def _resolve_card(self, card: _Card, decision: str, *, dismissed: bool = False) -> None:
        if self._cards.pop(card.request_id, None) is None:
            return
        frame: dict[str, Any] = {
            "type": "ask_user_resolved",
            "event_type": "ask_user.resolved",
            "request_id": card.request_id,
            "decision": decision,
            "accepted": not dismissed,
            "metadata": {"source": "agy_tty_bridge"},
        }
        if dismissed:
            frame.update(
                dismissed=True,
                dismiss_reason=decision,
                control_kind=card.kind,
                tool_use_id=card.tool_id,
                opened_at=card.opened_at,
            )
        await self._emit(frame)

    def list_native_controls(self) -> dict:
        return {
            "controls": [
                {
                    "request_id": card.request_id,
                    "kind": card.kind,
                    "tool_use_id": card.tool_id,
                    "opened_at": card.opened_at,
                    "questions": card.questions,
                    "menu_state": "live" if card.shown else "unknown",
                }
                for card in self._cards.values()
            ],
            "counts": dict(self._counts),
        }

    async def dismiss_native_control(self, request_id: str, *, dismissed_by: str) -> dict:
        card = self._cards.get(request_id)
        if card is None:
            raise ValueError(f"No open agy control {request_id!r}")
        await self._resolve_card(card, "dismissed_by_operator", dismissed=True)
        return {"request_id": request_id, "dismissed_by": dismissed_by}

    async def _answer_card(self, request_id: str, answers: object, skipped: object) -> None:
        async with self._answer_lock:
            card = self._cards.get(request_id)
            if card is None:
                raise ValueError("Unknown or already answered agy question")
            if card.answer_in_flight:
                raise ControlRecoveryError("An answer to this agy control is already being entered")
            card.answer_in_flight = True
            try:
                async with self._send_lock:
                    if card.kind == "permission":
                        await self._answer_permission(card, self._answer_texts(answers, 0))
                        return
                    await self._answer_questions(card, answers, skipped)
            finally:
                card.answer_in_flight = False

    @staticmethod
    def _answer_texts(answers: object, index: int) -> list[str]:
        """The chosen labels for question ``index`` from the client's answers."""
        if isinstance(answers, str):
            return [answers] if index == 0 else []
        if not isinstance(answers, list) or index >= len(answers):
            return []
        item = answers[index]
        value = item.get("answer") if isinstance(item, dict) else item
        if isinstance(value, list):
            return [str(v) for v in value]
        return [] if value is None else [str(value)]

    async def _answer_permission(self, card: _Card, chosen: list[str]) -> None:
        if len(chosen) != 1:
            raise ValueError("A permission answer names exactly one option")
        menu = await self._wait_for_menu(agy_screen.permission_menu)
        label = self._permission_row(chosen[0], menu)
        await self._select(menu, label, read=agy_screen.permission_menu)
        card.decision = GENERIC_DENY if _DENY_ROW_RE.match(label) else label
        await self._wait_menu_closed(agy_screen.permission_menu)
        await self._resolve_card(card, card.decision)

    @staticmethod
    def _permission_row(chosen: str, menu: agy_screen.Menu) -> str:
        if chosen in menu.labels:
            return chosen
        generic = chosen.strip().casefold()
        pattern = {
            GENERIC_ALLOW: _ALLOW_ROW_RE,
            GENERIC_ALLOW_ALWAYS: _ALLOW_ALWAYS_ROW_RE,
            GENERIC_DENY: _DENY_ROW_RE,
        }.get(generic)
        match = next((label for label in menu.labels if pattern and pattern.match(label)), None)
        if match is None:
            raise ValueError(f"{chosen!r} is not one of agy's options: " + "; ".join(menu.labels))
        return match

    async def _answer_questions(self, card: _Card, answers: object, skipped: object) -> None:
        if skipped:
            raise ValueError("agy questions cannot be skipped from Forge; answer every question")
        for index, question in enumerate(card.questions):
            chosen = self._answer_texts(answers, index)
            if question["multiSelect"]:
                raise ControlRecoveryError(
                    "Multi-select agy questions are answered in the terminal (attach to the "
                    "session); Skuld has not recorded how agy draws them"
                )
            if len(chosen) != 1:
                raise ValueError(f"Question {index + 1} needs exactly one answer")
            menu = await self._wait_for_menu(
                agy_screen.question_menu,
                matches=lambda m, i=index, q=question: (
                    m.question_index == i + 1 and m.question == q["question"]
                ),
            )
            label = chosen[0]
            if label in menu.labels:
                await self._select(menu, label, read=agy_screen.question_menu)
                continue
            if agy_screen.WRITE_IN_ROW not in menu.labels:
                raise ValueError(f"{label!r} is not an option and this question takes no write-in")
            await self._select(menu, agy_screen.WRITE_IN_ROW, read=agy_screen.question_menu)
            await self._paste_raw(label, enter=True)
        await self._wait_menu_closed(agy_screen.question_menu)
        only = self._answer_texts(answers, 0)
        card.decision = only[0] if len(card.questions) == 1 and len(only) == 1 else "answered"
        await self._resolve_card(card, card.decision)

    async def _wait_menu_closed(self, read) -> None:
        """Keys were sent: the answer counts once agy has taken its menu down."""
        deadline = time.monotonic() + self._config.menu_render_wait_s
        while read(await self._capture()) is not None:
            if time.monotonic() >= deadline:
                raise ControlRecoveryError(
                    "agy's menu is still on screen after the answer was entered; check the "
                    "terminal before answering again"
                )
            await asyncio.sleep(self._config.poll_interval_s)

    async def _wait_for_menu(self, read, *, matches=None) -> agy_screen.Menu:
        deadline = time.monotonic() + self._config.menu_render_wait_s
        while True:
            menu = read(await self._capture())
            if menu is not None and (matches is None or matches(menu)):
                return menu
            if time.monotonic() >= deadline:
                raise ValueError(
                    f"agy's menu was not on screen within agy.menu_render_wait_s="
                    f"{self._config.menu_render_wait_s}; no keys were sent"
                )
            await asyncio.sleep(self._config.poll_interval_s)

    async def _select(self, menu: agy_screen.Menu, label: str, *, read) -> None:
        """Move the highlight to ``label``, check it is there, then press Enter."""
        target = menu.position(label)
        if target is None:
            raise ValueError(f"{label!r} is not on agy's menu: " + "; ".join(menu.labels))
        delta = target - menu.highlighted_position
        for _ in range(abs(delta)):
            await self._send_key("Down" if delta > 0 else "Up")
        moved = read(await self._capture())
        if moved is None or moved.rows[moved.highlighted_position][1] != label:
            raise ControlRecoveryError(
                f"agy's highlight did not reach {label!r}; Enter was not pressed, "
                "check the terminal"
            )
        await self._send_key("Enter")

    # ------------------------------------------------------------------ screen

    async def _poll_screen(self) -> None:
        """Frames for clients, permission menus, a dead agy, and the turn's time bound."""
        while self._alive:
            screen = await self._capture()
            if screen != self._last_frame:
                self._last_frame = screen
                await self._emit_frame(screen)
            await self._watch_permission(screen)
            if self._turn is not None and await self._pane_dead():
                await self._finish_turn(reason="agy_exited", is_error=True, error="agy exited")
                self._alive = False
                return
            await self._watch_turn_cap()
            await asyncio.sleep(self._config.poll_interval_s)

    async def _watch_permission(self, screen: str) -> None:
        if self._skip_permissions:
            return
        open_permission = next((c for c in self._cards.values() if c.kind == "permission"), None)
        menu = agy_screen.permission_menu(screen)
        if menu is None:
            if open_permission is not None and not open_permission.answer_in_flight:
                tool = self._tools.get(open_permission.tool_id)
                if tool is None or tool.finished or not agy_screen.busy(screen):
                    await self._resolve_card(
                        open_permission, open_permission.decision or "resolved_in_terminal"
                    )
            return
        if open_permission is None:
            await self._open_permission_card(menu)

    async def _watch_turn_cap(self) -> None:
        turn = self._turn
        if turn is None or self._interrupting:
            return
        if time.monotonic() - turn.started < self._config.turn_max_seconds:
            return
        self._counts["cap_hits:agy.turn_max_seconds"] += 1
        message = (
            f"agy ran for agy.turn_max_seconds={self._config.turn_max_seconds} without finishing "
            "the turn; Skuld stopped waiting. Raise agy.turn_max_seconds for longer work."
        )
        logger.error(message)
        await self._emit({"type": "system", "subtype": TURN_CAP_SUBTYPE, "message": message})
        await self._finish_turn(reason=TURN_CAP_SUBTYPE, is_error=True, error=message)

    async def _emit_frame(self, screen: str) -> None:
        rows = agy_screen.clean(screen).splitlines()
        self._frame_seq += 1
        await self._emit(
            {
                "type": "terminal_frame",
                "event_type": "terminal.frame",
                "pane_id": self._pane,
                "pane_index": "0",
                "window_name": "main",
                "active": True,
                "current_command": "agy",
                "cols": self._config.pane_cols,
                "rows_count": self._config.pane_rows,
                "rows": rows,
                "text": "\n".join(rows).rstrip(),
                "seq": self._frame_seq,
                "encoding": "utf-8",
                "raw": False,
                "ts": time.time(),
            }
        )

    async def _capture(self) -> str:
        result = await self._run_tmux("capture-pane", "-p", "-t", self._pane, check=False)
        return result.stdout if result.returncode == 0 else ""

    async def _pane_dead(self) -> bool:
        result = await self._run_tmux(
            "list-panes", "-t", self._pane, "-F", "#{pane_dead}", check=False
        )
        return result.returncode != 0 or result.stdout.strip() == "1"

    async def _has_session(self) -> bool:
        return (
            await self._run_tmux("has-session", "-t", self._session_name, check=False)
        ).returncode == 0

    async def _send_key(self, key: str) -> None:
        await self._run_tmux("send-keys", "-t", self._pane, key)

    async def _run_tmux(
        self, *args: str, check: bool = True, env: dict[str, str] | None = None
    ) -> _TmuxResult:
        process = await asyncio.create_subprocess_exec(
            self._config.tmux_bin,
            "-S",
            str(self._socket_path),
            *args,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
        )
        try:
            out, err = await process.communicate()
        except asyncio.CancelledError:
            with suppress(ProcessLookupError):
                process.kill()
            raise
        result = _TmuxResult(
            process.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")
        )
        if check and result.returncode != 0:
            raise TmuxCommandError(f"tmux {' '.join(args)} failed: {result.stderr.strip()}")
        return result


__all__ = ["AgyNotSignedInError", "AgyVersionError", "TmuxAgyTransport"]
