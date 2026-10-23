# cp-dev session status/context (LEX-1421 part A)

- doing: implementation, regression tests and independent review complete on `exp/cp-dev-session-status`; landing SHA and deploy-request are in the Forge description. cp-ops deploys exp only; live exercise verification awaits that deployment.
- D29 reuse: numeric observations extend the existing SDK/tmux/Codex events, durable `session_event_log`, session row, list serializer and `session_updated` stream. No TTY polling, provider request, new store or new message route. The small private JSONB projection and indexed per-native-response checkpoints make reads constant-time and make retries/restarts idempotent.
- tested: 496 final API/model/config/log/facade/transport regressions; 10 isolated real-PostgreSQL cases; broader transport suites separately passed 463 Codex and 160 SDK/tmux tests. Some focused cases occur in both groups. Numeric projection coverage is 94.23%, above the 85% gate. Ruff and diff checks pass. All probes were offline or against a disposable Unix-socket PostgreSQL cluster; no production or live provider was used.
- reviewed: independent Codex reviewers `u11_contract_audit` and `lex1406_review` approved the final reducer/log behavior; each transport was independently reviewed. Root independently reviewed the API/model/persistence/config/migration scaffold. Counterexamples are tests: repeated Claude blocks, noncontiguous/restarted replay, Codex multiple responses in one turn, equal cumulative or alternating context-only replay after compaction, reset/cost replay, old-native windows/regressions, malformed/partial/oversized numeric telemetry, stale session updates, conflict payloads and transactional rollback.
- migration: `000092_session_usage_status` adds `sessions.session_usage` and nullable derived `session_event_log.usage_key/usage` plus a partial `(session_id, usage_key, seq DESC)` index. Root, CLI and Helm copies match. Raw event payloads, timestamps and sequences remain immutable. Down removes only derived data; forward deployment does not backfill historical SDK logs. New native observations populate the projection; tmux drains its existing main JSONL on authenticated hooks.
- deploy-request: niuu `exp/control-plane` merge SHA is in the session description. Rebuild exp Forge and its broker artifact through the normal cp-ops rollout; existing broker sessions receive transport changes through their normal lifecycle. No new secret, mount, service, production config or LLM exercise is requested. Optional `session_usage.model_windows` uses native windows first, then explicit model defaults.

## Contract for the iOS lanes

Read `GET /api/v1/forge/sessions/{id}/status` through the same authenticated Forge base as the session list. The facade finds the owning visible instance; `?scope=local` retains the existing served-host constraint. Authentication, session read scope, owner/participant/tenant access and error handling match the existing session GET (401 unauthenticated at the authenticated edge, 403 denied, 404 absent). No new credential or grant is needed for the app.

```json
{
  "context": {"tokens": 7546, "window": 1000000, "share": 0.007546},
  "last_turn": {"input": 12, "cache_read": 7234, "cache_write": 300, "output": 236},
  "totals": {"input": 42, "cache_read": 14468, "cache_write": 600, "output": 336},
  "compactions": null,
  "model": "claude-opus-5-5",
  "engine": "claude",
  "host": "<owning registry instance id>",
  "started_at": "2026-10-23T12:00:00Z",
  "uptime_s": 60.0,
  "cost_estimate": null,
  "usage_available": true,
  "usage_reason": null,
  "totals_complete": false
}
```

The example is illustrative, not a live exercise receipt. `context.tokens = last_turn.input + cache_read + cache_write`; output is excluded. `last_turn` means the latest model response, not all responses in an agent tool loop. For Codex, native input already includes cached input; the API subtracts it into uncached `input` exactly once. Native raw context windows take precedence; defaults are 1,000,000 for `claude-opus-5-5`, `claude-fable-5-1`, and `claude-sonnet-5-5`. Unknown windows remain null. `share` is the raw fraction and may exceed 1; the UI can clamp its drawing while retaining the measured value.

Every numeric field can be null when unmeasured. Null is never a measured zero. `usage_available:false` and `usage_reason` explain temporary missing current context (for example `compaction_pending_usage`, `reset_pending_usage`, `transcript_catching_up`, `incomplete_record`, `transcript_unreadable`, or `native_counter_regression`). Compaction clears context until fresh post-boundary usage; old replay cannot refill the ring. `totals_complete:false` explicitly means observed lower bounds: an attached/resumed native session may have earlier unseen responses, costs or compactions. The initial release conservatively makes no complete-history claim. `compactions` is null until at least one native boundary is observed, then the deduplicated observed count. Native-reported cost is cumulative USD when available; Codex/tmux without reported pricing returns null. No blended-price heuristic is used.

`started_at` is the first recorded native/runtime init identity (a new native identity can establish a later start); it is not row creation time or a websocket reconnect handshake. `uptime_s` is measured from it while starting/provisioning/running and null for stopped/failed or unmeasured sessions. `model` prefers observed native metadata, then the configured session model; `engine` is `claude` or `codex` once measured. Public facade `host` is the owning registry instance ID; direct embedded Forge uses its configured stable host identity.

List/session rows now include the same `context:{tokens,window,share}`. Existing `session_updated` live events include it at `data.context` after usage commits, including model responses within a tool loop. The ring can update from that existing stream; fetch `/status` for the full card. Private native checkpoints never appear in the API or generic session serialization. Legacy `tokens_used` remains a separate existing aggregate and must not be used as context occupancy.

## Exp acceptance after cp-ops deploys

Use only newly created exp exercise sessions, preserve other lanes' fixtures, and record their IDs here. Drive numeric fixtures through the exercise broker's ordinary durable-log path without sending an LLM prompt. SDK repeated blocks must count once; hook-driven tmux JSONL must count the same main native response once and ignore children; Codex repeated token-count snapshots must retain exact cumulative totals. Check the public status, list and existing live stream together; repeat the fixture after a projector reload, compact, and replay old samples. Stop only the new exercises afterward. The automated real-transport fixtures and real-PostgreSQL API/list/live test already bind these checks; deployment/live receipts remain pending.

---

# cp-dev structured notify questions (LEX-1412)

- done: structured `ask` producer/read-wire support on `exp/cp-dev-structured-questions`; landing SHA is recorded in cp-dev's description. No deployment or live notification. Independent Codex review found no blockers, including the final compatibility tests; reviewer independently reran the eight REST/recovery cases after its earlier 177-test core review.
- contract: `notify` and `POST /api/v1/forge/sessions/<id>/notifications` accept optional `ask:{items:[...]}` only with literal `question:true`. Each item has a stable ASCII `id`, `kind:question|action`, and `text`; optional `header`, `options:[{label,description?}]`, `multi_select` and `allow_text`. Legacy `question:true` without `ask` remains valid. Actions ask the user to report done/not done; they never execute anything.
- bounds: 1–8 items, unique IDs of 1–64 characters; text 1–2000, header 1–80; 1–8 options when supplied, unique nonblank labels of 1–120 characters, descriptions up to 500; total compact UTF-8 ask JSON at most 32768 bytes. Flags are strict booleans; omitted values retain client defaults `multi_select:false`, `allow_text:true`. Optional nested fields must be omitted rather than null. NUL is refused to prevent PostgreSQL scrubbing structured answers. Questions that disallow text require choices; actions may use their done response.
- persistence: the existing durable `conversation.turn` and notification metadata JSONB retain `ask` through broker log retries, projection, direct REST submission, SSE/read wire and typed feed/session responses. No store, migration, routing or message-delivery behavior is added. The compact MCP list summary retains its previous summary fields; REST supplies the full question.
- guidance: both notify schemas/descriptions and the source `forge-notify` skill template describe mixed question/action items, defaults and later resolution by the asking session.
- validation: 363 focused notification, tool, broker, REST, facade, projector and storage-adapter tests pass; 3 process integration tests pass, including real stdio MCP → real broker → local fake Forge durable-log delivery for legacy and structured notifications. Labels/text/headers retain exact whitespace; duplicate choices use exact label equality. The root review's normalization counterexample is fixed and independently reviewed with all 49 structured-question cases passing. No LLM or live service was used. Ruff and diff checks pass.
- answer-delivery contract for CP: stable `request_id` on existing `POST /sessions/<id>/messages?scope=local`; HTTP 200 means delivered, 202 means pending, same request ID/content replays persisted state, different content is 409. Existing GET message-delivery receipts report pending/delivered/failed. Delivery is transport acceptance, not question resolution. No Forge-side change is needed.
- deploy-request: matching niuu/frontend merge SHAs are recorded in cp-dev's description. Deploy through cp-ops after the CP consumer has canonical 1.1.0 provenance and lands; no new env, mount, secret or migration. New brokers receive the updated notify schema/skill.
- D29 reuse: one small typed ask structure extends the shared notification contract; all producers and readers reuse existing durable log, metadata, validation and source skill generation.

# cp-dev E0 artifact titles

- doing: replay-title fix ready on `exp/cp-dev-artifact-titles`; independent Codex review found no issues and reran all 79 tests. No deployment or live replay was run.
- cause: the E0 operator script reused `manifest_log_rows`, which invented the title `T` for all 122 Thor manifest publications (12 provider keys). The manifest did not capture their titles. The Forge extractor and storage preserve complete titles.
- fixed: the existing builder now copies an explicitly captured nonblank title in full and omits unknown titles. It preserves legitimate `T` titles. The original capture manifest and E0 receipt are unchanged. Existing recorded E0 rows need the companion CP projection repair, not another log append.
- validation: 79 replay-title, extraction, burst and artifact-service tests pass, including the exact `QVimZJxaHtjyQfT1R497Kw` publication at `2026-10-21T01:04:10.334238+00:00`, all 122 capture publications, and legitimate one-character/full titles. Ruff and diff checks pass.
- D29 reuse: corrected the shared replay builder used by existing tests and the operator script; no new storage, runtime extractor, or metadata source. Operator guidance is in `docs/operator/e0/README.md`.

# cp-dev project side-quest defaults (LEX-1406)

- done: implementation, validation and independent review of `exp/cp-dev-project-defaults`; merge SHA is recorded in cp-dev's Forge description. No deployment has been performed.
- implemented: per-project `side_quest_defaults` persisted in the existing project JSONB document via the existing revision-checked project create/update API. Missing means legacy CP config fallback; explicit `null` clears that fallback; `{host,local_path}` selects a saved default. Serialization preserves all three states through unrelated updates and restarts.
- app contract: `PATCH /api/v1/forge/projects/<project UUID>?instance_id=<served registry UUID>` with `{"revision":<current>,"side_quest_defaults":{"host":"thor-exp","local_path":"/home/exp/workspaces/cp-exercise"}}`; clear with `side_quest_defaults:null`. `host` is `projects.instance_id`, not the registry UUID. Omitted field leaves it unchanged. Success returns the new revision; stale revision is 409. GET/list returns the saved field, including explicit null or true omission for legacy rows.
- served-host checks: a POST/PATCH containing this field can only reach the visible embedded Forge, even when the registry's default is remote. Remote query/body selectors fail before forwarding, including clear. The embedded service refuses replicas and host mismatch, requires local mounts enabled, and reuses the existing resolved-prefix allowlist with an existing writable directory. Canonical symlink targets are revalidated against the persisted field constraints before writing. A Git checkout is not required. Normal project APIs retain their existing behavior.
- persistence: migration `000091_project_side_quest_defaults` adds an idempotent JSONB shape CHECK in the root, CLI and Helm copies; no new store/column/backfill. Legacy rows remain missing. Down migration drops the CHECK and removes only this field for legacy decoder compatibility, so saved defaults are lost on downgrade.
- validation: 276 project/facade/auth tests passed, including 46 new API/service/policy cases and 3 migration-packaging checks. Fresh, Unix-socket-only scratch PostgreSQL passed 55 integration cases (48 new + 7 existing), including presence roundtrip, concurrent CAS and repeatable up/down; the cluster is stopped. The actual Forge facade POST/PATCH/GET → CP poll/store/Project/Configuration test proves exactly one poll per update, both absent→set→clear and legacy-config→set→clear. No model or live service was used.
- D29 reuse: `workspace_path` was rejected because it requires a matching Git repository and changes ordinary project context. This small optional field reuses project ownership, revision CAS, JSONB storage, REST routes, local mount validation and CP's existing ingest; replica synchronization deliberately does not copy host-local defaults.
- review: independent Codex `lex1406_review` found canonical symlink targets bypassed field validation. The exact four counterexamples are fixed and pass; final review has no remaining blocking findings.
- deploy-request: niuu landing SHA is in cp-dev's Forge description; see `DEPLOY-REQUEST.md`. cp-ops deploys the matching frontend consumer. No config or secret changes are needed.

# cp-dev R17 overflow retrieval (LEX-1387)

- done: `exp/cp-dev-r17-overflow`, based on `origin/exp/control-plane` `1bf2cb16`, tested and independently reviewed; exact landing SHA is in cp-dev's Forge description. No deployment or live session was started.
- implemented: opt-in broker `project_more` MCP read tool. The agent chooses only section/offset/sha; the fixed exp CP endpoint verifies the existing session bearer through exp Forge, derives its project, and applies Private policy. No device credential, new token, secret mount, or caller routing input.
- R17 counterexample: the real injected stdio MCP and a real Skuld broker process fetched all pages from the frontend's actual CP auth/router/store fixture and recovered a question absent from the capped seed. The test reads the tool hint from the seed. Its sole test transport mapping redirects the fixed internal CP origin to an ephemeral loopback fixture; no production runtime override exists.
- validation: 211 tests passed across `test_project_more.py`, `test_forge_mcp_tools.py`, `test_forge_mcp_broker.py`, `test_session_env.py`, `test_forge_mcp_injection.py`, and `test_rest_forge_mcp.py`; the cross-repo `test_project_more_consumer_e2e.py` integration passed separately. An additional 133 existing Forge session auth/token/facade tests pass, including invalid/expired credentials, revocation, mini-mode verification and local-only routing. New HTTP adapter statement/branch coverage: 100%. Python 3.13; Ruff and diff whitespace checks passed.
- review: independent Codex `artifacts_handoff` approved both repos with no remaining blockers. Its CP JWT-parser counterexample is fixed in the companion frontend route, with exact regressions and a final PyJWT compatibility re-check. Real MCP integration and 39 adapter tests were rerun after the final fixture update.
- deploy-request: niuu landing SHA in cp-dev's Forge description; cp-ops must deploy the matching frontend CP scoped route and enable `SKULD__FORGE_MCP__PROJECT_MORE_ENABLED=true` only on exp Forge, for newly launched brokers. Full instructions in `DEPLOY-REQUEST.md`.
- D29 reuse: this extends the existing Forge MCP spec/host, scoped session token, broker credential scrubbing, and stdio injection. One small CP HTTP adapter is new because the existing toolbox had no authenticated project retrieval path. Ordinary MCP catalogs remain unchanged unless explicitly enabled.

# forge-artifacts lane (LEX-1397, under LEX-1387): status

Branch `exp/cp-forge-artifacts`, cut from `origin/exp/control-plane` at `33087ca2`.
Lands on niuu `exp/control-plane` (xteo fork). Deploys only through cp-platform to exp Forge (:8380).

## Checklist

| Item | State | Evidence |
|---|---|---|
| F1 projector at ingest (hook rows, present_file turns, notify links, report_artifact) | done | `volundr/domain/artifacts.py`; `rest_session_log._project_artifacts_isolated`; tests `test_artifacts_extraction.py` (35), `test_rest_session_log_artifacts.py` |
| F1 internal backfill (SQL replay of `session_event_log`) | done | `PostgresArtifactPublicationRepository.scan_log`; `POST /artifact-publications/backfill` (admin); `artifacts.backfill_on_start` |
| F1 `forge_artifact_publications` + `GET /artifact-publications` (**F1a**) | done | migration `000089` (dirs `migrations/`, `src/cli/migrations/volundr/`, Helm configmap); `GET /artifact-bursts?after=<rev>` too |
| F1 `report_artifact` MCP tool | done | `niuu/domain/artifact_reports.py`, broker host + toolbox; transcript mode only |
| F1 capture-manifest golden (230 rows, 26 keys; replay inserts 0) | done | memory + real Postgres: thor 122, mini-m6 108, exact occurrence sets, 26 keys, second replay 0 |
| Codex present-file shape | done | `test_present_file_from_the_brokers_own_producer` (Skuld's producer, the turn Codex writes) |
| F1n bursts: persisted `burst_id`, late joins, bridges, silent backfill | done | platform B1–B9 + model B1–B6 on memory **and** Postgres |
| F1n durable 120 s hold, restart recovery, transactional suppression recheck | done | `ArtifactHold`; `emit()` one transaction; B6 restart test; restart-during-hold emits once |
| F1n golden (33 bursts, 25 notices) | done | memory + Postgres; mutation check: hold 0 → 33 notices, hold 600 → 22 (design §V1's figures) |
| F7 `GET /instance` (host, tailnet name; gateway adds self id + slug) | done | `rest_artifacts.create_instance_router`; facade `/instance` |
| F1t `Artifact.type` / `marker` | done | `TypeCatalog` (type URL, then front-matter name); per publication `type`/`marker` |
| Full app smoke (real lifespan, scratch PG) | done | append → live pub → burst → "Smoke page published" (art-burst:…) after hold → backfill 0 |
| Full backend suite + coverage gate | done | 26742 passed, coverage 88% (gate 85); the 14 failures (install.sh, Docker, ravn kickoff, release guard) fail identically on the untouched base |
| Review | done | independent review: no wedge or feed breakage; fixed burst `rev` ordering in emit/settle, suppression recheck under the feed lock, one bad burst no longer stalls the hold (settled `emit_failed` after 5), backfill stops at now − live lag, deadlocks are 503, one report per turn |
| Merge `exp/control-plane`, land, record sha | done | `dbfcf260` (first `9f145a1a`, then migration renumbered to `000089` (cp-forge-state also adds a `000088`; neither deployed)) |
| Send sha to cp-platform (RF) | relayed | ios-coordinator relayed it: cp-platform round 23 deploys exp-forge at `e2208da8` (F1, F1n, F7, F1t + forge-state) |
| E0 on exp stack | done (items 1–3); item 4 pending | exp Forge live at `e2208da8`. (1) `/instance` → host `thor-exp`; (2) all three feeds empty before; (3) Thor's 128 manifest rows appended to exercise session `da8a686a`: **122 publications, 12 keys, 33 bursts** (all backfill, = fixture), **0 notices**; replay #2 adds 0; admin backfill scanned 126, recorded 0, 0 notices. The 25-notice golden needs forced-live timing: offline only (memory + Postgres). (4) a real publish: pending the gated provider key. `docs/operator/e0/` |
| LEX-1397 comment | done | landing comment posted |

## Package 2 (relayed 2026-10-22): RF+E0, F8, C4, R9

| Row | Repo / branch | State | Evidence |
|---|---|---|---|
| RF + E0 | niuu, exp Forge | done (item 4 pending) | see the E0 row above; **config ask:** exp Forge's `/instance` reports `tailnet_name: 100.66.123.128`, not a tailnet name. cp-platform: set `host_identity.tailnet_name: thor.tail737f2a.ts.net` (DEPLOY-REQUEST) |
| F8 m6 on par | niuu `exp/cp-forge-artifacts` | re-review | review 1: 2 BLOCKER + 4 MAJOR, all fixed (write mode refuses production ports and needs the scratch id; read mode pinned with `scope=local`; full Forge route-set compare; recorded-OpenAPI mode; the gateway's 422→501 masking fixed). Branch Thor vs m6 shape **ON PAR 39/39**; live Thor `11afe41d` serves one route the branch lacks (notification images, see below). `docs/operator/forge-parity.md`, `docs/operator/f8-parity/` |
| C4 Artifact registry + lens | lexi-frontend `exp/cp-artifacts` | re-review; **needs cp-core B1** | `bbe7470`. 295 control-plane tests + 1 expected-fail; `tsc` clean. The golden gives exactly 26 rows (revisions = distinct provider versions, all 230 publications); R7-8a holds and every view validates; bursts match the reference (thor 33, m6 17), with B1–B9/B1–B6 on the CP side; Forge's burst id and `rev` are used as given. Review 1: 1 BLOCKER (in cp-core's store) + 3 MAJOR, the majors and minors fixed |
| R9 Fragment session and ids | lexi-ios `exp/cp-fragment-ids` | **landed** `a76587ef` on lexi-ios `exp/control-plane` | `notes.py fragment` mints `f-<16 hex>` once, records `{host, session_id}` (host from `PROJECTS__INSTANCE_ID` via `keys.HOST_MAP`; refuses inside Forge without one), step ids `<feature_id>#<i>:<sha8>`; `<N>.md` `features[]` is the one source R1's `ShipUploaded` reads (validates against the frozen schema); duplicate ids refused. Identity cases 8/8; `tools/release` 145 tests (1 known `/bin/zsh` error); `release-check` OK. Two reviews. Swift `ReleaseNotesTests` run on the next train (no Xcode on Thor) |

### C4 file ownership: proposal to cp-core (agreed with changes; see the C4 row)

C4 builds on cp-core's store at `507deef` and does **not edit** cp-core's files except two one-line hooks:

- **C4 owns (new files)** in `services/lexi-agent-service/control-plane/`:
  - `artifacts.ts`: the registry projection (publications → artifacts by `provider_key`, revisions by provider version, project from the producing session's current project) and the CP side of the burst rule (`bursts.py` port; reads Forge's `burst_id`, records `burst` occurrences through `store.recordOccurrence()`). Its tables are created by `artifacts.ts` on the same db (`CREATE TABLE IF NOT EXISTS artifact_*`), so `store.ts`'s schema is untouched.
  - `artifacts-ingest.ts`: polls exp Forge's `GET /artifact-publications?after=` and `GET /artifact-bursts?after=` (F1a, final) through the same allow-listed exp-Forge base as `ExpForgeIngest`, and hands publication rows to `store.apply` with `feed: 'publications'` (the shape cp-core's `applyPublication` already takes).
  - `views-artifacts.ts`: `GET /v1/cp/views/artifacts` (scope all/recent/project, `opens: system_browser`) and the `Project.artifacts` top-5 helper.
  - `__tests__/artifacts.test.ts` and fixtures under `contract/artifacts/`.
- **One-line hooks in cp-core's files:** `routes.ts` (register the view) and `runtime.ts` (start the artifacts poller next to `ExpForgeIngest`). If cp-core prefers to make those two edits, C4 exports `registerArtifactRoutes(app, deps)` and `startArtifactIngest(deps)`.
- **Ask:** if cp-core already plans publications ingest in `ingest.ts` now that F1a is final, say so and C4 drops `artifacts-ingest.ts`.

Until I hear back, C4 work stays in the new files only.

### C4 → cp-core request B1 (please relay)

`recordOccurrence` freezes an occurrence at its first recording, so in the live order (a publication, a poll, then the agent's notice) a burst never becomes "to check", a later attach never scopes it in All updates, and widening pushes no frame. C4 can't fix this in its own files. **The patch is ready:** lexi-frontend `exp/cp-artifacts` `services/lexi-agent-service/control-plane/requests/` (README + a 65-line patch to `store.ts` and `views/headlines.ts`):
- one ledger row per revision, with revision 0 keeping today's id;
- the upsert also updates `project_id`/`occurred_at`;
- a stale `announced_by` join is dropped;
- headlines read the newest row.

C4's live-order test is `it.fails` until the patch lands. **Ask:** cp-core applies the patch (or its own equivalent) on `exp/control-plane`, and C4 flips the test.

### F8 findings for the coordinator

1. **Release gap blocks parity at P1.** Live Thor (`11afe41d`) serves `GET /api/v1/forge/notifications/{notification_id}/image`; `exp/control-plane` doesn't (the release-only notification images). Before P1, cp-platform's X1 (branch sync) must bring that commit into `exp/control-plane`, or m6 isn't on par and Thor loses the route.
2. **The m6 attach 501 is not a missing route.** A well-formed attach works through Thor@`11afe41d` → m6@`6c31bfe1`. A 501 is either `PUT /description` (missing at `6c31bfe1`) or a **masked domain refusal**: both revisions' gateways turned any 422 into "update the host" when `role`/`parent` was sent (the CP always sends `role`). Fixed on the branch (`_rejects_unknown_fields`); it takes effect on m6 once m6 runs the branch.
3. **The probe's read mode is not on the production allow-list** (boot-0: no routes). Running it against production at P1 needs the allow-list's owner to add `/instance`, `/artifact-publications`, `/artifact-bursts`, `/notifications`, `/openapi.json` and `/health` (all statically zero-write).

### Reuse check (VISION §0, D29)

- **F8:** reused the served `/openapi.json`, `/instance` (F7) and the existing facade attach/description routes. New: `scripts/forge_parity_probe.py`, because `scripts/forge_live.py` creates billable sessions and needs httpx (not on m6's system python), and `scripts/extract_openapi.py` builds mocked node routers rather than what a host serves under its mount list. Gateway fix: a 20-line helper in `rest_volundr.py`, no new type.
- **E0:** reused the test harness (`tests/support/artifacts.manifest_log_rows`) and Forge's own log append and backfill routes; nothing new in `src/`.
- **R9:** extended `tools/release/notes.py`, `cp_producer.py` and `train.py`; R1's producer now reads the train's `<N>.md` instead of a second builder. New: `test_fragment_ids.py` and the vendored identity fixture.
- **C4:** reused cp-core's store (`publications`, `sessions`, `notifications`, `ingestBatch`, `recordOccurrence`), the views module (`sessionRef`, `projectRef`, `allArtifacts`, router auth), `schema.ts`, `fetchJson`/`expForgeBase`/`expForgeOrigin`, `identity.ts` `uuid5`/`eventId`, and Forge's `burst_id` and burst feed. New: `artifact_*` tables, because the store has nowhere to keep persisted burst ids, membership, bridges or the bursts-feed cursor (`setCursor` is private, keyed by feed), plus `artifact_file_bases`.

## Decisions (docs-supported, noted here instead of asking)

- **Host name** of publications/bursts: `host_identity.host`; empty falls back to
  `projects.instance_id` (exp Forge: `thor-exp`, the manifest's exp host key), then `local`.
- **Which rows form bursts:** Artifact-tool publishes and `report_artifact` reports of claude.ai
  artifacts. Notify links of kind `artifact` are recorded as publications (provenance, §5.2 path 3)
  but are announcements (model/bursts.md), so they never join or open a burst. Presented files are
  recorded, never burst (design §V1: no notice for files in Slice 0).
- **Suppression** (model/bursts.md, any session): an `agent` notice linking the artifact, created in
  `[started_at − 30 min, min(emission, last_at + 30 min)]`, suppresses; Slice 0's own system notices
  never count. Rechecked inside the emission transaction. Same predicate as the model's
  announcement (`announces`, tested against model B6).
- **Live vs backfill:** live when ingest sees the row within 10 min of its `ts`; the internal
  backfill always writes `backfill`.
- **Revision id:** `rev_` + sha256(`<provider>:<key>|<provider_version>`)[:32]; without a version,
  hashed with host, session and tool-use id (design §5.1 gives no exact formula).
- **Burst feed:** `GET /artifact-bursts?after=<rev>` added beside `/artifact-publications`, because
  late joins widen a burst after its publications were served; `rev` advances on every change.
- **`report_artifact`** writes a `conversation.turn` (`metadata.kind = artifact_report`), its own
  shape rather than a notification, so a report never becomes a notice.
- **Release gap:** not merged. `exp/control-plane` already has session descriptions (3e603dd6) and
  the attachments index; the release-only commits are notification images, which Slice 0 does not
  use. Branch sync stays cp-platform's X1.
- **Wire:** cp-api-v1 1.0.0 untouched (these are Forge-side routes C4 reads).
- **A burst spanning several owners' sessions** (B1 allows it) is visible to, and announced to,
  the seed session's owner. Single-owner hosts today (dev-user); revisit if Forge hosts multiple
  owners.
- **F8 health:** the probe judges `/health` on the Forge plugins only (volundr, guild, niuu); m6 disables ting, mimir, ravn and observatory on purpose.
- **F8 read mode** makes no per-session reads (they reach the owner lookup, which writes: `exp/prod-read-allowlist.json`); attach and create are checked in `--write` mode against loopback Forges only.
- **A live row arriving late** (3–10 min) opens a burst already due, so its hold is short; this
  matches the reference (`due = at + hold`).

---

# forge-state lane (LEX-1398, under LEX-1387): status

Branch `exp/cp-forge-state`, cut from `origin/exp/control-plane` at `33087ca2`.
Lands on niuu `exp/control-plane` (xteo fork). Deploys only through cp-platform to exp Forge (:8380).

## Checklist

| Item | State | Evidence |
|---|---|---|
| F12a flush is not a turn start | done | `b24c6d58`; `tests/test_skuld/test_stuck_state.py` (fails without the fix) |
| F12b idle_prompt self-heal (incl. dead prompt) | done | same file, `TestF12bIdlePrompt` |
| F12c heartbeat guard (600 s, config) | done | `TestF12cHeartbeatGuard`; untracked transports are never guessed idle |
| F12d `last_work_at`, `tool_open`, `open_controls` on the session | done | migration 000088 (3 copies); ingest SQL; REST, event, MCP summary |
| 20 derived-state goldens | done | `tests/test_niuu/test_work_facts.py` (fold + fields-only path, 42 pass); `tests/integration/volundr/test_session_work_facts.py` on a throwaway Postgres 16 (15 pass, retries fold nothing twice) |
| F11b `question`, `about_question` on notify (+ feed notify, REST submit) | done | `77666654`; bare uuid refused |
| F11b tool description: producer rule | done | `QUESTION_GUIDANCE` in both notify descriptions and the forge-notify skill |
| R7-2b, R7-2c, model ask cases | done | `tests/test_niuu/test_question_signals.py`: A1–A27 + R7-2b/c replayed through Forge's draft → turn → projection → wire (33 pass; 21 fail if the wire drops the fields) |
| Oracle (read-only, docs worktree) | done | `validate.py`: derived 20/20, R7-2b/R7-2c pass; `model/acceptance/run.py` 117/117 |
| F2 durable copy + tailnet URL | done | `c7e93b9c`; `tests/test_volundr/test_presented_files_durable.py`, `tests/test_skuld/test_presented_durable.py` |
| Review of the branch | done | independent review: 2 high (stored XSS on inline files; F12c idle for transports without a turn flag), 6 medium; all high and medium fixed except the deferred items below |
| Merge into `exp/control-plane`, record sha | done | `e2208da8` (lane tip `1f3ecf78`) |
| Send sha to cp-platform; exp Forge redeploy | relayed | ios-coordinator relayed `e2208da8` + overlay: cp-platform round 23 |
| Live check 1: session work facts on exp Forge | done | 2026-10-22: `:8380/health` = `e2208da8`; `GET /sessions` rows carry `last_work_at`, `tool_open`, `open_controls` (the E0 session) |
| F2 live proof: URL serves from exp Forge over the tailnet, from mini-m6 | **blocked (cp-platform)** | Exercise session allowed (ios-coordinator, 22 Oct). Session `a6b9242a` (`f2-f11b-live-check`): the broker exits about 2 s after every start (`liveness: pod is no longer running`), the same as E0's `da8a686a`, so nothing can run `present-file` in it. The cause is in `<workspace>/.skuld.log` inside the `lexiexp_forgehome` volume, which only cp-platform reads. Session stopped and archived. mini-m6 reaches :8380 (health and sessions 200) |
| Live check 3: `question` on a notify | partly | `POST /sessions/a6b9242a…/notifications` with `question: true` returned 201, and `GET /notifications` lists `question: true` (id `30c2e4bd`; `source: operator`). The in-session notify path waits for a working broker |
| F2 bug from cp-platform R25: upload 405 through the facade | fixed | `beffb6ef`, `<this landing>`: niuu's facade gains the PUT passthrough, and the forge_session allow-list admits it (OWN, notify scope): Forge's middleware enforced the same list, so the PUT was also 403 straight to Forge. Tested through the facade in front of the embedded Forge with a real session token (`tests/test_niuu/test_rest_volundr_presented_files.py`; fails without the fix). Review: 0 high, 1 medium (unbounded facade body: now capped at `presented_files.max_bytes`), lows fixed. `room_access.py` unchanged: it gates broker routes, and this PUT goes to Forge |
| `tests/test_projects/test_mesh_lightweight.py::test_assignment_forwards_role_parent_and_detach` | for cp-forge-artifacts | fails on `exp/control-plane` `1b8fb72b` without my changes (F8's gateway 422 fix?) |
| LEX-1398 comment | done | landing comment posted |
| F2 gap: durable files removed on session delete | done | `d76c1420`, `0deedaba`; `tests/test_volundr/test_presented_files_cleanup.py` (fails without the fix); a PUT that lands after the delete removes what it wrote (404) |
| F2 gap: uploads resume after a broker restart | done | `presented.json` pending record at staging; `_resume_presented_uploads` at startup, one file at a time; a final 4xx (`refused.json`) is never retried; `tests/test_skuld/test_presented_durable.py` |
| Review of the gap fixes | done | independent review: 0 high, 2 medium (delete/PUT race; resume burst), 4 low; all fixed except the card URL note below |

Full suite: 26,731 pass; the 17 failures are identical on the untouched base (box-specific:
install_sh, ting turn timeouts, docker_container, api_release_guard, ravn kickoff).

## Decisions (docs-supported, noted here instead of asking)

- **F12d `open_controls`** comes from the broker's control state (design §V9.2: `[{request_id,
  opened_at}]`, plus `kind`), carried in every activity report's metadata and served from
  `activity_metadata`. `null` = unknown (older broker, or awaiting input with no request id),
  never "none open". Resolving one of two gates forces a refresh so the list shrinks.
- **F12d storage:** two columns on `sessions` (`last_work_at`, `open_tool_count`), folded in the
  same single UPDATE as `latest_final_*` per inserted row, so producer retries are idempotent.
  The work-row predicate is one module (`niuu.domain.work_facts`) used by Skuld and Forge.
- **F12c** never guesses idle for a transport that doesn't report its own turn flag; it only
  stops heartbeating. The liveness reaper is off by default, so that is safe.
- **F11b field names** follow the frozen contract (`question`, `about_question`), not design
  §V8.4's older `ask`/`resolves`. Stored in `forge_notifications.metadata` (no migration),
  served on the read wire and the SSE payload (null when unset). Forge does not enforce the
  producer rule; the control plane does (C3a). The tool description states it.
- **F11b `<host>`:** Forge doesn't know the control plane's host key, so the description tells
  agents to build `question:<host>:<notification_id>` with the host key (thor, mini-m6). No
  `question_ref` is returned.
- **F2 trust model:** Forge on the tailnet serves reads without auth (mini mode), so the
  durable URL needs no token: `presented_files.public_base_url` + the existing route. The
  config refuses `*.xteo.mesh` and non-http(s).
- **F2 storage:** dynamic-adapter store (`LocalPresentedFileStore`, `~/.niuu/presented-files`),
  no DB table, so F1 keeps sole ownership of publication rows.

## F1/F2 boundary (cp-forge-artifacts)

I couldn't message cp-forge-artifacts: this session has no `message` grant. The boundary I took,
based on its STATUS.md and code:
- **F2 (here):** the durable copy, the upload route, the durable-first GET, and the absolute URL.
  The broker writes the URL into the `present_file` turn's `tool_input.url`.
- **F1 (theirs):** publication rows. Their `_present_file` builds a relative
  `/api/v1/forge/sessions/{sid}/files/presented/{file_id}`. It can prefer `tool_input.url` when
  present, or build it with `volundr.domain.presented_files.durable_file_url(base, sid, fid)`.
- **At landing** (F1 landed first, at `9f145a1a`, then renumbered its migration to 000089 in
  `16e0e48d`): mine stays 000088; config sections merged; both lanes' status kept here, one
  section each. F1's `_present_file` now records the turn's `tool_input.url` when it is a valid
  tailnet http(s) URL, otherwise the relative route as before
  (`tests/test_volundr/test_presented_files_durable.py`).

## For cp-platform (exp deploy)

The durable store is **off by default** (review: the local store is not durable on k8s). The exp
Forge overlay needs:

```yaml
presented_files:
  enabled: true
  public_base_url: "http://thor.tail737f2a.ts.net:8380"
```

Without `public_base_url`, files are durable but keep the relative route.

## Review: fixed, and deferred

Fixed:
- inline durable files carry `Content-Security-Policy: sandbox` and `nosniff`;
- F12c never guesses idle for transports on the port's default `is_turn_active`;
- the registry rebuild never serves the durable receipt;
- `update()` returns the stored work facts;
- the store is off by default;
- the PUT body is streamed with the size limit;
- the inline upload wait is capped at 5 s (then a background retry);
- frames that change nothing skip the session write;
- the hook-name lookup matches the SQL exactly;
- the refresh's attention detail names the gate still open.

Deferred (noted, not blocking boot 0):
- child/teammate Claude hooks are not filtered from the work facts, matching the reference
  `derived_state.py`, which doesn't filter them either;
- ~~no cleanup of durable files on session delete~~ (done, `d76c1420`);
- ~~background upload retries are lost if the broker restarts~~ (done, `d76c1420`);
- a card whose upload lands late (retry or resume) keeps the relative route in its turn, not the
  absolute URL. It still opens the durable copy: the route serves the durable copy first.
- two concurrent PUTs of different bytes under one id could interleave (only the session's own
  broker can PUT, with a fresh random id).

## Blockers

- **No cross-session message grant** (`forge_mcp.grants` lacks `message`). Deploys go through
  ios-coordinator, which reads this file and the session description (second package, 2026-10-22).

## Next package (2026-10-22, from ios-coordinator)

1. F2 live proof after round 23: pending the deploy confirmation.
2. F2 gaps: done (above).
3. V2 project seed and voice: lexi-frontend, worktree `~/repos/worktrees/exp-cp-seed`, branch
   `exp/cp-seed` (cut from `origin/exp/control-plane` `507deef`); status in that branch's STATUS.md.
4. C2v-b: after cp-core lands the C2v split.

## Judge R16 (fc519b0c), 22 Oct

| Finding | State | Evidence |
|---|---|---|
| **R16-1** F2: the public facade dropped `Content-Security-Policy: sandbox` and `nosniff` | fixed | `40176eff` + follow-up: one policy (`niuu.domain.presented_files.presented_file_safety_headers`: nosniff always, sandbox for all but PDF) on every route that serves a presented file: Forge's durable copy, Forge's live-broker fallback (`private, no-store`), the **public facade** (enforced whatever the node sends; only the two known cache policies pass), and the broker itself (its presented route, inline with the staged name and type, and its SendUserFile route), so `/s/{id}/api/...` inherits it. The judge's counterexample is `test_the_public_get_keeps_the_sandbox` (the public GET equals the node's headers). Review: 0 high, 1 medium (the broker had no policy: fixed), lows fixed. **Note:** a durable file stays in a browser's cache up to a day after a viewer's access ends |
| **R16-3** B1: a changed `(instance, same uuid)` parent was overwritten | fixed (lexi-frontend `exp/cp-owners`) | see that branch's STATUS |
| **R16-2** V2: seed overflow links hardcode `:9500` | in progress | |
| **R16-6** remove `originOf` in `views/project.ts` once cp-core's `session()` parses origin | waiting on cp-core | |


## Handoff to cp-dev (22 Oct)

**Open items (niuu side):**
- None blocking. R16-1 landed (`60a53223`), queued for deploy. After deploy, re-check the public GET on exp: `curl -sI http://thor.tail737f2a.ts.net:8380/api/v1/forge/sessions/<sid>/files/presented/<pf_id>` must show `content-security-policy: sandbox` (non-PDF) and `x-content-type-options: nosniff`.
- Deferred, written up above: a `_put` race between two concurrent writers of one id (only the session's own broker can PUT, with a fresh random id); child/teammate hooks are not filtered from work facts (matches the reference).

**Gotchas:**
- **Facade vs embedded:** on the platform server `/api/v1/forge/...` is niuu's Guild facade (`src/niuu/adapters/inbound/rest_volundr.py`), which proxies to the embedded Forge (`src/volundr/adapters/inbound/rest.py`). A route must exist in BOTH, and response headers are rebuilt by the facade: test through the facade (`tests/test_niuu/test_rest_volundr_presented_files.py`, built on `tests/support/forge_session.py`), not only the embedded app. That gap hid the F2 405 (R25) and the R16-1 header loss.
- **Session-token allow-list:** `src/niuu/domain/services/forge_session_policy.py` `ROUTE_POLICIES` is enforced by the facade edge AND Forge's `ForgeSessionAuthMiddleware`. A new broker-called route needs a `RoutePolicy` (the F2 PUT is `OWN`, notify scope).
- **Broker loopback token:** broker routes (`/api/present-file`, etc.) are loopback-only, owner role; exp sessions run with fake model keys, so an agent can't call them: cp-platform drives shell turns through the broker's loopback token (`docker exec`) on request.
- **Policy by type:** `niuu.domain.presented_files.presented_file_safety_headers` (PDF exempt from the sandbox on purpose). Every route that serves presented bytes uses it, the broker's included.
- Tests: `env -u PYTHONPATH uv run --all-extras pytest`. The 17 box-specific failures (install_sh, ting turn timeouts, docker_container, api_release_guard, ravn kickoff) fail identically on the base.

**Which tests bind which counterexamples:**
- R16-1: `test_the_public_get_keeps_the_sandbox`, `test_the_facade_enforces_the_policy_even_if_the_node_omits_it` (niuu facade), `test_live_broker_fallback_carries_the_same_safety_policy` (Forge), `test_broker_serves_presented_files_inline_and_sandboxed` (broker), the SendUserFile route check in `test_send_user_file.py`.
- F2 R25 (upload through the facade): `test_broker_upload_through_the_facade_then_served_after_stop`, `test_forge_refusals_pass_through_unchanged`, the 413/id tests in the same file.
- F12: the 20 derived-state goldens (`tests/test_niuu/test_work_facts.py`, `tests/integration/volundr/test_session_work_facts.py`).
- F11b: `tests/test_niuu/test_question_signals.py` (A1–A27, R7-2b/c).

**Evidence:** LEX-1398 comments (each landing); this file's tables; cp-platform's R27 receipt (F2 check 2 from mini-m6) and round 30 receipt 71b932cd.
