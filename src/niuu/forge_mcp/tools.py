"""Forge MCP tool definitions and dispatch, independent of the MCP transport.

The Skuld broker serves these tools to its own session over stdio (via
``python -m skuld.forge_mcp``); Forge can later serve the same toolbox over
Streamable HTTP for external agents. A transport only has to turn
:meth:`ForgeMcpToolbox.list_tools` / :meth:`ForgeMcpToolbox.call` into protocol
frames.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from niuu.domain.notifications import (
    AGENT_KINDS,
    MAX_BODY_CHARS,
    MAX_CORRELATION_ID_CHARS,
    MAX_LINK_LABEL_CHARS,
    MAX_LINK_URL_CHARS,
    MAX_LINKS,
    MAX_TITLE_CHARS,
    NotificationDraft,
    NotificationKind,
    NotificationLinkKind,
    NotificationSeverity,
    NotificationSource,
)
from niuu.forge_mcp.models import ForgeApiError, ForgeMcpGrant, ForgeMcpLimits, ToolResult
from niuu.forge_mcp.ports import ForgeClient, ForgeMcpHost

# Wire-format patterns Forge itself enforces (request ids) or that keep ids from
# being spliced into a REST path; they are protocol shape, not tunables.
_REQUEST_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,200}$"
_INSTANCE_ID_PATTERN = r"^[A-Za-z0-9._:-]{1,100}$"
_MCP_REQUEST_ID_PREFIX = "fmcp-"
_TRUNCATION_MARK = "…"


class UnknownToolError(KeyError):
    """The requested tool does not exist (a protocol error, not a tool failure)."""


# --------------------------------------------------------------------------- arguments


class _Args(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _uuid_text(value: str) -> str:
    try:
        return str(uuid.UUID(str(value)))
    except ValueError as exc:
        raise ValueError("must be a Forge session UUID") from exc


class _SessionRef(_Args):
    session_id: str
    instance_id: str | None = Field(default=None, pattern=_INSTANCE_ID_PATTERN)

    @field_validator("session_id")
    @classmethod
    def _session_uuid(cls, value: str) -> str:
        return _uuid_text(value)


class _NoArgs(_Args):
    pass


class _NotifyArgs(NotificationDraft):
    model_config = ConfigDict(extra="forbid")

    @field_validator("kind")
    @classmethod
    def _agent_kind(cls, value: NotificationKind) -> NotificationKind:
        if value not in AGENT_KINDS:
            allowed = ", ".join(sorted(kind.value for kind in AGENT_KINDS))
            raise ValueError(f"agents may raise only: {allowed} (reply_ready is automatic)")
        return value


class _ListNotificationsArgs(_Args):
    limit: int | None = Field(default=None, ge=1)
    before: int | None = Field(default=None, ge=0)
    after: int | None = Field(default=None, ge=0)
    kinds: list[NotificationKind] | None = None
    min_severity: NotificationSeverity | None = None
    sources: list[NotificationSource] | None = None
    session_id: str | None = None
    project_id: str | None = None
    unread: bool | None = None

    @field_validator("session_id")
    @classmethod
    def _session_uuid(cls, value: str | None) -> str | None:
        return None if value is None else _uuid_text(value)


class _ListSessionsArgs(_Args):
    limit: int | None = Field(default=None, ge=1)
    status: str | None = Field(default=None, pattern=r"^[a-z_]{1,32}$")
    project_id: str | None = None
    scope: str = Field(default="guild", pattern=r"^(guild|local)$")
    include_archived: bool = False


class _TranscriptArgs(_SessionRef):
    turns: int | None = Field(default=None, ge=1)


class _SendMessageArgs(_SessionRef):
    content: str = Field(min_length=1)
    request_id: str | None = Field(default=None, pattern=_REQUEST_ID_PATTERN)

    @field_validator("content")
    @classmethod
    def _not_blank(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("content must not be blank")
        return value


class _MessageStatusArgs(_SessionRef):
    request_id: str = Field(pattern=_REQUEST_ID_PATTERN)


class _CreateSessionArgs(_Args):
    name: str = Field(min_length=1)
    initial_prompt: str = ""
    model: str = ""
    definition: str | None = None
    launch_spec: str | None = None
    repo: str | None = None
    branch: str | None = None
    local_path: str | None = None
    system_prompt: str = ""

    @field_validator("local_path")
    @classmethod
    def _absolute(cls, value: str | None) -> str | None:
        if value is not None and not value.startswith("/"):
            raise ValueError("local_path must be absolute")
        return value


# --------------------------------------------------------------------------- schemas

_SESSION_ID_SCHEMA = {
    "type": "string",
    "format": "uuid",
    "description": "Forge session UUID (the `id` from list_sessions).",
}
_INSTANCE_ID_SCHEMA = {
    "type": "string",
    "pattern": _INSTANCE_ID_PATTERN,
    "description": "Guild instance that owns the session (the `instance_id` from list_sessions).",
}
_REQUEST_ID_SCHEMA = {"type": "string", "pattern": _REQUEST_ID_PATTERN}


def _object(properties: dict[str, Any], required: Iterable[str] = ()) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": properties,
        "required": list(required),
        "additionalProperties": False,
    }


_NOTIFY_SCHEMA = _object(
    {
        "kind": {
            "type": "string",
            "enum": sorted(kind.value for kind in AGENT_KINDS),
            "description": (
                "milestone = meaningful work finished; decision = a significant choice you "
                "made; attention = blocked or need the user's input; error = a failure the "
                "user must know about; info = a brief FYI."
            ),
        },
        "severity": {
            "type": "string",
            "enum": [severity.value for severity in NotificationSeverity],
            "default": NotificationSeverity.INFO.value,
            "description": "info (default), success, warning (needs a look) or critical.",
        },
        "title": {
            "type": "string",
            "minLength": 1,
            "maxLength": MAX_TITLE_CHARS,
            "description": "One short line the user sees in the feed and on their phone.",
        },
        "body": {
            "type": "string",
            "maxLength": MAX_BODY_CHARS,
            "description": "Optional markdown: a few sentences of context, not a transcript.",
        },
        "links": {
            "type": "array",
            "maxItems": MAX_LINKS,
            "description": "Artifacts to open: PRs, URLs, presented files, other sessions.",
            "items": _object(
                {
                    "label": {"type": "string", "minLength": 1, "maxLength": MAX_LINK_LABEL_CHARS},
                    "url": {"type": "string", "maxLength": MAX_LINK_URL_CHARS},
                    "kind": {
                        "type": "string",
                        "enum": [kind.value for kind in NotificationLinkKind],
                        "default": NotificationLinkKind.URL.value,
                    },
                    "file_id": {
                        "type": "string",
                        "maxLength": MAX_LINK_LABEL_CHARS,
                        "description": "The file_id present-file returned, for kind=file.",
                    },
                },
                required=("label",),
            ),
        },
        "correlation_id": {
            "type": "string",
            "maxLength": MAX_CORRELATION_ID_CHARS,
            "description": "Optional id tying related notifications together (e.g. a PR number).",
        },
    },
    required=("kind", "title"),
)

_NOTIFY_DESCRIPTION = """\
Notify the user about THIS session. The notification appears inline in the \
conversation, in the Forge Notifications feed across every session and host, and \
may reach the user's phone or chat through their delivery rules. Use it sparingly \
and deliberately:

- milestone: a meaningful piece of work is finished (tests green, PR opened, \
migration applied, investigation concluded) — not every step.
- decision: you made a significant choice the user should know about (approach \
picked, scope changed, something intentionally skipped).
- attention: you are blocked or need the user's input or approval; say exactly \
what you need.
- error: something failed that the user must know about.
- info: a brief FYI worth surfacing outside the transcript.

Do NOT send progress updates or narrate routine steps; one good notification \
beats five chatty ones. Do NOT announce that the whole task is complete merely \
because your turn is ending: Forge raises a "reply ready" notification \
automatically at the end of every turn. Keep the title to one short line and the \
body to a few sentences of markdown; link artifacts (PR URLs, files you presented \
with present-file via kind=file and file_id, other sessions) in `links`.

Returns {notification_id, turn_id, session_seq, state}. state=committed means \
Forge recorded it; state=pending means it is saved in this session's durable log \
and will reach Forge automatically when Forge is reachable — do not resend it."""


@dataclass(frozen=True)
class ToolSpec:
    """One tool: its MCP metadata, argument model and required grant."""

    name: str
    description: str
    input_schema: dict[str, Any]
    args_model: type[BaseModel]
    grant: ForgeMcpGrant | None = None

    def to_mcp(self) -> dict[str, Any]:
        description = self.description
        if self.grant is not None:
            description += (
                f"\n\nRequires the '{self.grant.value}' grant; without it the call returns "
                "an error explaining how an operator can grant it."
            )
        return {"name": self.name, "description": description, "inputSchema": self.input_schema}


TOOL_SPECS: tuple[ToolSpec, ...] = (
    ToolSpec(
        name="environment",
        description=(
            "Describe where this session runs: Forge session id and name, host/node, "
            "workspace, engine and model, project, Forge URL, runtime version, attached "
            "MCP servers and the grants this session holds. Contains no secrets. Call it "
            "before referring to your own session or acting on other sessions."
        ),
        input_schema=_object({}),
        args_model=_NoArgs,
    ),
    ToolSpec(
        name="notify",
        description=_NOTIFY_DESCRIPTION,
        input_schema=_NOTIFY_SCHEMA,
        args_model=_NotifyArgs,
    ),
    ToolSpec(
        name="list_notifications",
        description=(
            "List Forge notifications you can see, newest first (bounded). Use it to check "
            "what was already reported before notifying again, or what other sessions "
            "raised. Page older items with `before` (the previous next_before) or catch up "
            "with `after` (a seq)."
        ),
        input_schema=_object(
            {
                "limit": {"type": "integer", "minimum": 1},
                "before": {"type": "integer", "minimum": 0},
                "after": {"type": "integer", "minimum": 0},
                "kinds": {
                    "type": "array",
                    "items": {"type": "string", "enum": [k.value for k in NotificationKind]},
                },
                "min_severity": {
                    "type": "string",
                    "enum": [s.value for s in NotificationSeverity],
                },
                "sources": {
                    "type": "array",
                    "items": {"type": "string", "enum": [s.value for s in NotificationSource]},
                },
                "session_id": _SESSION_ID_SCHEMA,
                "project_id": {"type": "string"},
                "unread": {"type": "boolean"},
            }
        ),
        args_model=_ListNotificationsArgs,
    ),
    ToolSpec(
        name="list_sessions",
        description=(
            "List Forge sessions you can see (bounded, newest activity first as Forge "
            "returns them), across every Guild host by default. Each item carries id and "
            "instance_id — pass both to the other session tools."
        ),
        input_schema=_object(
            {
                "limit": {"type": "integer", "minimum": 1},
                "status": {
                    "type": "string",
                    "description": "e.g. running, stopped, starting, failed.",
                },
                "project_id": {"type": "string"},
                "scope": {"type": "string", "enum": ["guild", "local"], "default": "guild"},
                "include_archived": {"type": "boolean", "default": False},
            }
        ),
        args_model=_ListSessionsArgs,
    ),
    ToolSpec(
        name="get_session",
        description="Get one Forge session's state: status, activity, model, source, project.",
        input_schema=_object(
            {"session_id": _SESSION_ID_SCHEMA, "instance_id": _INSTANCE_ID_SCHEMA},
            required=("session_id",),
        ),
        args_model=_SessionRef,
    ),
    ToolSpec(
        name="session_transcript",
        description=(
            "Read the tail of a session's conversation: the last `turns` turns, each "
            "truncated, with the tools each turn used. Use it to see what a peer session "
            "is doing before messaging it."
        ),
        input_schema=_object(
            {
                "session_id": _SESSION_ID_SCHEMA,
                "instance_id": _INSTANCE_ID_SCHEMA,
                "turns": {"type": "integer", "minimum": 1},
            },
            required=("session_id",),
        ),
        args_model=_TranscriptArgs,
    ),
    ToolSpec(
        name="send_message",
        description=(
            "Send a message into ANOTHER session as its next user input. It is delivered "
            "as operator-authorized steering, not as a verified agent identity, so say who "
            "you are and why in the content. Reuse the same request_id when retrying so it "
            "is never delivered twice (one is generated and returned if you omit it); "
            "check delivery with message_status."
        ),
        input_schema=_object(
            {
                "session_id": _SESSION_ID_SCHEMA,
                "instance_id": _INSTANCE_ID_SCHEMA,
                "content": {"type": "string", "minLength": 1},
                "request_id": _REQUEST_ID_SCHEMA,
            },
            required=("session_id", "content"),
        ),
        args_model=_SendMessageArgs,
        grant=ForgeMcpGrant.MESSAGE,
    ),
    ToolSpec(
        name="message_status",
        description=(
            "Delivery status of a message sent with send_message (accepted, pending, "
            "delivered or failed). Delivered means the agent received it, not that it "
            "acted on it."
        ),
        input_schema=_object(
            {
                "session_id": _SESSION_ID_SCHEMA,
                "instance_id": _INSTANCE_ID_SCHEMA,
                "request_id": _REQUEST_ID_SCHEMA,
            },
            required=("session_id", "request_id"),
        ),
        args_model=_MessageStatusArgs,
        grant=ForgeMcpGrant.MESSAGE,
    ),
    ToolSpec(
        name="create_session",
        description=(
            "Create and start a new Forge session. Returns its launch state — creation is "
            "not completion; follow it with get_session and session_transcript. Give "
            "either repo (+branch) or an absolute local_path as the workspace."
        ),
        input_schema=_object(
            {
                "name": {
                    "type": "string",
                    "description": "Lowercase RFC 1123 label (a-z, 0-9, hyphens), max 63.",
                },
                "initial_prompt": {"type": "string", "description": "The first user message."},
                "model": {"type": "string"},
                "definition": {
                    "type": "string",
                    "description": "Session definition, e.g. skuldClaude or skuldCodex.",
                },
                "launch_spec": {"type": "string"},
                "repo": {"type": "string"},
                "branch": {"type": "string"},
                "local_path": {"type": "string"},
                "system_prompt": {"type": "string"},
            },
            required=("name",),
        ),
        args_model=_CreateSessionArgs,
        grant=ForgeMcpGrant.LIFECYCLE,
    ),
    ToolSpec(
        name="start_session",
        description="Start (or resume) a stopped Forge session.",
        input_schema=_object(
            {"session_id": _SESSION_ID_SCHEMA, "instance_id": _INSTANCE_ID_SCHEMA},
            required=("session_id",),
        ),
        args_model=_SessionRef,
        grant=ForgeMcpGrant.LIFECYCLE,
    ),
    ToolSpec(
        name="stop_session",
        description="Stop another Forge session. Its workspace and transcript are kept.",
        input_schema=_object(
            {"session_id": _SESSION_ID_SCHEMA, "instance_id": _INSTANCE_ID_SCHEMA},
            required=("session_id",),
        ),
        args_model=_SessionRef,
        grant=ForgeMcpGrant.LIFECYCLE,
    ),
)

TOOL_NAMES: tuple[str, ...] = tuple(spec.name for spec in TOOL_SPECS)


# --------------------------------------------------------------------------- projections


def _truncate(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)] + _TRUNCATION_MARK


def _session_summary(raw: dict[str, Any]) -> dict[str, Any]:
    """The fields a model needs to reason about a session; endpoints and tokens are dropped."""
    source = raw.get("source") if isinstance(raw.get("source"), dict) else {}
    coordination = raw.get("coordination") if isinstance(raw.get("coordination"), dict) else {}
    summary = {
        "id": raw.get("id"),
        "instance_id": raw.get("instance_id"),
        "name": raw.get("name"),
        "status": raw.get("status"),
        "activity_state": raw.get("activity_state"),
        "activity_state_since": raw.get("activity_state_since"),
        "needs_attention": raw.get("needs_attention"),
        "model": raw.get("model"),
        "workload_type": raw.get("workload_type"),
        "project_id": coordination.get("project_id"),
        "role": coordination.get("role"),
        "source": {
            key: source.get(key)
            for key in ("type", "repo", "branch", "local_path")
            if source.get(key)
        },
        "created_at": raw.get("created_at"),
        "last_active": raw.get("last_active"),
        "message_count": raw.get("message_count"),
        "error": raw.get("error"),
    }
    return {key: value for key, value in summary.items() if value not in (None, {}, "")}


def _turn_summary(turn: dict[str, Any], *, max_chars: int) -> dict[str, Any]:
    parts = turn.get("parts") if isinstance(turn.get("parts"), list) else []
    tools = [
        str(part.get("name"))
        for part in parts
        if isinstance(part, dict) and part.get("type") == "tool_use" and part.get("name")
    ]
    summary: dict[str, Any] = {
        "id": turn.get("id"),
        "role": turn.get("role"),
        "created_at": turn.get("created_at"),
        "content": _truncate(str(turn.get("content") or ""), max_chars),
    }
    if tools:
        summary["tools"] = tools
    return summary


def _notification_summary(raw: dict[str, Any], *, max_body_chars: int) -> dict[str, Any]:
    keys = (
        "id",
        "seq",
        "session_id",
        "session_name",
        "instance_id",
        "kind",
        "severity",
        "source",
        "title",
        "links",
        "created_at",
        "read",
    )
    summary = {key: raw.get(key) for key in keys if raw.get(key) is not None}
    body = str(raw.get("body") or "")
    if body:
        summary["body"] = _truncate(body, max_body_chars)
    return summary


def _forge_error_text(tool: str, exc: ForgeApiError) -> str:
    if exc.status is None:
        return f"{tool} failed: Forge is unreachable ({exc.detail}). Try again later."
    return f"{tool} failed: Forge answered {exc.status}: {exc.detail}"


def _validation_text(tool: str, exc: ValidationError) -> str:
    problems = "; ".join(
        f"{'.'.join(str(loc) for loc in error['loc']) or 'arguments'}: {error['msg']}"
        for error in exc.errors()
    )
    return f"Invalid arguments for {tool}: {problems}"


# --------------------------------------------------------------------------- dispatch


class ForgeMcpToolbox:
    """Lists and runs the Forge MCP tools against a :class:`ForgeClient` and a host."""

    def __init__(
        self,
        *,
        client: ForgeClient,
        host: ForgeMcpHost,
        grants: Iterable[ForgeMcpGrant | str],
        limits: ForgeMcpLimits,
    ) -> None:
        self._client = client
        self._host = host
        self._grants = frozenset(ForgeMcpGrant(grant) for grant in grants)
        self._limits = limits
        self._specs = {spec.name: spec for spec in TOOL_SPECS}
        self._handlers: dict[str, Callable[[Any], Awaitable[ToolResult]]] = {
            "environment": self._environment,
            "notify": self._notify,
            "list_notifications": self._list_notifications,
            "list_sessions": self._list_sessions,
            "get_session": self._get_session,
            "session_transcript": self._session_transcript,
            "send_message": self._send_message,
            "message_status": self._message_status,
            "create_session": self._create_session,
            "start_session": self._start_session,
            "stop_session": self._stop_session,
        }

    @property
    def grants(self) -> frozenset[ForgeMcpGrant]:
        return self._grants

    def list_tools(self) -> list[dict[str, Any]]:
        """Every tool, granted or not, so the model learns why a denied one fails."""
        return [spec.to_mcp() for spec in TOOL_SPECS]

    async def call(self, name: str, arguments: dict[str, Any] | None) -> ToolResult:
        spec = self._specs.get(name)
        if spec is None:
            raise UnknownToolError(name)
        if spec.grant is not None and spec.grant not in self._grants:
            return ToolResult.error(
                f"The {name} tool needs the '{spec.grant.value}' grant, which this session "
                "does not have. An operator can add it to the session's launch spec or "
                "session create request (forge_mcp.grants). Do not try to work around it; "
                "tell the user what you wanted to do instead."
            )
        try:
            args = spec.args_model.model_validate(arguments or {})
        except ValidationError as exc:
            return ToolResult.error(_validation_text(name, exc))
        try:
            return await self._handlers[name](args)
        except ForgeApiError as exc:
            return ToolResult.error(_forge_error_text(name, exc))

    def _ok(self, payload: dict[str, Any]) -> ToolResult:
        return ToolResult.of(payload, max_chars=self._limits.output_max_chars)

    def _refuse_self(self, session_id: str, action: str) -> ToolResult | None:
        if self._host.session_id and session_id == self._host.session_id:
            return ToolResult.error(f"{action} targets your own session; that is not allowed.")
        return None

    # -- own session

    async def _environment(self, _args: _NoArgs) -> ToolResult:
        environment = await self._host.environment()
        environment["grants"] = sorted(grant.value for grant in self._grants)
        environment["tools"] = [
            spec.name for spec in TOOL_SPECS if spec.grant is None or spec.grant in self._grants
        ]
        return self._ok(environment)

    async def _notify(self, args: _NotifyArgs) -> ToolResult:
        draft = NotificationDraft.model_validate(args.model_dump())
        return self._ok(await self._host.notify(draft))

    # -- reads

    async def _list_notifications(self, args: _ListNotificationsArgs) -> ToolResult:
        limit = self._limits.clamp_list(args.limit)
        params: dict[str, str] = {"limit": str(limit)}
        if args.before is not None:
            params["before"] = str(args.before)
        if args.after is not None:
            params["after"] = str(args.after)
        if args.kinds:
            params["kind"] = ",".join(kind.value for kind in args.kinds)
        if args.min_severity is not None:
            params["min_severity"] = args.min_severity.value
        if args.sources:
            params["source"] = ",".join(source.value for source in args.sources)
        if args.session_id:
            params["session_id"] = args.session_id
        if args.project_id:
            params["project_id"] = args.project_id
        if args.unread is not None:
            params["unread"] = "true" if args.unread else "false"
        page = await self._client.list_notifications(params)
        items = page.get("items") if isinstance(page.get("items"), list) else []
        body_chars = self._limits.transcript_turn_max_chars
        payload = {
            "items": [
                _notification_summary(item, max_body_chars=body_chars)
                for item in items[:limit]
                if isinstance(item, dict)
            ],
            "next_before": page.get("next_before"),
            "head_seq": page.get("head_seq"),
            "unread_count": page.get("unread_count"),
        }
        return self._ok(payload)

    async def _list_sessions(self, args: _ListSessionsArgs) -> ToolResult:
        limit = self._limits.clamp_list(args.limit)
        params = {"scope": args.scope}
        if args.status:
            params["status"] = args.status
        if args.project_id:
            params["project_id"] = args.project_id
        if args.include_archived:
            params["include_archived"] = "true"
        sessions = await self._client.list_sessions(params)
        summaries = [_session_summary(s) for s in sessions if isinstance(s, dict)]
        return self._ok(
            {
                "total": len(summaries),
                "returned": min(len(summaries), limit),
                "sessions": summaries[:limit],
            }
        )

    async def _get_session(self, args: _SessionRef) -> ToolResult:
        session = await self._client.get_session(args.session_id, instance_id=args.instance_id)
        return self._ok({"session": _session_summary(session)})

    async def _session_transcript(self, args: _TranscriptArgs) -> ToolResult:
        turns = self._limits.clamp_turns(args.turns)
        page = await self._client.get_conversation(
            args.session_id, turns=turns, instance_id=args.instance_id
        )
        raw_turns = page.get("turns") if isinstance(page.get("turns"), list) else []
        max_chars = self._limits.transcript_turn_max_chars
        tail = [
            _turn_summary(turn, max_chars=max_chars)
            for turn in raw_turns[-turns:]
            if isinstance(turn, dict) and not turn.get("in_progress")
        ]
        return self._ok(
            {
                "session_id": args.session_id,
                "total_turns": page.get("total_turns", len(raw_turns)),
                "turns": tail,
            }
        )

    # -- granted: message

    async def _send_message(self, args: _SendMessageArgs) -> ToolResult:
        refused = self._refuse_self(args.session_id, "send_message")
        if refused is not None:
            return refused
        request_id = args.request_id or f"{_MCP_REQUEST_ID_PREFIX}{uuid.uuid4().hex}"
        result = await self._client.send_message(
            args.session_id,
            content=args.content,
            request_id=request_id,
            instance_id=args.instance_id,
        )
        return self._ok(
            {
                "session_id": args.session_id,
                "request_id": request_id,
                "status": result.get("status"),
                "delivery": result.get("delivery"),
            }
        )

    async def _message_status(self, args: _MessageStatusArgs) -> ToolResult:
        status = await self._client.get_message_delivery(
            args.session_id, request_id=args.request_id, instance_id=args.instance_id
        )
        return self._ok({"session_id": args.session_id, "delivery": status})

    # -- granted: lifecycle

    async def _create_session(self, args: _CreateSessionArgs) -> ToolResult:
        if args.repo and args.local_path:
            return ToolResult.error("create_session takes repo or local_path, not both.")
        body: dict[str, Any] = {"name": args.name}
        for key in ("initial_prompt", "model", "system_prompt", "definition", "launch_spec"):
            value = getattr(args, key)
            if value:
                body[key] = value
        if args.repo:
            body["source"] = {"type": "git", "repo": args.repo}
            if args.branch:
                body["source"]["branch"] = args.branch
        if args.local_path:
            body["source"] = {"type": "local_mount", "local_path": args.local_path}
        created = await self._client.create_session(body)
        return self._ok({"session": _session_summary(created)})

    async def _start_session(self, args: _SessionRef) -> ToolResult:
        started = await self._client.start_session(args.session_id, instance_id=args.instance_id)
        return self._ok({"session": _session_summary(started)})

    async def _stop_session(self, args: _SessionRef) -> ToolResult:
        refused = self._refuse_self(args.session_id, "stop_session")
        if refused is not None:
            return refused
        stopped = await self._client.stop_session(args.session_id, instance_id=args.instance_id)
        return self._ok({"session": _session_summary(stopped)})
