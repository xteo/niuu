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

## Package 3 (relayed 2026-10-22): X1 branch sync, then H1

### X1 branch sync: done 2026-10-22 (next due **2026-10-26**, twice a week; also before E1)

| Repo | Production branch merged | `exp/control-plane` | Conflicts and resolution (exp side) |
|---|---|---|---|
| niuu | `lexi/dev-api-integration` `33087ca2` (already in exp), and the **Thor release `11afe41d`** | `42188eb1` → **`9463bc2a`** | Two merges. First the notification images (`2a215f30`), then `11afe41d` itself, which only records ancestry: its descriptions/attachments are exp's `3e603dd6`/`a0100520`, and `000087` is byte-identical. **The migration** `000072_forge_notification_images` is **`000090`** here, because exp's `000072` is `startup_schema_history`. It has a lineage alias to the release's `000072` (same sha256) in `migrations/`, the package and the chart, so P1 adopts Thor's applied row. **Code:** question signals (F11b) and image fields kept together (the turn payload is `draft_payload` + question metadata); `main.py` runs both the artifact hold and the image worker; presented-file download keeps F2's durable copy first. **Release tests adapted to exp:** the header identity adapter, the principal-scoped session stream, NotificationService's required image deps. **Also fixed:** `test_mesh_lightweight` now models an old host's refusal as FastAPI's `extra_forbidden` list (F8's rule; it was broken on exp since `1b8fb72b`). Full suite: 27077 passed, 4 known box failures |
| lexi-frontend | `main` `836aea1` | `63c838a` → **`8999066`** | none (agent-cli diagnostic crumb). agent-service 1556 tests, agent-cli 12, `tsc` clean |
| lexi-ios | `main` at **build 2372** `cdd737c1` | `d161ceb7` → **`6282a827`** | `AppDelegate.didFinishLaunching`: exp's LEX-1403 misconfiguration guard stays first, and main's LEX-1402 Reply edit-menu install follows. `tools/release` 155 tests (1 known `/bin/zsh`), `release-check` OK. **The Swift build is unverified on Thor** (no Xcode): please have cp-ios-app or the next train build `exp/control-plane` on mini-m6 |

**Lane branches against the synced tips** (merge with `git merge-tree`; rebase of each lane's own commits in a scratch worktree): every lane merges cleanly and rebases cleanly.
- **niuu:** `exp/cp-forge-artifacts` (5 own commits), `exp/cp-forge-state`.
- **lexi-frontend:** `exp/cp-artifacts`, `cp-core` (2 own commits), `cp-needs-you`, `cp-o1a`, `cp-owners`, `cp-project-views`, `cp-seed`.
- **lexi-ios:** `exp/cp-fragment-ids`, `exp/cp-u1`, `exp/cp-u1x` are all already inside `exp/control-plane`, so rebasing them has nothing to apply.

**For cp-ios-app (mini-m6):** merge only, nothing was rebased or force-pushed. `exp/cp-u1`/`exp/cp-u1x` are untouched. The next time you merge `exp/control-plane`, you get build 2372's Reply feature (LEX-1402):
- `AppDelegate`: the Reply menu install after the LEX-1403 guard;
- `ChatScreen`/`ChatScreenModel`;
- `LexiInputBar` (+iOS, +Mac);
- `ForgeComposerState`, `ForgeSteerComposerBand`, `CodeSessionDetailView+Content`, `CodeSessionLiveModel`;
- LexiMarkdownKit's `MarkdownReply*`;
- `QAScenarioRegistry` + `MarkdownReplyQABoard`;
- `ReleaseNotes/2372.md` and CHANGELOG.

The sync branches are `exp/cp-x1-sync` in each repo.

**Reuse check (X1):** git merges only; the conflict helpers stayed in my scratchpad; nothing new in any repo besides the resolved code and the migration renumber/alias.

## Package 2 (relayed 2026-10-22): RF+E0, F8, C4, R9 — **done** (every row landed; C4's live checks run by cp-platform in R28, results in plan `platform/STATUS.md` "## C4 live checks (R28)")

| Row | Repo / branch | State | Evidence |
|---|---|---|---|
| RF + E0 | niuu, exp Forge | done (item 4 pending) | see the E0 row above; **config ask done** (round 24): `/instance` now reports `tailnet_name: thor.tail737f2a.ts.net`, the manifest's `app.forge` host, so C4's F7 cross-check matches (checked 2026-10-22; exp Forge at `924e5ba0`) |
| F8 m6 on par | niuu | **landed** `1b8fb72b` on `exp/control-plane` | `scripts/forge_parity_probe.py` (36 tests): read mode is statically zero-write and pinned to the probed host; write mode runs only on scratch ports 41000–41999 and never against a production instance id; it compares every Forge route. The branch in Thor's and m6's shapes is **ON PAR 39/39**. The gateway's masked-422 fix has 5 tests. Two review rounds. `docs/operator/forge-parity.md`. The m6 rollout itself is at P1 |
| C4 Artifact registry + lens | lexi-frontend | **landed** `3cf3b69` on `exp/control-plane` ; **deployed** on exp-cp (R26); cp-core B1 landed in `2077445` (test flipped by cp-core; 369 pass at `2077445`) | Lane `7b91eff`, after two review rounds. The golden gives exactly 26 rows (revisions = distinct provider versions, all 230 publications). R7-8a holds and every view validates as `ArtifactsView`. Bursts match the reference (thor 33, m6 17), with B1–B9/B1–B6 on the CP side; Forge's burst id and `rev` are used as given. Whole agent-service suite: 1476 passed + 1 expected-fail. `Project.artifacts` comes from C2v-b's `projectView` over C4's provider. Live checks, including the "to check" headline with B1 `2077445` live on exp-cp (R27): cp-platform runs them in R28 (the bearer tokens stay with cp-platform) |
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

Revised after a second review: the patch is now 241 lines, with a one-time repair on open (no data wipe needed), "to check" raised once, and a `rev` column instead of a count; it was verified in a scratch copy with 301 tests. C4's live-order test is `it.fails` until the patch lands. **Ask:** cp-core applies the patch (or its own equivalent) on `exp/control-plane`, and C4 flips the test.

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
