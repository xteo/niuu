# Forge session notifications

Forge keeps one durable, cursor-ordered notification feed per node. Sessions, Forge
itself and operators all write into it. The web app, Lexi and external controllers read
it over REST and receive new entries live over the existing session SSE stream. Delivery
rules can forward selected notifications to external sinks, such as Telegram, push or a
webhook, through a durable outbox.

The binding interfaces are in
[`docs/forge/session-notifications-contract.md`](../forge/session-notifications-contract.md).
This page covers how to operate the feature.

## Where notifications come from

| Source | Kind(s) | How it is recorded | Dedupe key |
|---|---|---|---|
| `agent` | milestone, decision, attention, error, info | The model calls the Forge MCP `notify` tool. Skuld appends a notification turn to the session log, and Forge projects it when the log batch is appended. | `turn:{session}:{turn_id}:{kind}` |
| `system` | reply_ready | Every final assistant reply (the same rule that drives the session inbox). The title is the reply's first line and the body is a bounded excerpt. | `turn:{session}:{final_turn_id}:reply_ready` |
| `system` | attention | A session starts waiting for its owner (a question, confirmation or permission request). | `attention:{session}:{state_since}[:{request_id}]` |
| `operator` | any except reply_ready | `POST /api/v1/forge/sessions/{id}/notifications` | `submit:{session}:{principal}:{idempotency_key}` |

The notification id is a uuid5 of the dedupe key, so every retry maps to the same
notification. A retried log append, activity report or submit never creates a second
row, a second delivery or a second live event.

Projection from the session log runs after the idempotent insert and conflict detection,
and before the append is acknowledged. It always reads the stored rows back. A frame that
re-uses a stored seq with a different payload (a `log_conflict`) is never projected. The
insert and the delivery scheduling happen in one transaction, so a committed notification
always has its deliveries scheduled.

The attention key carries the pending `request_id` when the broker reports one. A second
question raised while a session is already waiting keeps the same `state_since`, and
without the request id it would be dropped as a duplicate of the first.

Ownership, meaning the owner, tenant, project, session name, engine and model, is copied
from the session row at projection time. Notifications have no foreign key to sessions, so
they outlive a deleted session.

## Reading the feed

All routes are under `/api/v1/forge`. The Guild facade serves the same paths.

| Method and path | Purpose |
|---|---|
| `GET /notifications` | The caller's feed. See the query parameters below. |
| `GET /notifications/read-state` | `{read_through_seq, revision, unread_count, head_seq}` |
| `PUT /notifications/read-state` | `{read_through_seq, expected_revision}`. The watermark only moves forward. A stale `expected_revision` returns `409`. A `read_through_seq` beyond the newest notification you can see returns `422`. |
| `GET /sessions/{id}/notifications?after=&limit=` | One session's notifications as a JSON list, ascending. Requires read access to the session. |
| `POST /sessions/{id}/notifications` | Direct submit: a `NotificationDraft` plus `idempotency_key`. Returns `201` for a new notification, `200` for a deduplicated one. `session_seq` is always null. Requires the session's `emit_event` permission, the same as appending to its log. |
| `GET/POST /notifications/rules`, `PUT/DELETE /notifications/rules/{id}` | Your own delivery rules. |
| `GET /notifications/sinks` | Sinks a rule may name: `[{name, label, requires_integration}]` |
| `GET /notifications/{id}/deliveries` | Outbox rows for a notification you can see. |

`GET /notifications` accepts these query parameters:

- `limit`: defaults to `notifications.default_page_size`, up to `max_page_size`.
- One cursor, never both:
  - `before=<seq>` pages newest first. The response's `next_before` is the next page's
    cursor, or null at the end.
  - `after=<seq>` pages ascending, for gap-fill after a reconnect.
- Filters: `kind` and `source` (comma-separated), `min_severity`, `session_id`,
  `project_id`, and `unread=true|false`. Unknown values return `422`.
- The response is `{items, next_before, head_seq, read_through_seq, unread_count}`. Each
  item carries `read = seq <= read_through_seq`. The counters cover your whole scope and
  ignore the filters.

**Scope.** A non-admin sees only notifications they own. A `volundr:admin` also sees every
notification in their own tenant, plus untenanted ones. The feed, the SSE filter and the
Guild facade all apply this one rule (`niuu.domain.notifications.notification_visible_to`).

**Ordering.** `seq` is a per-node `BIGSERIAL`. Each projection transaction takes a
transaction-scoped advisory lock before it inserts, so seqs become visible in increasing
order. A reader paging with `after=<seq>` therefore never skips a notification that
committed late. `created_at` is set from `clock_timestamp()` after the lock is taken, so it
increases with `seq`.

## Live events

A new notification is broadcast on `GET /api/v1/forge/sessions/stream` as
`event: session_notification`. Its `data` is the notification without `read`. Forge sends
it only to the owner, or to an admin of the notification's tenant. A client without an
identity gets no notification events. Other event types are unchanged.

Every node applies the same rule. The Guild facade also re-checks each event before
forwarding it, so an older node cannot leak another owner's notification into the fleet
stream.

The stream is not durable. After a reconnect, fill the gap with
`GET /notifications?after=<last seen seq>`.

When Sleipnir is enabled, each new notification is also published as
`volundr.session.notification`. The payload is the notification, and its urgency follows
the severity: `critical` > `warning` > `info`/`success`. Consumers must honour `owner_id`.

## The Guild facade (all hosts)

Without `all_instances=true`, requests go to the host named by `instance_id`, or else to
the default (local) host. `scope=local` pins the embedded host.

`GET /notifications?all_instances=true` fans out to every visible host. It merges their
pages newest first by `(created_at, instance_id, seq)`, and each host's rows keep their
seq order.

- **Cursors** are opaque: unpadded base64url JSON of `{instance_id: seq}`.
  - `next_before` pages older. A host that is missing from it has nothing older left, and
    a `null` position means "from that host's newest".
  - `after` takes the same shape for gap-fill. Only the hosts named in an `after`
    cursor are read; send a host with seq `0` to read it from the start.
  - `next_after` is the gap-fill cursor to keep. It holds the newest seq per host.
- **Response fields.**
  - `unread_count` is summed across hosts, and the per-host counters are under
    `instances`.
  - The top-level `head_seq` and `read_through_seq` are `null`, because seqs are per
    host.
  - Each item carries `instance_id`, `instance_name` and `instance_slug`.
- **Failures.**
  - Hosts that fail or answer garbage are listed in `X-Forge-Unavailable-Instances`.
    They keep their cursor position so the next page retries them.
  - If every host fails, the response is `502`.
  - A request error that every host would repeat, such as a bad filter, is returned as
    is.

Read state works the same way:

- `GET /notifications/read-state?all_instances=true` returns
  `{unread_count, instances: {id: state}}`.
- `PUT /notifications/read-state` accepts either the single-host body or
  `{"instances": {id: {read_through_seq, expected_revision}}}`. If any host reports a
  revision conflict, the response is `409` with
  `detail: {conflicts: [...], instances: {...applied}}`.

Session routes go to the host that owns the session. Rules and sinks go to the selected
host (local by default). Deliveries are looked up on the host that holds the
notification.

## Delivery rules and the outbox

A rule selects notifications from its owner and names a sink:

```json
{"name": "Decisions to Telegram", "enabled": true,
 "match": {"kinds": ["decision", "attention"], "min_severity": "warning",
           "sources": [], "project_ids": [], "session_ids": []},
 "sink": "integration", "integration_connection_id": "conn-123",
 "config": {"rate_limit": {"max_count": 10, "window_seconds": 3600}},
 "quiet_hours": {"start": "22:00", "end": "07:00", "timezone": "Europe/London",
                 "allow_min_severity": "critical"}}
```

- In `match`, an empty list means "any".
- `sink` must be one of the configured `notifications.sinks` names. The exception is a
  rule that sets `integration_connection_id`: that must be one of the caller's enabled
  messaging integrations, and its credentials and adapter are used.
- `quiet_hours` is a daily window in an IANA timezone. A window whose start is after its
  end spans midnight.
- `config.rate_limit` caps the deliveries per rule within a window.

When a notification is recorded, every matching enabled rule gets a `pending` row in
`forge_notification_deliveries`. The dispatcher claims due rows with
`FOR UPDATE SKIP LOCKED` and a lease, then settles each one as `delivered`, `failed`
(retried with exponential backoff), `dead` (after `max_attempts`), or `suppressed` (held
back by quiet hours or the rate limit). The row is kept for audit either way.

- Quiet hours and rate limits are evaluated when a row is claimed, not when it is
  scheduled.
- Every settle is fenced by `(delivery id, attempts)`, so a worker whose lease expired
  cannot overwrite a newer claim.

Nothing is delivered externally unless a sink is configured and a rule selects it.

Two pieces come from the delivery workstream (contract §7):

- the dispatcher loop that claims and settles outbox rows;
- the sink adapters under `niuu.adapters.notifications`.

Until they are deployed, matching rules leave their rows `pending`, and the dispatcher
picks those rows up when it starts.

## Configuration

```yaml
notifications:
  enabled: true              # projection, the API and the SSE event
  reply_ready:
    enabled: true            # a reply_ready for every final reply
    title_chars: 120         # <= 200 (protocol limit)
    body_chars: 280          # <= 4000 (protocol limit)
  default_page_size: 50
  max_page_size: 200
  dispatcher:                # used by the outbox dispatcher
    poll_interval_seconds: 2.0
    batch_size: 50
    lease_seconds: 60
    max_attempts: 8
    backoff_base_seconds: 5
    backoff_max_seconds: 900
    max_error_chars: 2000
  sinks:                     # dynamic adapters: name, label, adapter, then kwargs
    - name: ops-webhook
      label: Ops webhook
      adapter: niuu.adapters.notifications.webhook.WebhookNotificationAdapter
      url: https://hooks.example.com/forge
```

Sink names use lowercase letters, digits, `.`, `_` and `-`. `integration` is reserved.
In Helm, set `notifications:` in `values.yaml`. It is rendered verbatim into the Volundr
config. `GET /api/v1/forge/feature-flags` reports `notifications_enabled` and
`capabilities.notifications`.

## Storage

Migration `000069_forge_notifications` creates four tables:

- `forge_notifications`, indexed on `(owner_id, seq)`, `(session_id, seq)`,
  `(project_id, seq)` and `(kind, seq)`;
- `forge_notification_read_states`, one watermark per reader;
- `forge_notification_rules`;
- `forge_notification_deliveries`, which cascades from both its notification and its
  rule, and is indexed on `(status, next_attempt_at)`.

The migration ships in `migrations/`, in the Helm migrations configmap, and in the CLI
bundle (`src/cli/migrations/volundr/`). Mini mode applies it at startup.

To check it against a disposable PostgreSQL database, run:
`FORGE_HISTORY_TEST_DATABASE_URL=postgresql://… pytest -m integration tests/test_adapters/test_notifications_postgres_integration.py`.
The `database` lane of `scripts/verify_forge.py` includes it.
