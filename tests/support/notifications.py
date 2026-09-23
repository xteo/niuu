"""In-memory notification repositories for service and REST tests.

They implement the same semantics as the Postgres adapters (dedupe by key,
monotonic seqs, rule evaluation on new rows only, forward-only watermark CAS) so
tests can exercise the domain without a database.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from volundr.domain.notification_ports import (
    NotificationDeliveryRepository,
    NotificationRepository,
    NotificationRuleRepository,
)
from volundr.domain.notifications import (
    ClaimedDelivery,
    DeliveryStatus,
    Notification,
    NotificationCandidate,
    NotificationDelivery,
    NotificationPage,
    NotificationQuery,
    NotificationReadStateConflictError,
    NotificationRule,
    NotificationScope,
    ReadWatermark,
    rules_matching,
)


class InMemoryNotificationStore:
    """Shared state; ``feed``, ``rule_repo`` and ``outbox`` are the three ports."""

    def __init__(self) -> None:
        self.notifications: dict[str, Notification] = {}
        self.rules: dict[UUID, NotificationRule] = {}
        self.deliveries: dict[UUID, NotificationDelivery] = {}
        self.watermarks: dict[str, ReadWatermark] = {}
        self.project_calls = 0
        self.seq = 0
        self.feed = InMemoryNotificationRepository(self)
        self.rule_repo = InMemoryNotificationRuleRepository(self)
        self.outbox = InMemoryNotificationDeliveryRepository(self)


class InMemoryNotificationRepository(NotificationRepository):
    def __init__(self, store: InMemoryNotificationStore) -> None:
        self.store = store

    async def project(self, candidates: list[NotificationCandidate]) -> list[Notification]:
        store = self.store
        store.project_calls += 1
        created: list[Notification] = []
        for candidate in candidates:
            if candidate.dedupe_key in store.notifications:
                continue
            store.seq += 1
            notification = Notification(
                **candidate.model_dump(),
                id=candidate.id,
                seq=store.seq,
                created_at=datetime.now(UTC),
            )
            store.notifications[candidate.dedupe_key] = notification
            created.append(notification)
        enabled = [rule for rule in store.rules.values() if rule.enabled]
        now = datetime.now(UTC)
        for notification in created:
            for rule in rules_matching(notification, enabled):
                delivery = NotificationDelivery(
                    id=uuid4(),
                    notification_id=notification.id,
                    rule_id=rule.id,
                    sink=rule.sink,
                    status=DeliveryStatus.PENDING,
                    attempts=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
                store.deliveries[delivery.id] = delivery
        return created

    def _visible(self, scope: NotificationScope) -> list[Notification]:
        return [
            n for n in self.store.notifications.values() if scope.allows(n.owner_id, n.tenant_id)
        ]

    async def get(self, notification_id: UUID) -> Notification | None:
        return next((n for n in self.store.notifications.values() if n.id == notification_id), None)

    async def list_feed(
        self, scope: NotificationScope, query: NotificationQuery, *, read_through_seq: int
    ) -> NotificationPage:
        items = self._visible(scope)
        severities = query.severities()
        items = [
            n
            for n in items
            if (not query.kinds or n.kind in query.kinds)
            and (not severities or n.severity in severities)
            and (not query.sources or n.source in query.sources)
            and (query.session_id is None or n.session_id == query.session_id)
            and (query.project_id is None or n.project_id == query.project_id)
            and (query.unread is None or (n.seq > read_through_seq) == query.unread)
            and (query.before is None or n.seq < query.before)
            and (query.after is None or n.seq > query.after)
        ]
        items.sort(key=lambda n: n.seq, reverse=not query.ascending)
        page = items[: query.limit]
        more = len(items) > query.limit and not query.ascending
        return NotificationPage(items=page, next_before=page[-1].seq if more else None)

    async def list_for_session(
        self, session_id: UUID, *, after: int, limit: int
    ) -> list[Notification]:
        items = [
            n
            for n in self.store.notifications.values()
            if n.session_id == session_id and n.seq > after
        ]
        return sorted(items, key=lambda n: n.seq)[:limit]

    async def feed_counts(
        self, scope: NotificationScope, *, read_through_seq: int
    ) -> tuple[int, int]:
        items = self._visible(scope)
        head = max((n.seq for n in items), default=0)
        return head, sum(1 for n in items if n.seq > read_through_seq)

    async def get_watermark(self, user_id: str) -> ReadWatermark:
        return self.store.watermarks.get(user_id, ReadWatermark())

    async def advance_watermark(
        self, user_id: str, *, read_through_seq: int, expected_revision: int
    ) -> ReadWatermark:
        current = self.store.watermarks.get(user_id, ReadWatermark())
        if current.revision != expected_revision:
            raise NotificationReadStateConflictError("changed")
        updated = ReadWatermark(
            read_through_seq=max(current.read_through_seq, read_through_seq),
            revision=current.revision + 1,
        )
        self.store.watermarks[user_id] = updated
        return updated


class InMemoryNotificationRuleRepository(NotificationRuleRepository):
    def __init__(self, store: InMemoryNotificationStore) -> None:
        self.rules = store.rules
        self.deliveries = store.deliveries

    async def list_for_owner(self, owner_id: str) -> list[NotificationRule]:
        return [rule for rule in self.rules.values() if rule.owner_id == owner_id]

    async def get(self, owner_id: str, rule_id: UUID) -> NotificationRule | None:
        rule = self.rules.get(rule_id)
        return rule if rule is not None and rule.owner_id == owner_id else None

    async def list_enabled_for_owner(self, owner_id: str) -> list[NotificationRule]:
        return [rule for rule in await self.list_for_owner(owner_id) if rule.enabled]

    async def create(self, rule: NotificationRule) -> NotificationRule:
        self.rules[rule.id] = rule
        return rule

    async def update(self, rule: NotificationRule) -> NotificationRule | None:
        current = self.rules.get(rule.id)
        if current is None or current.owner_id != rule.owner_id:
            return None
        self.rules[rule.id] = rule
        return rule

    async def delete(self, owner_id: str, rule_id: UUID) -> bool:
        current = self.rules.get(rule_id)
        if current is None or current.owner_id != owner_id:
            return False
        del self.rules[rule_id]
        for delivery_id in [d.id for d in self.deliveries.values() if d.rule_id == rule_id]:
            del self.deliveries[delivery_id]
        return True


class InMemoryNotificationDeliveryRepository(NotificationDeliveryRepository):
    """Only the read side; claims and settles are exercised against PostgreSQL."""

    def __init__(self, store: InMemoryNotificationStore) -> None:
        self.deliveries = store.deliveries

    async def claim_due(self, *, limit: int, lease_seconds: float) -> list[ClaimedDelivery]:
        raise NotImplementedError("claims are exercised against PostgreSQL")

    async def mark_delivered(self, delivery: NotificationDelivery) -> bool:
        raise NotImplementedError("settles are exercised against PostgreSQL")

    async def mark_failed(self, delivery, *, error, retry_at) -> bool:  # type: ignore[override]
        raise NotImplementedError("settles are exercised against PostgreSQL")

    async def mark_suppressed(self, delivery, *, reason) -> bool:  # type: ignore[override]
        raise NotImplementedError("settles are exercised against PostgreSQL")

    async def list_for_notification(self, notification_id: UUID) -> list[NotificationDelivery]:
        return [d for d in self.deliveries.values() if d.notification_id == notification_id]
