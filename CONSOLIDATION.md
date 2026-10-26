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
