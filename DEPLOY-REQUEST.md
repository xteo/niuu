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
