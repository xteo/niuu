# deploy-request (X1 sync, 2026-10-22), for cp-platform

1. **exp Forge ← niuu `exp/control-plane` `9463bc2a`.**
   - **New:** the Thor release's notification images (`notify` accepts `image`; `GET /notifications/{id}/image`).
   - **Migration** `000090_forge_notification_images`: a new table, idempotent. On P1 it adopts Thor's applied `000072` through the lineage alias.
   - **Optional config:** `notifications.images` (defaults work).
   - **Also in:** F8's gateway fix (`1b8fb72b`).
   - **Check:** `/health` OK; F8's probe `openapi` on the deployed doc shows `GET /api/v1/forge/notifications/{notification_id}/image`.
2. **exp-cp ← lexi-frontend `exp/control-plane` `8999066`.** `main`'s agent-cli change only; no CP change beyond what's deployed. Rebuild at your next round.
3. **lexi-ios `exp/control-plane` `6282a827`** (build 2372 merged): no deploy. Please have the next Lexi CP train or cp-ios-app build it on mini-m6; Thor can't build Swift.

---

# C4 live checks on exp-cp, for a token holder (cp-platform or ios-coordinator)

exp-cp runs `3cf3b69` (R26, receipt 0a564183) and reports `[cp] ready … ledger seq 839` (682 before C4).
forge-artifacts can't run these checks: `/v1/cp/views/*` takes the device bearer, `/exp/cp/status` takes the
operator bearer, and `/etc/lexiexp/secrets` is closed to this session, as it should be. Please run them and put the
outputs (no tokens) in your STATUS, or grant a read-only token file:

```bash
DEV=$(cat <device token file>); OP=$(cat <operator token file>); CP=http://127.0.0.1:9600
curl -s -H "Authorization: Bearer $DEV" "$CP/v1/cp/views/artifacts?scope=all&limit=200" | jq '{total, rows: (.rows|length), opens: ([.rows[].opens]|unique), ids: ([.rows[].id|select(startswith("artifact:claude_ai:"))]|length)}'
#   expect: opens ["system_browser"]; 26 claude.ai ids from the boot-0 replay (plus any exp-Forge rows)
curl -s -H "Authorization: Bearer $DEV" "$CP/v1/cp/views/artifacts?scope=recent" | jq '{total, n: (.rows|length), cursor}'   # n ≤ 10
curl -s -o /dev/null -w '%{http_code}\n' -H "Authorization: Bearer $DEV" "$CP/v1/cp/views/artifacts?scope=project"  # 400
curl -s -H "Authorization: Bearer $OP" "$CP/exp/cp/status" | jq .artifacts   # last poll ok, f7.matches_manifest true, config_errors 0
```

After `2077445` (cp-core B1) is deployed, the live "to check" headline check is: a real exp publication, then an
agent notice linking it, then within one poll the burst is in Activities' "to check" for its project. That needs a
live provider key (boot-0 gate), the same as E0 item 4.

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

## Follow-up (2026-10-22, cp-platform R25): the F2 upload through the facade

The merge carrying `beffb6ef` (sha in the session description). No config change: the facade's cap is Forge's
`presented_files.max_bytes`. Then rerun check 2: a presented file's URL from mini-m6, before and after stop.

