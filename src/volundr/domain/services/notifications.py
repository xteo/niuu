"""Forge notification service: projection, the reader feed, watermarks and rules.

Producers never write the feed directly. A notification is *projected* from a
durable source (a stored session-log row, a needs-input transition, or a direct
submit carrying an idempotency key) into ``forge_notifications`` together with
its scheduled deliveries, in one transaction. Only newly inserted rows are then
broadcast as ``session_notification``, so producer retries never duplicate a row,
a delivery or a live event.
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from uuid import UUID, uuid4

from niuu.domain.models import IntegrationType, Principal
from niuu.domain.notifications import (
    AGENT_KINDS,
    MAX_BODY_CHARS,
    MAX_TITLE_CHARS,
    NotificationDraft,
    NotificationKind,
    NotificationSeverity,
    NotificationSource,
    draft_from_log_payload,
    summarize_reply,
    turn_dedupe_key,
)
from volundr.domain.models import EventType, RealtimeEvent, Session, SessionLogEntry
from volundr.domain.notification_ports import (
    NotificationDeliveryRepository,
    NotificationRecorder,
    NotificationRepository,
    NotificationRuleRepository,
)
from volundr.domain.notifications import (
    INTEGRATION_SINK,
    Notification,
    NotificationCandidate,
    NotificationDelivery,
    NotificationFeed,
    NotificationNotFoundError,
    NotificationQuery,
    NotificationReadState,
    NotificationRule,
    NotificationRuleSpec,
    NotificationScope,
    NotificationSinkInfo,
    NotificationValidationError,
)
from volundr.domain.ports import EventBroadcaster, IntegrationRepository
from volundr.domain.session_read_state import is_final_output

logger = logging.getLogger(__name__)

EngineResolver = Callable[[Session], str | None]


def engine_resolver_for(
    cli_types: Mapping[str, str | None], default_definition: str
) -> EngineResolver:
    """Map a session to its engine through its session definition's broker CLI type.

    ``cli_types`` maps a session-definition key to ``defaults.broker.cliType``
    (``claude``, ``codex-ws``, ``grok`` ...). The engine is the CLI family, so
    ``codex-ws`` is reported as ``codex``. Sessions persisted before definitions
    were recorded ran on the default definition.
    """

    def resolve(session: Session) -> str | None:
        cli_type = cli_types.get(session.session_definition or default_definition)
        if not cli_type:
            return None
        return cli_type.split("-", 1)[0]

    return resolve


@dataclass(frozen=True)
class _SessionContext:
    session_id: UUID
    session_name: str
    owner_id: str
    tenant_id: str | None
    project_id: str | None
    engine: str | None
    model: str | None


def _bounded(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"


def attention_dedupe_key(session_id: UUID, state_since: datetime, request_id: str) -> str:
    """Dedupe key for one needs-input request.

    ``attention:{session_id}:{state_since ISO}``, suffixed with the pending
    ``request_id`` when the broker reports one: a second question raised while the
    session is still awaiting input keeps the same ``state_since`` and must not be
    swallowed as a duplicate of the first.
    """
    key = f"attention:{session_id}:{state_since.isoformat()}"
    if not request_id:
        return key
    return f"{key}:{request_id}"


def submit_dedupe_key(session_id: UUID | None, principal_id: str, idempotency_key: str) -> str:
    return f"submit:{session_id or '-'}:{principal_id}:{idempotency_key}"


class NotificationService(NotificationRecorder):
    """Projects, stores and serves Forge notifications."""

    def __init__(
        self,
        repository: NotificationRepository,
        rule_repository: NotificationRuleRepository,
        delivery_repository: NotificationDeliveryRepository,
        *,
        broadcaster: EventBroadcaster | None = None,
        reply_ready_enabled: bool,
        reply_title_chars: int,
        reply_body_chars: int,
        sinks: list[NotificationSinkInfo],
        engine_resolver: EngineResolver | None = None,
        integration_repository: IntegrationRepository | None = None,
    ) -> None:
        self._repository = repository
        self._rules = rule_repository
        self._deliveries = delivery_repository
        self._broadcaster = broadcaster
        self._reply_ready_enabled = reply_ready_enabled
        self._reply_title_chars = reply_title_chars
        self._reply_body_chars = reply_body_chars
        self._sinks = list(sinks)
        self._engine_resolver = engine_resolver
        self._integrations = integration_repository

    # -- Projection --------------------------------------------------------

    def is_projectable(self, payload: dict) -> bool:
        """Cheap pre-filter: could this log frame produce a notification?"""
        if draft_from_log_payload(payload) is not None:
            return True
        return self._reply_ready_enabled and is_final_output(payload)

    async def project_log_entries(
        self, session: Session, entries: list[SessionLogEntry]
    ) -> list[Notification]:
        """Project STORED session-log rows; returns (and broadcasts) only new rows."""
        context = self._context(session)
        candidates: list[NotificationCandidate] = []
        for entry in sorted(entries, key=lambda item: item.seq):
            if entry.session_id != session.id:
                raise ValueError("Every projected log row must belong to the session")
            candidates.extend(self._candidates_for_entry(context, entry))
        return await self._record(candidates)

    def _candidates_for_entry(
        self, context: _SessionContext, entry: SessionLogEntry
    ) -> list[NotificationCandidate]:
        candidates: list[NotificationCandidate] = []
        parsed = draft_from_log_payload(entry.payload)
        if parsed is not None:
            turn_id, draft = parsed
            candidates.append(
                self._candidate(
                    context,
                    dedupe_key=turn_dedupe_key(context.session_id, turn_id, draft.kind),
                    draft=draft,
                    source=NotificationSource.AGENT,
                    session_seq=entry.seq,
                    metadata={"turn_id": turn_id},
                )
            )
        if self._reply_ready_enabled and is_final_output(entry.payload):
            turn = entry.payload["turn"]
            title, body = summarize_reply(
                turn["content"],
                title_chars=self._reply_title_chars,
                body_chars=self._reply_body_chars,
            )
            draft = NotificationDraft(
                kind=NotificationKind.REPLY_READY,
                severity=NotificationSeverity.SUCCESS,
                title=title,
                body=body,
            )
            candidates.append(
                self._candidate(
                    context,
                    dedupe_key=turn_dedupe_key(
                        context.session_id, turn["id"], NotificationKind.REPLY_READY
                    ),
                    draft=draft,
                    source=NotificationSource.SYSTEM,
                    session_seq=entry.seq,
                    metadata={"turn_id": turn["id"]},
                )
            )
        return candidates

    async def record_attention(
        self,
        session: Session,
        *,
        state_since: datetime,
        kind: str,
        prompt: str,
        request_id: str,
    ) -> Notification | None:
        context = self._context(session)
        draft = NotificationDraft(
            kind=NotificationKind.ATTENTION,
            severity=NotificationSeverity.WARNING,
            title=_bounded(f"{context.session_name} needs your input", MAX_TITLE_CHARS),
            body=_bounded(prompt.strip(), MAX_BODY_CHARS),
        )
        candidate = self._candidate(
            context,
            dedupe_key=attention_dedupe_key(session.id, state_since, request_id),
            draft=draft,
            source=NotificationSource.SYSTEM,
            session_seq=None,
            metadata={"input_kind": kind, "request_id": request_id},
        )
        created = await self._record([candidate])
        return created[0] if created else None

    async def submit(
        self,
        session: Session,
        principal: Principal,
        draft: NotificationDraft,
        *,
        idempotency_key: str,
    ) -> tuple[Notification, bool]:
        """Direct submit; returns ``(notification, created)`` (False when deduped)."""
        if draft.kind not in AGENT_KINDS:
            raise NotificationValidationError(
                f"Kind {draft.kind.value!r} is derived by Forge and cannot be submitted"
            )
        context = self._context(session)
        candidate = self._candidate(
            context,
            dedupe_key=submit_dedupe_key(session.id, principal.user_id, idempotency_key),
            draft=draft,
            source=NotificationSource.OPERATOR,
            session_seq=None,
            metadata={"submitted_by": principal.user_id},
        )
        created = await self._record([candidate])
        if created:
            return created[0], True
        existing = await self._repository.get(candidate.id)
        if existing is None:
            raise RuntimeError(f"Deduplicated notification {candidate.id} is not readable")
        return existing, False

    def _context(self, session: Session) -> _SessionContext:
        coordination = session.coordination
        return _SessionContext(
            session_id=session.id,
            session_name=session.name,
            owner_id=session.owner_id or "",
            tenant_id=session.tenant_id,
            project_id=str(coordination.project_id) if coordination else None,
            engine=self._engine_resolver(session) if self._engine_resolver else None,
            model=session.model or None,
        )

    @staticmethod
    def _candidate(
        context: _SessionContext,
        *,
        dedupe_key: str,
        draft: NotificationDraft,
        source: NotificationSource,
        session_seq: int | None,
        metadata: dict,
    ) -> NotificationCandidate:
        return NotificationCandidate(
            dedupe_key=dedupe_key,
            session_id=context.session_id,
            session_seq=session_seq,
            session_name=context.session_name,
            owner_id=context.owner_id,
            tenant_id=context.tenant_id,
            project_id=context.project_id,
            kind=draft.kind,
            severity=draft.severity,
            source=source,
            title=draft.title,
            body=draft.body,
            links=draft.links,
            engine=context.engine,
            model=context.model,
            correlation_id=draft.correlation_id,
            metadata=metadata,
        )

    async def _record(self, candidates: list[NotificationCandidate]) -> list[Notification]:
        if not candidates:
            return []
        created = await self._repository.project(candidates)
        for notification in created:
            await self._broadcast(notification)
        return created

    async def _broadcast(self, notification: Notification) -> None:
        if self._broadcaster is None:
            return
        await self._broadcaster.publish(
            RealtimeEvent(
                type=EventType.SESSION_NOTIFICATION,
                data=notification.wire(),
                timestamp=notification.created_at,
            )
        )

    # -- Reading -----------------------------------------------------------

    async def list_feed(self, principal: Principal, query: NotificationQuery) -> NotificationFeed:
        scope = NotificationScope.for_principal(principal)
        watermark = await self._repository.get_watermark(principal.user_id)
        page = await self._repository.list_feed(
            scope, query, read_through_seq=watermark.read_through_seq
        )
        head, unread = await self._repository.feed_counts(
            scope, read_through_seq=watermark.read_through_seq
        )
        return NotificationFeed(
            items=page.items,
            next_before=page.next_before,
            head_seq=head,
            read_through_seq=watermark.read_through_seq,
            unread_count=unread,
        )

    async def list_for_session(
        self, principal: Principal, session_id: UUID, *, after: int, limit: int
    ) -> tuple[list[Notification], int]:
        """A session's notifications (ascending) and the reader's watermark.

        The caller has already authorized ``principal`` for the session.
        """
        watermark = await self._repository.get_watermark(principal.user_id)
        items = await self._repository.list_for_session(session_id, after=after, limit=limit)
        return items, watermark.read_through_seq

    async def read_through_seq(self, principal: Principal) -> int:
        watermark = await self._repository.get_watermark(principal.user_id)
        return watermark.read_through_seq

    async def get_read_state(self, principal: Principal) -> NotificationReadState:
        scope = NotificationScope.for_principal(principal)
        watermark = await self._repository.get_watermark(principal.user_id)
        head, unread = await self._repository.feed_counts(
            scope, read_through_seq=watermark.read_through_seq
        )
        return NotificationReadState(
            read_through_seq=watermark.read_through_seq,
            revision=watermark.revision,
            unread_count=unread,
            head_seq=head,
        )

    async def update_read_state(
        self, principal: Principal, *, read_through_seq: int, expected_revision: int
    ) -> NotificationReadState:
        """Move the reader's watermark forward (compare-and-swap on ``revision``)."""
        scope = NotificationScope.for_principal(principal)
        current = await self._repository.get_watermark(principal.user_id)
        head, _ = await self._repository.feed_counts(
            scope, read_through_seq=current.read_through_seq
        )
        if read_through_seq > head:
            raise NotificationValidationError(
                f"Cannot mark unseen notifications as read (head is {head})"
            )
        watermark = await self._repository.advance_watermark(
            principal.user_id,
            read_through_seq=read_through_seq,
            expected_revision=expected_revision,
        )
        head, unread = await self._repository.feed_counts(
            scope, read_through_seq=watermark.read_through_seq
        )
        return NotificationReadState(
            read_through_seq=watermark.read_through_seq,
            revision=watermark.revision,
            unread_count=unread,
            head_seq=head,
        )

    async def list_deliveries(
        self, principal: Principal, notification_id: UUID
    ) -> list[NotificationDelivery]:
        notification = await self._repository.get(notification_id)
        scope = NotificationScope.for_principal(principal)
        if notification is None or not scope.allows(notification.owner_id, notification.tenant_id):
            raise NotificationNotFoundError(f"Notification not found: {notification_id}")
        return await self._deliveries.list_for_notification(notification_id)

    # -- Rules and sinks ---------------------------------------------------

    async def list_sinks(self, principal: Principal) -> list[NotificationSinkInfo]:
        sinks = list(self._sinks)
        if await self._messaging_connections(principal):
            sinks.append(INTEGRATION_SINK)
        return sinks

    async def list_rules(self, principal: Principal) -> list[NotificationRule]:
        return await self._rules.list_for_owner(principal.user_id)

    async def create_rule(
        self, principal: Principal, spec: NotificationRuleSpec
    ) -> NotificationRule:
        await self._validate_rule(principal, spec)
        now = datetime.now(UTC)
        rule = NotificationRule(
            **spec.model_dump(),
            id=uuid4(),
            owner_id=principal.user_id,
            created_at=now,
            updated_at=now,
        )
        return await self._rules.create(rule)

    async def update_rule(
        self, principal: Principal, rule_id: UUID, spec: NotificationRuleSpec
    ) -> NotificationRule:
        existing = await self._rules.get(principal.user_id, rule_id)
        if existing is None:
            raise NotificationNotFoundError(f"Notification rule not found: {rule_id}")
        await self._validate_rule(principal, spec)
        updated = await self._rules.update(
            NotificationRule(
                **spec.model_dump(),
                id=rule_id,
                owner_id=principal.user_id,
                created_at=existing.created_at,
                updated_at=datetime.now(UTC),
            )
        )
        if updated is None:
            raise NotificationNotFoundError(f"Notification rule not found: {rule_id}")
        return updated

    async def delete_rule(self, principal: Principal, rule_id: UUID) -> None:
        if not await self._rules.delete(principal.user_id, rule_id):
            raise NotificationNotFoundError(f"Notification rule not found: {rule_id}")

    async def _validate_rule(self, principal: Principal, spec: NotificationRuleSpec) -> None:
        if spec.integration_connection_id is None:
            if spec.sink == INTEGRATION_SINK.name:
                raise NotificationValidationError(
                    "The integration sink needs an integration_connection_id"
                )
            if spec.sink not in {sink.name for sink in self._sinks}:
                raise NotificationValidationError(
                    f"Unknown sink {spec.sink!r}; configure it under notifications.sinks"
                )
            return
        connections = await self._messaging_connections(principal)
        if spec.integration_connection_id not in {conn.id for conn in connections}:
            raise NotificationValidationError(
                "integration_connection_id must name one of your enabled messaging integrations"
            )

    async def _messaging_connections(self, principal: Principal) -> list:
        if self._integrations is None:
            return []
        connections = await self._integrations.list_connections(
            principal.user_id, IntegrationType.MESSAGING
        )
        return [conn for conn in connections if conn.enabled]
