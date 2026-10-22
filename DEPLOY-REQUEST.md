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
