# deploy-request: lexi-frontend `exp/control-plane` `3cf3b69` → exp-cp (C4), for cp-platform

C4 (the artifact registry and the `/v1/cp/views/artifacts` lens) has landed. exp-cp runs `6dbdc4f` (round 24), which has no C4: rebuild it at **`3cf3b69`**. exp Forge's tailnet name (round 24) is confirmed.
- **No new required env.** The artifacts poller uses the same exp-Forge base as cp-core's ingest
  (`CP_EXP_FORGE_BASE`) and `CP_EXP_FORGE_POLL_MS`.
- **Optional:** `CP_ARTIFACT_FILE_BASES_FILE`, a JSON `{host: base}` of phone-reachable Forge file bases for
  hosts other than thor-exp. thor-exp's base comes from the manifest's `app.forge`. A malformed file is
  logged and skipped.

**Checks after the rebuild (forge-artifacts runs these):**
1. `GET /v1/cp/views/artifacts?scope=recent` → 200, validating as `ArtifactsView`. Every row has
   `opens: system_browser`.
2. From the boot-0 replay: 26 claude.ai rows in `scope=all`.
3. `GET /exp/cp/status` shows `artifacts` (the last poll, the F7 cross-check, skipped and config_errors).

**Known gap until cp-core applies B1** (`control-plane/requests/` on lexi-frontend `exp/cp-artifacts`): live bursts
don't become "to check". B1 carries a one-time repair, so no data wipe is needed when it lands.

---

# Optional exp Forge update (cp-platform): niuu `exp/control-plane` `1b8fb72b`

F8 changes one gateway behaviour. An attach carrying `role`/`parent` that the host refuses for a domain reason
(for example "parent is not a project member") now returns that 422 instead of 501 "Update this session's Forge
host" (`rest_volundr.py` `_rejects_unknown_fields`). There is no migration and no config change. Useful before C4
and U5 exercise attach on exp; otherwise ride the next exp deploy.

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
