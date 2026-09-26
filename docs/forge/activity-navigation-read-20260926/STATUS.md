# Individual activity reads and exact reply anchors — local validation

September26,2026. User explicitly approved a per-activity read API. This source
also corrects durable reply IDs lost by observation-order display fragmentation.
No live server, migration, running gateway or other runner's worktree was changed.

## Behaviour

- `PUT /api/v1/forge/notifications/{id}/read?instance_id=<host>` acknowledges exactly
  one visible item for the current reader. Idempotent receipt, no watermark advance,
  no cross-host fan-out, no other reader affected, missing/invisible item404.
- Item marks and mark-all share the reader revision/row lock. Repeated acknowledgements
  do not increment revision; a concurrent item read invalidates an old mark-all CAS.
  Feed and session read flags, unread filters/counts, and Guild revision summaries agree.
- Migration000071 is additive and mirrored in local SQL, packaged CLI migrations,
  and Helm configmap. It cascades only a deleted notification's individual marks.
- Projection fragments retain durable source-turn provenance. Exact IDs win; otherwise
  only a unique terminal fragment can resolve a source ID. The response carries the
  requested ID and the resolved row's absolute index. No title/time/log-seq guessing.
- Earlier compact MCP authoring guidance113acac1 remains included.

## Validation

[266 focused tests](TESTS.txt),98.57% branch/statement coverage across the six focused
notification/projection/policy modules;85% gate retained. All modified REST/gateway
paths are exercised via HTTP/mocked infrastructure, including archive and live gateway
anchor windows. [243 replay/archive/MCP regressions](REGRESSIONS.txt) and
[9 migration parity/DDL checks](MIGRATIONS.txt) pass. Zero reported warnings; Ruff
check/format and `git diff --check` pass.

[Wheel source and packaged-migration inspection](BUILD.json) passes. PostgreSQL SQL
is checked against mocked asyncpg as required; no database was started or mutated.
The opt-in native PostgreSQL tests now include concurrent individual acknowledgements,
reader isolation, CAS and cascade/reversibility. They were **not run**: a disposable
CI database is required. Full repository CI is not represented as passing.

## Rollout boundary

This is **not deployed**. Recheck migration numbering against the integration target
before any authorized rollout. Update both Forge and the owning Skuld gateway for
source-turn aliases. Already-running older gateways and projected-only archives
without source provenance can still return404. Do not restart active sessions just
to refresh code; arrange an authorized recovery window. A missing anchor is explicit,
never falsely reported as the linked message. Item-read capability is independent of
whether that session's old gateway supports reply aliases.

Source branch `xteo/activity-navigation-read-20260926`, isolated worktree
`/home/thor/repos/worktrees/niuu-activity-navigation-read-20260926`.
Worker `thor:c8103dc7-a834-5e32-a06d-1a98d7a8e2b5`. Native app follow-up lives in
`/home/thor/repos/worktrees/lexi-ios-activities-20260926` and remains unshipped.
