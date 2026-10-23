# Deploy request: restore Claude notify discovery after R34 → cp-ops

Deploy niuu `exp/control-plane` at the no-ff merge SHA in cp-dev's session description (lane `exp/cp-dev-notify-redeploy`). Independent review and tests are complete.

The cause is selective native schema rejection, not a disconnected Forge MCP transport: Claude 2.1.309 omits notify when its root schema has the `allOf` conditional introduced with structured asks. The fix removes only that advertised root conditional; server-side strict `ask`/`question:true` validation and the nested item schema are unchanged.

- Rebuild exp Forge and its broker artifact through the ordinary rollout. No migration, config, credential, mount, network or production change.
- Claude caches its accepted tools and Forge advertises `listChanged:false`. Refresh the two authorized exercise broker/CLI processes through their normal lifecycle after deployment; do not restart unrelated sessions. The known exercise IDs are coordinator `2d0ae16e-58b7-4bd8-84d0-c0371b8cf52b` and build `79f6e58d-e67b-4fb7-9f18-b3419dc58ff4` in project `375b6772-fb1d-414a-9dc5-069f7841db13`. Preserve the existing stopped docs session and all fixture/question history.
- Acceptance must inspect native Claude's accepted tool list and find `notify`, not merely a connected status or raw MCP tools/list. The isolated catalog-only regression test uses SDK initialize/get_mcp_status and no model query; its exact test is `tests/test_skuld/test_forge_mcp_native_catalog.py`, opt in with `FORGE_LIVE_CLI=1` and `-m live_cli`. A new question/provider prompt is unnecessary for this proof; leave operational question creation to the coordinator unless separately authorized.
- Report the deployed SHA and native catalog result through the coordinator. R34's failed notify evidence remains valid history. Part A status acceptance is already complete and the build view remains a separate, unimplemented C3b item.

Validation: 270 regressions, three real broker process tests, one native CLI catalog test covering transcript and feed; 95.97% changed-module coverage. Independent review approved and proved the native test fails when the historical root conditional is restored in memory. Normal rollback returns the old discovery defect; no data/schema rollback is needed.

---

# Deploy request: LEX-1421 session status/context → cp-ops (23 Oct 2026)

Deploy the niuu `exp/control-plane` merge SHA in cp-dev's Forge description. Source lane: `exp/cp-dev-session-status`. Implementation, tests and independent review are complete; no deployment has been performed by cp-dev.

1. Rebuild the exp Forge service and normal broker artifact. Apply packaged startup migration `000092_session_usage_status`; it adds only nullable derived usage columns and a partial lookup index in the existing session/log tables. Root, CLI and Helm copies are identical. No secret or mount changes, no new provider keys, and no production action.
2. Existing brokers adopt the transport instrumentation through their ordinary lifecycle; do not restart unrelated sessions for this exercise. API fields are additive and remain null until authoritative observations arrive. SDK resumes can have partial totals; tmux replays its main native JSONL from its own bounded hook-driven reader. Do not use legacy estimated token counts as context.
3. Create fresh exp exercise sessions and verify the public `/api/v1/forge/sessions/{id}/status`, list `context`, and existing `session_updated.data.context`. Use the numeric fixtures in `tests/test_skuld/test_session_usage_projection.py` and `tests/test_domain/test_session_usage.py`, without a provider prompt. Repeated native response IDs/snapshots must not increase totals; old samples after compaction must not restore the context ring. Stop only these new sessions and report IDs/results through the coordinator so cp-dev can record acceptance.

Validation: final 496-case API/config/log/facade regression run, ten real isolated-PostgreSQL tests (including HTTP ingest → status/list/live, concurrency, retry/restart, malformed telemetry and up/down/up migration), 94.23% numeric-projector coverage; transport suites 463 Codex / 160 Claude SDK+tmux; independent review clear. Exact command/output receipts are in cp-dev evidence, not in a production database.

Rollback: rolling back code can leave the additive columns in place. If explicitly applying the packaged down migration, it removes only derived summary/checkpoints/index and preserves every raw event; replay/backfill is a separate operation, never automatic on status GET. No deployment to production Forge is authorized by this request. iOS wire semantics are in STATUS.md, “Contract for the iOS lanes”.

---

# Deploy request: LEX-1412 structured notify asks → cp-ops (23 Oct 2026)

Deploy this reviewed niuu `exp/cp-dev-structured-questions` landing with the matching frontend CP answer consumer after both merge SHAs are in cp-dev's description. The frontend consumer awaits ios-coordinator's canonical contract 1.1.0 commit; do not treat a lane SHA as its deployment target.

- Rebuild exp-forge via the existing rollout. No new environment, secret, mount, migration or message API behavior. Optional ask uses existing notification metadata JSONB and durable log projection; old notifications retain their old wire.
- New brokers receive the notify tool's structured question/action schema and generated forge-notify skill. Existing brokers refresh through their ordinary lifecycle. No extra session start, notification or real LLM call is requested as a probe.
- Validation: 363 focused tests, three real stdio/broker process tests, independent review and Ruff pass. CP's companion uses existing local message idempotency and delivery receipts; see the frontend STATUS Contract for cp-ios-app.

---

# LEX-1406: persistent project side-quest host/folder defaults

Lane `exp/cp-dev-project-defaults`; implementation and tests independently reviewed; exact landing SHA is in cp-dev's Forge description. **No deployment has been performed. cp-ops deploys exp only.**

Deploy the paired niuu and lexi-frontend changes. Niuu's startup migrator applies `000091_project_side_quest_defaults` from the usual packaged migration set; the root and Helm copies match. This only constrains an optional key in existing `forge_projects.document` JSONB. It adds no table, column, backfill, config setting, credential or secret mount. The exp Forge local mount policy remains the authority for allowed directories.

The app uses its existing project update API, pinned to the served registry UUID:

```http
PATCH /api/v1/forge/projects/<project UUID>?instance_id=<served registry UUID>
Content-Type: application/json

{"revision":1,"side_quest_defaults":{"host":"thor-exp","local_path":"/home/exp/workspaces/cp-exercise"}}
```

Use the current returned revision, not the illustrative `1`. To clear, send `{"revision":<current>,"side_quest_defaults":null}`. Omission leaves the field unchanged. A persisted null suppresses legacy cp.json fallback; a missing key preserves it. The response contains the new revision, and a stale write is 409. Host is the Forge project host identity, not the registry UUID. The folder must already exist, be accessible and writable, and pass local mount prefix policy. Defaults can only be edited on the project's authoritative served host; the facade refuses remote selection before forwarding.

Acceptance after deploy: use a scratch project with no cp.json path, save a permitted exp folder with the current revision, then allow one ordinary CP poll. Project composer and Configuration must show the same saved defaults and side-quest availability. Clear with the next revision, allow one poll, and confirm unavailable; repeat a stale revision and confirm 409/no overwrite. The source test exercises that complete API→poll→view path without live services or an LLM, and additionally proves that an explicit clear cannot restore a legacy configured path.

Rollback: the down migration removes only this new key so an older strict project decoder can read the rows. This discards saved defaults, returning those rows to legacy fallback behavior; it preserves all other document fields and row metadata. Apply rollback only as part of the matching exp release rollback.

Cross-repo test: `CP_PROJECT_DEFAULTS_FIXTURE_COMMAND` is JSON argv for frontend's `node_modules/.bin/tsx` and `control-plane/views/__tests__/project-defaults-consumer.fixture.ts`; run `PYTHONPATH=src python -m pytest -q -m integration tests/test_projects/test_side_quest_defaults_consumer.py --no-cov`. Routine suite: `python -m pytest -q tests/test_projects tests/test_niuu/test_rest_volundr.py tests/test_niuu/test_rest_volundr_forge_session.py --no-cov`. PostgreSQL tests use only a disposable cluster and `FORGE_HISTORY_TEST_DATABASE_URL`, never the exp or production database.

# Config follow-up for exp Forge (cp-platform), from E0 on 2026-10-22

exp Forge `e2208da8` is live and E0 items 1–3 pass (`docs/operator/e0/e0-evidence.json`). One config gap:
`GET /api/v1/forge/instance` answers `tailnet_name: "100.66.123.128"` because `host_identity.tailnet_name` is unset
and it falls back to `server_public_host`. C4 builds file URLs on the host's tailnet name (wire: never `*.xteo.mesh`,
phone-reachable). Please add to exp Forge's config, then restart it:

```yaml
host_identity:
  tailnet_name: thor.tail737f2a.ts.net
```

Check: `/instance` → `"tailnet_name": "thor.tail737f2a.ts.net"`. No code change, no migration.

---

# Deploy request: forge-artifacts → exp Forge (RF), for cp-platform

**Sha:** niuu `exp/control-plane` at **`dbfcf260`** (F1/F1n/F7/F1t `cf1b778a`; migration renumbered to `000089` in `16e0e48d`).

**What it adds to exp Forge:** F1 artifact publications, F1n burst notices (120 s durable hold),
F7 `GET /instance`, F1t type/marker. Migration `000089_forge_artifact_publications` (also under
`src/cli/migrations/volundr/`, so the wrapper's `prepare_schema` applies it).

**Config (optional; defaults work):**
```yaml
host_identity:
  host: thor-exp                        # default already: PROJECTS__INSTANCE_ID (thor-exp)
  tailnet_name: thor.tail737f2a.ts.net  # default: server_public_host
```
Notices need `notifications.enabled` (default true). Nothing reaches production; no new egress.

**After the rebuild, checks (E0, run by forge-artifacts):**
1. `GET :8380/api/v1/forge/instance` → `host: thor-exp`.
2. `GET :8380/api/v1/forge/artifact-publications?scope=local` → 200, empty feed.
3. Replay: the capture manifest's Thor rows appended to an exercise session's log give the
   golden (33 bursts; 25 notices with forced live), and the admin backfill emits 0 notices.
4. A real exp publication (needs a live provider key, still gated in boot-0) → one notice after 120 s.


---

# Deploy request: forge-state → exp Forge, for cp-platform

**Sha:** niuu `exp/control-plane` at the forge-state merge commit (LEX-1398; lane branch
`exp/cp-forge-state`). It includes forge-artifacts above.

**What it adds:**
- F12: no stale `active` after a turn ends;
- `last_work_at`, `tool_open` and `open_controls` on every session;
- F11b: `question` and `about_question` on notify;
- F2: durable presented files with tailnet URLs.

It adds migration `000088_session_work_facts` (also under `src/cli/migrations/volundr/`).
Brokers pick up the Skuld changes when their sessions start on the new release.

**Config (needed for F2; the store is off by default):**
```yaml
presented_files:
  enabled: true
  public_base_url: "http://thor.tail737f2a.ts.net:8380"
  store:
    adapter: "volundr.adapters.outbound.local_presented_files.LocalPresentedFileStore"
    root: "<a persistent dir under /home/lexiexp-forge>"   # default ~/.niuu/presented-files
```

**After the rebuild (forge-state runs these):**
1. `GET :8380/api/v1/forge/sessions` shows `last_work_at`, `tool_open`, `open_controls`.
2. An exercise session presents a file. From mini-m6, its `url` (on `thor.tail737f2a.ts.net:8380`)
   returns the bytes, and still does after the session is stopped.
3. A notify with `question: true` lists the `question` field in `GET /notifications`.

## Follow-up (2026-10-22): F2 gaps, no config change

The merge of `exp/cp-forge-state` that carries `0deedaba` (the sha is in this session's description,
`deploy-request: niuu <sha>`). It can ride the round after 23; nothing in the overlay changes.

- Deleting a session removes its durable presented files.
- A broker restart resumes uploads that had not landed.

Check after the rebuild: delete a throwaway session that presented a file; its folder under the store
root (`presented_files.store.root`) is gone, and its file URL answers 404.

## Follow-up (2026-10-22, cp-platform R25): the F2 upload through the facade

The merge carrying `beffb6ef` (sha in the session description). No config change: the facade's cap is Forge's
`presented_files.max_bytes`. Then rerun check 2: a presented file's URL from mini-m6, before and after stop.

# cp-dev R17: authenticated seed overflow retrieval

Lane: `exp/cp-dev-r17-overflow`; independently reviewed; landing SHA is in cp-dev's Forge description. **cp-ops deploys exp only.**

1. Deploy the companion lexi-frontend CP change serving `GET /v1/cp/forge/project-more?section=&offset=&sha=`. It verifies the existing `forge_session` bearer through exp Forge before deriving the project and enforcing Private policy.
2. Deploy this niuu landing to exp Forge and set the nonsecret environment variable `SKULD__FORGE_MCP__PROJECT_MORE_ENABLED=true` on the exp-forge container. LocalProcessPodManager passes it to newly launched brokers. The default is false; existing brokers need a normal lifecycle restart to read new settings/code. Do not restart live sessions for this rollout.
3. The broker always uses `http://10.89.0.21:9600/v1/cp/forge/project-more`, `trust_env=False`, no redirects, and its existing session token. No new credential, secret mount, production change, or proxy permission is needed. `project_more` takes only section/offset/sha, and legacy/non-exp tool catalogs are unchanged.
4. Verify with an empty-prompt exp side-quest broker's built-in MCP: retrieve an omitted section using its seed's `project_more` hint, follow `next_offset` with the returned `sha`, then stop the scratch session. A Private project and a stopped/restarted session credential must be refused. This deployment acceptance probe is for cp-ops; the implementation tests used only local fixtures and no LLM.

Cross-repo regression command (set paths to the local checkouts):

```sh
CP_PROJECT_MORE_FIXTURE_COMMAND='["/path/to/lexi-frontend/services/lexi-agent-service/node_modules/.bin/tsx","/path/to/lexi-frontend/services/lexi-agent-service/control-plane/views/__tests__/project-more-consumer.fixture.ts"]' \
  PYTHONPATH=src python -m pytest -q -m integration tests/test_skuld/test_project_more_consumer_e2e.py --no-cov
```

This test starts the actual CP fixture, real Skuld broker, and real stdio MCP without a model. Its isolated transport shim maps only the fixed exp origin to the ephemeral fixture. The final omitted question must be recovered through complete text-channel pages; no device key is injected into the agent.
