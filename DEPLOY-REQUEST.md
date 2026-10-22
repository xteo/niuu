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
