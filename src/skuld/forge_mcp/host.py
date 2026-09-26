"""The broker as the Forge MCP host: its own session's environment and notifications."""

from __future__ import annotations

import socket
import uuid
from dataclasses import asdict
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from niuu.build_identity import build_identity
from niuu.domain.notifications import NotificationDraft, build_notification_turn
from niuu.domain.services.forge_session_token import SESSION_ID_CLAIM
from niuu.forge_mcp.credentials import is_forge_session_claims, token_scopes, unverified_claims
from niuu.forge_mcp.models import ForgeApiError
from niuu.forge_mcp.ports import ForgeClient, ForgeMcpHost
from skuld.conversation_models import ConversationTurn

if TYPE_CHECKING:
    from skuld.broker import Broker

NOTIFICATION_TURN_PREFIX = "nt_"
STATE_COMMITTED = "committed"
STATE_PENDING = "pending"

# Transport adapter module → engine name reported to the model.
_ENGINE_BY_MODULE = {
    "skuld.transports.tmux_interactive": "claude",
    "skuld.transports.sdk": "claude",
    "skuld.transports.sdk_websocket": "claude",
    "skuld.transports.subprocess": "claude",
    "skuld.transports.persistent_subprocess": "claude",
    "skuld.transports.remote_control": "claude",
    "skuld.transports.codex": "codex",
    "skuld.transports.codex_ws": "codex",
    "skuld.transports.grok": "grok",
    "skuld.transports.opencode": "opencode",
    "skuld.transports.muse": "muse",
    "skuld.transports.pi": "pi",
    "skuld.transports.dsh": "dsh",
}


def forge_credential(token: str) -> dict[str, Any]:
    """What credential the MCP's Forge calls use; never the token itself."""
    claims = unverified_claims(token)
    if not is_forge_session_claims(claims):
        return {"kind": "broker", "note": "no session token; the broker's own credential"}
    expires = claims.get("exp")
    return {
        "kind": "forge_session",
        "session_id": claims.get(SESSION_ID_CLAIM),
        "scopes": list(token_scopes(claims)),
        "expires_at": (
            datetime.fromtimestamp(int(expires), UTC).isoformat()
            if isinstance(expires, int | float)
            else None
        ),
    }


def engine_for_adapter(adapter_path: str) -> str:
    module = adapter_path.rsplit(".", 1)[0]
    return _ENGINE_BY_MODULE.get(module, module.rsplit(".", 1)[-1])


def new_notification_turn_id() -> str:
    return f"{NOTIFICATION_TURN_PREFIX}{uuid.uuid4().hex}"


class BrokerForgeMcpHost(ForgeMcpHost):
    """Environment and notify for the broker's own session."""

    def __init__(self, broker: Broker, client: ForgeClient) -> None:
        self._broker = broker
        self._client = client

    @property
    def session_id(self) -> str | None:
        return str(self._broker.session_id)

    async def environment(self) -> dict[str, Any]:
        broker = self._broker
        settings = broker._settings
        environment: dict[str, Any] = {
            "session": {"id": str(broker.session_id), "name": settings.session.name},
            "node": {"hostname": socket.gethostname()},
            "workspace": broker.workspace_dir,
            "engine": engine_for_adapter(settings.transport_adapter),
            "transport": settings.transport_adapter.rsplit(".", 1)[-1],
            "model": broker.model,
            "reasoning_effort": settings.session.reasoning_effort or None,
            "forge_url": broker.volundr_api_url or None,
            "runtime": build_identity(),
            "mcp_servers": broker._effective_mcp_server_names(),
            "skills": broker._materialized_skill_names(),
            "present_file": True,
            "durable_log": bool(settings.event_log_enabled and broker.volundr_api_url),
            "forge_credential": forge_credential(settings.forge_mcp.token),
        }
        environment.update(await self._forge_view())
        return environment

    async def _forge_view(self) -> dict[str, Any]:
        """Project and Guild instance come from Forge's session row, not local config."""
        if not self._broker.volundr_api_url:
            return {"project_id": None, "instance_id": None, "forge_reachable": False}
        try:
            session = await self._client.get_session(str(self._broker.session_id), instance_id=None)
        except ForgeApiError as exc:
            return {
                "project_id": None,
                "instance_id": None,
                "forge_reachable": exc.status is not None,
                "forge_error": exc.detail,
            }
        coordination = session.get("coordination")
        coordination = coordination if isinstance(coordination, dict) else {}
        return {
            "project_id": coordination.get("project_id"),
            "project_role": coordination.get("role"),
            "parent": coordination.get("parent") or None,
            "instance_id": session.get("instance_id"),
            "forge_reachable": True,
        }

    async def notify(self, draft: NotificationDraft) -> dict[str, Any]:
        broker = self._broker
        if not (broker._settings.event_log_enabled and broker.volundr_api_url):
            raise ForgeApiError(
                "this session has no durable Forge log (volundr_api_url unset or "
                "event_log_enabled=false), so a notification could never reach Forge",
                status=503,
            )
        turn_id = new_notification_turn_id()
        created_at = datetime.now(UTC).isoformat()
        turn = ConversationTurn(
            **build_notification_turn(
                draft,
                turn_id=turn_id,
                session_id=str(broker.session_id),
                created_at=created_at,
            )
        )
        # Same durable path as present-file: history + durable log first, then live.
        entry = broker._append_turn(turn)
        if entry is None:
            raise ForgeApiError("the durable log refused the notification turn", status=503)
        receipt = broker._watch_event_log_entry(entry)
        await broker._channels.broadcast({"type": "conversation.turn", "turn": asdict(turn)})
        stored = await broker._await_event_log_receipt(
            receipt, broker._settings.forge_mcp.notify_confirm_timeout_s
        )
        if stored is False:
            raise ForgeApiError(
                "the notification's log entry was not stored (dropped while Forge was "
                "unreachable, or its sequence conflicted); it is visible in this session only",
                status=409,
            )
        notification = turn.parts[0]["input"]
        return {
            "notification_id": notification["notification_id"],
            "turn_id": turn_id,
            "session_seq": entry["seq"],
            "state": STATE_COMMITTED if stored else STATE_PENDING,
        }
