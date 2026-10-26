# Consolidation of `lexi/dev-api-integration` onto upstream `dev` (LEX-1483, 2026-10-26)

`lexi/dev-api-integration` on xteo/niuu is now upstream `niuulabs/niuu` `dev`
(**6c606cb27**, 2026-10-25, re-fetched 2026-10-26 11:00 UTC and unchanged) plus our
work, as a linear history. The previous tip **33087ca2e** is kept as
`backup/dev-api-integration-20261026` on xteo/niuu.

Every candidate was patch-id checked against upstream `dev` (`git cherry`); none was
already upstream. Production code was read from the commits, not from branch names.

## 1. Folded (in branch order)

| New | Source | What | Notes |
|---|---|---|---|
| 7042e75d3 | a0100520a | attachments index route | clean |
| 47d88598f | acd6cf7a0 | bounded incremental fold of the index | clean |
| 5302fbd37 | 3e603dd69 | agent-maintained session descriptions | migration renumbered **000087 → 000089** + alias; chart/CLI copies dropped (upstream reads `migrations/` only, 57f53a965); INSERT column order kept so `actor_id` (upstream 000087) stays last |
| 2fde9a6ac | 33087ca2e | session descriptions e2e test + contract doc | references to 000089 |
| 1ba9143e5 | b2bc0e71e | notify accepts an image reference | clean |
| a87955bd4 | 61ad73d60 | how Forge reaches a session broker (refactor) | import-only conflict |
| 44b25de33 | b1e1db7ee | durable copy of notification images | migration renumbered **000072 → 000090** + alias; see §3 for the one behavioural merge |
| d3539a3ea | 16648708e | images through the Guild facade | test adapted to upstream's `session_event_stream` (as m6's b46c5e68b) |
| 1e4bda358 | 2a215f303 | docs: notification images | section placed after "Finding the Guild from a node" |
| a62b923eb | 272367f0e | `conversation?tail=N` | see §3 |
| f68f2dc80 | 4564b3b61 | tail overrides a protocol-2 page request | clean |
| daf63c6ad | 0166eaf85 | exclude the Forge capture secret from child environments | clean |
| 5fad8ac10 | f64f56753 | isolate Claude subscription file authentication | clean |
| 9ebc61c3e…b2fff5828 | 5dc2407db, 9d60f7245, 5a3137494, b67d95c65, abded9e21, d476938ac, d8988ac7c | `xteo/codex-subagents-workflow-20260914`: Codex native-agent research, capability matrix, proposed API | docs only; not superseded upstream; scanned for credentials (none) |

The m6 line's b46c5e68b (image tests on the newer identity adapters) is folded into
44b25de33/d3539a3ea as test adaptations, not as a separate commit; its sibling
fedd6ec65 (images renumbered to 000087 on m6) is superseded by 000090.

## 2. Dropped, with the reason

| Commit(s) | Branch | Reason |
|---|---|---|
| 11afe41d6, 340240a49, 89d65155d | `forge/session-metadata-thor-20261016` | Thor release ports of a0100520a+acd6cf7a0 / 3e603dd69+33087ca2e onto Thor's old base, plus the merge of notification images. Same features; folded from the originals (range-diff: only base adaptations). |
| 1895f5fc5, 8cc2e1650 | `forge/session-metadata-m6-20261016`, `forge/attachments-index-m6-20261016` | m6 ports of the same; content-identical to ours apart from base context. |
| fedd6ec65, b46c5e68b | `forge/notification-images-m6-20260930` | renumber superseded by 000090; test adaptation folded (above). |
| b331d5e6e | under the Thor release | already upstream (patch-id equal). |
| 121962181, 27e10086d, 03550521c, 31906a0a3 | `forge/ux-cleanup` (09-22) | A parallel *public image* pipeline: a scrub of names and fixtures across 87 files (13k-line fixture deletion, screenshots removed, "Damien"→"user" in comments), a packaged-build manifest that overrides build identity, image scan/smoke scripts and a `forge-public-image` workflow. Upstream's ci/* work since 10-17 (parallel image builds, Syft/Grype scan with cached DB, promote-by-tree, `secrets.yaml`) owns building, scanning and publishing now, and the public-image workflow last ran 09-22. The scrub is a publishing policy, not integration-branch content. Recoverable at `origin/forge/ux-cleanup` 31906a0a3. **Note:** Thor's *web* UI (`niuu-forge-web.service`) is still served from a static build of this branch (`forge-ux-cleanup-31906a0a-uxstyles-ae19f223`); B3 should rebuild the web from the consolidated branch. |
| 9ca2aae49, 301934f3e, 4e37a04e3, 167fb09fa, 715b07031, 1a6363709, 941ff0ce8, cea541693 | `forge/ux-improvement` (09-22) | The earlier variant of the same public-image pipeline on an older base (+ two rollout design docs for container releases that describe that pipeline). Superseded by upstream's ci/* image work as above. Recoverable at `origin/forge/ux-improvement` cea541693. |
| `exp/control-plane` feature commits | | Not this ticket: the B1 lifts (LEX-1484/1485/1486) land after this, one ticket each. |
| side-quest defaults (000091), cp store/ledger/manifest, deploy-request docs (incl. a47c7a5a7) | `exp/control-plane` | CP-only, dropped for good (plan 09). |

## 3. Merges that touched behaviour (both sides described)

1. **Notification live events and Guild instance fields** (44b25de33). Upstream now stamps
   the node's Guild instance (`instance_id/_name/_slug`) on every live
   `session_notification`. The images commit routes every broadcast through
   `notification_event()`, and the image worker announces image notifications only once
   their image settles. Kept both: `notification_event(notification, instance_fields)`
   and the worker receives the same `GuildInstance` as `NotificationService`, so an image
   notification's (delayed) live event has the feed's shape. Test added
   (`test_settled_image_announcement_carries_the_guild_instance`). Without this, image
   notifications would have been the only live events without instance fields.
2. **Conversation `?tail=N` and upstream's `DurableTranscripts`** (a62b923eb). Upstream
   serves durable reads through a single-flight cache (shallow cached per seq, full
   single-flight). Kept both: without `tail` the path is exactly upstream's; with `tail`
   it is exactly our read (`forge.get_transcript(..., tail_turns=N)`, bounded doubling
   windows capped at 50k frames). **Open question:** tail reads are neither cached nor
   single-flight. They read at most the same window as a full rebuild, but several
   concurrent `tail` polls on one stopped session rebuild concurrently. Routing tail
   through `DurableTranscripts` is a small follow-up if wanted.

## 4. Migrations

Volundr's history is keyed by filename + sha256 (`volundr_schema_history`);
`migrations/lineage-aliases.json` maps a renumbered file to already-applied filenames
(identical SQL required). Added:

| Canonical file | Alias (applied filename) | sha256 |
|---|---|---|
| `000089_session_descriptions.up.sql` | `000087_session_descriptions.up.sql` (Thor, 10-16) | 3d47e0afee0c560b32bc66ae6d9857cd277806564ae748d55917d77b5ba99b88 |
| `000090_forge_notification_images.up.sql` | `000072_forge_notification_images.up.sql` (Thor, 09-30) | cd39abfca6355c64df73f10a1b5ae9c0933cc8cfebf43a6962993a2b2e5565e4 |

Up/down pairs exist for both; the Volundr chart reads `migrations/` through its symlink
(upstream's single-source rule), so there is no configmap copy to keep in sync.
`tests/test_charts/test_migrations_single_source.py` and the per-feature migration tests
(`test_session_descriptions_migration.py`, `test_notifications_migration.py`) check the
alias entries and the sha256 of the renumbered files.

### Dry run (scratch Postgres, 127.0.0.1 only, removed afterwards)

Each host's **schema-only** dump (`pg_dump --schema-only`, read-only) was restored into
a scratch container of the host's Postgres major (Thor 16, m6 17), its
`volundr_schema_history` rows (filename, sha256, applied_at; read-only export) were
loaded, and the consolidated runner (`niuu.adapters.postgres_schema.apply_startup_migrations`
with `cli.resources.ordered_migration_files(migrations/)`) was run twice. The second run
was a no-op on both. Driver: see the LEX-1483 report; results below.

**Thor** (`volundr-pg`, Docker PG 16, external DB; Volundr stream only, see below):

- already applied: the existing ledger rows
- adopted by alias (ledger row added, SQL not executed): 000050, 000051, 000052, 000069,
  000070, 000071, 000072_startup_schema_history, 000073, 000074, 000075, 000084, 000085,
  000086, **000089_session_descriptions**, **000090_forge_notification_images**
- executed (upstream files Thor's 09-28 base predates): 000062_pat_authority,
  000063_topology_source_authority, 000064_instance_tenant_attribution,
  000065_admin_settings, 000066_compute_leases, 000067_compute_pools,
  000068_compute_profile_revision, 000076_chronicle_attribution, 000077_instance_health,
  000078_session_span_w3c_trace_id, 000079_resident_realm_binding,
  000080_session_participants, 000081_ravn_trigger_store, 000082_ravn_budget_ledger,
  000083_guild_node_join, 000087_session_actor, 000088_workload_delegations
- data-dependent predicates checked read-only against production: 000064's CHECK on
  `niuu_instances` (0 rows, 0 violations); 000076 backfills nullable columns on 528
  chronicles; the unique indexes in 000066/000083/000088 are on new tables or new
  nullable columns.

**m6** (embedded PG 17, `niuu platform up`; Volundr + `ting/` streams):

- Volundr executed: 000087_session_actor, 000088_workload_delegations,
  **000089_session_descriptions**, **000090_forge_notification_images**; nothing aliased
  (m6 never applied our old numbers).
- Ting executed: `ting/000047_a2a_campaign_launch_slug`.

**Hazard for B3 (Thor):** Thor's start script (`~/.config/niuu-forge-thor/start_platform.py`)
replaces `RootServer.start`, so the root server's own migration pass never runs there.
Only Volundr's startup migrations run, which is why Thor's ledger has no `ting/` rows
although its database holds Ting tables. Upstream's root server now also migrates an
external database (`NIUU_DATABASE_MODE=external`) *including* the Ting stream. Run on
Thor's schema copy, that stream fails at `ting/000025_legacy_schema_compat.up.sql`
(`column "depends_on" does not exist`). So B3 must keep Thor's start script (or seed
the `ting/` ledger first) and must not switch Thor to the native external mode.

## 5. Thor vs m6 variants

- The `forge/*-thor-20261016` and `forge/*-m6-20261016` commits are the **same features
  ported onto two different bases**, not host configuration. range-diff: the m6 port is
  content-identical to ours; the Thor port differs only in tests adapted to its older base
  (the images-migration ordering test, a broadcaster fixture, an absent token-scope test).
  No ports, paths, hosts or credentials live in those commits. The one numbering
  difference (images 000072 on Thor, 000087 on the m6 images branch) is resolved by
  000090 + the alias.
- Host differences live in host files outside the repo and stay there:
  - **Thor**: release worktree `~/.local/share/niuu/releases/forge-session-metadata-20261016`
    (11afe41d6), unit `volundr-forge.service` + drop-ins (`PROJECTS__INSTANCE_ID=thor`,
    `NIUU_BUILD_VERSION`, `PYTHONPATH` to the release), `~/.config/niuu-forge-thor/`
    (`config.yaml`, `start_platform.py`: external Docker PG `volundr-pg`, tailnet bind
    100.66.123.128:8080, service DB provisioning); web from a static build (see §2).
  - **m6**: LaunchAgent `world.niuu.forge.m6` → `~/.local/share/niuu-forge/forge-run.sh`
    → `niuu platform up --mounts …`, embedded PG 17 in `~/.local/share/niuu-forge/pgdata`,
    config `~/.config/niuu-forge/config.yaml`.
- **Finding:** m6 production runs **forge-m6-6c31bfe1** (the 09-29 Work release), not
  1895f5fc5. The 1895f5fc5 release directory exists but `forge-run.sh` still points at
  6c31bfe1. m6's ledger ends at upstream's 000086: no session descriptions, no
  attachments index, no notification images are live on m6.
- So one branch serves both hosts. Each promotion is: new release worktree at the
  consolidated sha, then the host's existing unit/LaunchAgent and config pointed at it.
  No host-specific commits are needed.

## 6. Validation (2026-10-26, Thor, clean env: `env -i` allow-list, `uv sync --frozen --all-extras --dev`)

| Gate | Result |
|---|---|
| `make lint` (ruff check + format) | pass |
| full backend suite (`pytest tests/ -n 16 --cov=src --cov-fail-under=85`) | **28315 passed**, 16 skipped, 1 xfailed, **14 failed**, coverage **88.89 %** |
| `make test-forge` (unit lane, `-W error`, Skuld coverage gate) | **9095 passed**, 3 skipped, **4 failed**, Skuld coverage 87.58 % |
| `make test-forge-tmux` | **60 passed** |
| `make test-forge-database` (scratch PG 16 on 127.0.0.1) | **88 passed** |
| session descriptions real-PG integration (000089 + targeted write) | **4 passed** |
| `helm lint charts/volundr` + `helm template` | pass; renders 000089/000090, the alias manifest and the tail knobs |
| migration dry run (Thor + m6 copies, §4) | pass, second run a no-op |
| web lane | not run: `git diff upstream/dev..HEAD -- web-next` is empty |

**Every remaining failure also fails on pure upstream `dev` 6c606cb27 on this box** (each
was re-run in a detached upstream worktree):

- 14 in the full suite: `test_scripts/test_install_sh*` (10: the GB10 GPU without a
  Docker NVIDIA runtime), `test_docker_container` recovery, `ravn/unit/test_terminal_docker`
  workspace mount, `test_ravn/test_workflow_kickoff[/workspace]` (host path),
  `test_volundr/test_api_release_guard` (a zombie process on the host).
- 4 in the forge unit lane: the docker-container and release-guard tests above, plus
  two `test_resident_notification_relay` tests that fail under `-W error` on an unclosed
  sqlite connection garbage-collected mid-test. Upstream fails the same file the same way
  (`TestComposition::test_on_by_default_with_the_feed`; which test the GC hits varies).

The first full run had 23 more failures, all ours and fixed in 44b25de33: upstream's newer
resident-notification and Guild tests built `NotificationService` and the notifications
router without the image ports (`image_repository`, `image_scheduler`,
`image_cache_max_age_seconds`), which the images commit makes required. They stay
required rather than optional (no fallbacks).

`git log --oneline upstream/dev..HEAD` lists the 20 commits of §1, then this document.

## 7. Open questions

1. Conversation `tail` reads bypass `DurableTranscripts` (§3.2). Leave as is, or route them
   through it?
2. B3, Thor: keep `start_platform.py` (§4 hazard) and rebuild the web UI from this branch
   (it is a `forge/ux-cleanup` static build today).
3. B3, m6: m6 is two releases behind (6c31bfe1); its promotion applies 4 Volundr
   migrations + 1 Ting migration and is the first time m6 gets descriptions, attachments
   and images.
4. `forge/ux-cleanup` / `forge/ux-improvement` dropped: if a public Forge image is still
   wanted, it should be rebuilt on upstream's ci/* pipeline rather than revived.

---

# B1a — session work facts + usage status (LEX-1484, 2026-10-26)

Branch `lexi/b1a-work-facts` from `lexi/dev-api-integration` 20c639ee0 (upstream `dev`
re-fetched 2026-10-26, still 6c606cb27, so no rebase was needed), fast-forwarded onto
`lexi/dev-api-integration`. Patch-id check (`git cherry`): neither source commit was on
the branch.

## B1a.1 Taken

| New | Source (`exp/control-plane`) | What |
|---|---|---|
| c1bde8848 | b24c6d586 | G10/F12: stale-active fixes in Skuld (F12a turn-boundary flush is not a turn start, F12b `idle_prompt` heals a stale busy state, F12c heartbeat re-reports busy only while the transport produced a row within `activity_heartbeat.stale_output_seconds`), work facts folded at log ingest (`last_work_at`, `open_tool_count`), the broker's `open_controls`, the shared predicate `niuu.domain.work_facts`, the 20 vendored derived-state goldens (`tests/fixtures/control_plane`, ruff-excluded so they stay byte-identical) |
| 5f6a9efa9 | 1f3ecf78b (work-facts part only) | review fixes: no guessed idle for a transport without its own turn flag, `update()` returns the stored facts, no session write for frames that change no fact, hook-name lookup matches the ingest SQL, the refreshed attention detail names the gate still open. The commit's presented-file changes (CSP/nosniff, durable store, upload timeouts) are left for B1b |
| 4352e6ea1 | 43ce88cb4 (LEX-1421) | native usage projection: Claude SDK, tmux JSONL and Codex (app-server + exec) usage events from `src/skuld/transports`, deduplicated per native response and projected once at durable log ingest (`volundr.domain.session_usage`), `GET /sessions/{id}/status`, `context` on the session row and on `session_updated`, the Guild facade route. Its STATUS/DEPLOY-REQUEST docs are not taken |
| 99e23570d | (new) | database lane runs the usage real-PG test; history-import test fixtures and the append-order test brought up to the lift |

Not taken (other lifts / dropped for good, per the brief): presented files and
publications (B1b), structured asks and the notify catalog (B1c, incl. 93dc04f64,
776666546, f65d3b8f6), live model (B1d), side-quest defaults, the cp store, R16/R17
exp-seed reads (a44c1c661), the F8 probe, and the control-plane `HostIdentityConfig`
(F7) that 43ce88cb4's `main.py` read.

## B1a.2 Adapted

1. **Migrations single-source.** Upstream reads `migrations/` only, so the CLI copies
   (`src/cli/migrations/volundr/`) and the chart configmap hunks from both commits were
   dropped; the chart picks the files up through its `migrations/` glob. The exp
   "three copies match" tests became single-source tests (number unique, follows its
   predecessor, idempotent up / reversing down).
2. **Session INSERT and `actor_id`.** Upstream's 000087 added `coordination, actor_id`
   at the end of the INSERT; `session_usage` is inserted before them so `actor_id`
   stays last (as for descriptions in B0). Tests indexing the argument tail follow.
3. **Status `host` on an embedded Forge** comes from `projects.instance_id` (Thor sets
   `PROJECTS__INSTANCE_ID=thor`); exp read `host_identity.host` first, which is F7 and
   not part of this lift. Through the Guild facade `host` is the owning instance id,
   unchanged.
4. **Workload delegation / session credentials.** `GET /api/v1/forge/sessions/{id}/status`
   carries `require_scope(forge:session:read)`, so it joins upstream's `forge_session`
   allow-list as an owner-scoped read (`Binding.OWNED`, like `GET /sessions/{id}`);
   without it a session broker's token would get 403 at the edge while the route
   advertised the read scope. Human callers are unaffected.
5. **Append order (behaviour, both sides kept).** Upstream sorts an append batch by
   session (stable, for deadlock-free row locks) and kept the caller's order within a
   session; the usage commit sorts by `(session, seq)`. Kept the session order and the
   seq order within a session, because the open-tool count and the usage checkpoints
   are order-sensitive folds. Upstream's `test_append_uses_same_session_lock_in_stable_session_order`
   now asserts seq order within the session. The exp branch had left that test failing.
6. **Notification outbox.** Nothing in B1a writes notifications; usage and work facts
   reach clients through the existing `session_updated` stream only, so the lift does
   not meet upstream's notification outbox.
7. The B0 open question on conversation `tail` reads vs `DurableTranscripts` (§7.1) is
   untouched by this lift and **remains open**.

## B1a.3 Migrations

| File | exp name | What |
|---|---|---|
| `000091_session_work_facts` | `000088_session_work_facts` | `sessions.last_work_at TIMESTAMPTZ`, `sessions.open_tool_count INTEGER NOT NULL DEFAULT 0` |
| `000092_session_usage_status` | `000092_session_usage_status` (same number) | `sessions.session_usage JSONB` (private projection), `session_event_log.usage_key TEXT`, `session_event_log.usage JSONB`, partial index `idx_session_event_log_usage_key` |

No lineage alias: neither exp file was ever applied to a production ledger.

**Dry run on production ledger exports (2026-10-26, read-only exports, scratch PG 16/17
on 127.0.0.1, removed afterwards).** Thor's and m6's schema-only dumps + their
`volundr_schema_history` rows were restored, migrated to the integration tip
(20c639ee0, the B0 baseline) and then to the B1a tip. Step two on both hosts:
**executed exactly `000091_session_work_facts` and `000092_session_usage_status`**,
adopted nothing, `ting/` stream unchanged (m6: 44 applied, 0 to run); a second run was a
no-op; the five columns and the index exist afterwards.

Correction to §4: Thor's ledger already holds `000050_session_definition`,
`000051_device_tokens` and `000052_session_activity_state_since` under their canonical
names (applied 2026-09-09), so the runner counts them as applied, not adopted by alias.

## B1a.4 API shape the iOS client sees

All new fields are additive; every one can be null (or false) until a broker running
this code reports, and null is never a measured zero.

**Session row** — `GET /api/v1/forge/sessions`, `GET /api/v1/forge/sessions/{id}` (the
Guild facade serves the same paths and passes the fields through, adding its instance
fields):

```json
{
  "last_work_at": "2026-10-26T12:00:03.120000+00:00",
  "tool_open": false,
  "open_controls": [
    {"request_id": "req-1", "kind": "question", "opened_at": "2026-10-26T12:00:01+00:00"}
  ],
  "context": {"tokens": 7546, "window": 1000000, "share": 0.007546}
}
```

- `last_work_at` (ISO 8601 or null): source time of the newest *work* row (assistant
  output, result, content blocks, tool and turn hooks). Never a heartbeat, reconnect
  handshake or terminal frame.
- `tool_open` (bool): a `PreToolUse` without its `PostToolUse`/`PostToolUseFailure`
  (reset by `result`/`Stop`).
- `open_controls` (list or null): the broker's open human gates. `[]` = none open;
  **null = unknown** (older broker, nothing reported yet, or awaiting input on a gate
  without a request id) and must not be read as "none open". `kind` is the gate kind
  (`permission`, `question`, …).
- `context` (`{tokens, window, share}`, each int/int/float or null): last measured
  context occupancy; `tokens = input + cache_read + cache_write` of the latest model
  response (output excluded); `share` is the raw fraction and may exceed 1. Legacy
  `tokens_used` is a different aggregate; do not use it for context.

**Live event** — the existing `session_updated` on the session SSE stream carries the
same four fields in `data` (`data.last_work_at`, `data.tool_open`, `data.open_controls`,
`data.context`). It is published after each usage commit (including responses inside a
tool loop) and on every session update; no new event type.

**Status card** — `GET /api/v1/forge/sessions/{id}/status` (same auth, read scope and
owner/participant/tenant policy as the session GET: 401 / 403 / 404). Persisted only: no
broker, transcript or provider call on read.

```json
{
  "context": {"tokens": 7546, "window": 1000000, "share": 0.007546},
  "last_turn": {"input": 12, "cache_read": 7234, "cache_write": 300, "output": 236},
  "totals": {"input": 42, "cache_read": 14468, "cache_write": 600, "output": 336},
  "compactions": null,
  "model": "claude-opus-5-5",
  "engine": "claude",
  "host": "thor",
  "started_at": "2026-10-23T12:00:00Z",
  "uptime_s": 60.0,
  "cost_estimate": null,
  "usage_available": true,
  "usage_reason": null,
  "totals_complete": false
}
```

- `last_turn` = latest model response, `totals` = observed sums; each component int or
  null. Codex native input includes cached input; the API subtracts it once.
- `compactions`: null until a native compaction boundary is observed, then the
  deduplicated count. `cost_estimate`: native-reported cumulative USD or null (Codex and
  tmux report none); no price heuristic.
- `usage_available: false` + `usage_reason` explain missing current context
  (`compaction_pending_usage`, `reset_pending_usage`, `transcript_catching_up`,
  `incomplete_record`, `transcript_unreadable`, `native_counter_regression`).
- `totals_complete: false` means totals/compactions/cost are observed lower bounds
  (resumed sessions may have unseen earlier responses); this release never claims true.
- `started_at`: first native/runtime init identity; `uptime_s` only while
  starting/provisioning/running, else null. `engine`: `claude` | `codex` once measured.
- Context windows: native raw windows win; otherwise `volundr.session_usage.model_windows`
  (defaults 1,000,000 for `claude-opus-5-5`, `claude-fable-5-1`, `claude-sonnet-5-5`;
  chart `sessionUsage.model_windows`); unknown models keep `window`/`share` null.

## B1a.5 Validation (2026-10-26, Thor, clean `env -i` allow-list, `uv --frozen`)

| Gate | B1a | B0 |
|---|---|---|
| `make lint` | pass | pass |
| full backend suite (`-n 16`, cov gate 85) | **28506 passed**, 16 skipped, 1 xfailed, **14 failed**, coverage **88.90 %** | 28315 / 14, 88.89 % |
| `make test-forge` | **9244 passed**, 3 skipped, 1 xfailed, **4 failed**, Skuld coverage 87.69 % | 9095 / 4 |
| `make test-forge-tmux` | **60 passed** | 60 |
| `make test-forge-database` (scratch PG 16, 127.0.0.1) | **115 passed** (now incl. work-facts and usage real-PG tests) | 88 |
| `helm lint charts/volundr` + `helm template` | pass; renders 000091/000092 and `session_usage.model_windows` | pass |
| migration dry run on Thor + m6 ledger exports (B1a.3) | only 000091 + 000092 execute; second run a no-op | |
| pre-push web-next build | run by the push hook (no `web-next` change) | |

The failures are exactly B0's box-specific set, each reproduced on pure upstream `dev`
(§6): 14 = `test_install_sh*` (10), `test_docker_container` recovery,
`test_terminal_docker` workspace mount, `test_workflow_kickoff[/workspace]`,
`test_api_release_guard`; forge lane 4 = docker-container, release-guard and two
`test_resident_notification_relay` GC-under-`-W error` cases. One earlier full run also
failed `test_ting_queue_trigger::test_run_survives_poll_exception` (a 50 ms sleep
waiting for three 10 ms polls under `-n 16` load; Ravn, untouched here); it passed 3/3
alone and in the final run.

## B1a.6 Open questions

1. Conversation `tail` reads vs `DurableTranscripts` (§7.1): still open, untouched.
2. Embedded status `host` uses `projects.instance_id`; if the control plane needs a
   different host-map key (F7 `host_identity`), that lands with whichever lift brings F7.
3. `activity_heartbeat.stale_output_seconds` (Skuld, default 600) is config-only like the
   rest of `activity_heartbeat`; the Skuld session config template does not render it.
