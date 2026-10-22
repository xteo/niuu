# Forge host parity (F8: mini-m6 on par with Thor)

mini-m6 runs `6c31bfe1`, an older Forge than Thor. F8 proves that the control plane's
branch serves the same attach, artifact and notification routes on both hosts, in each
host's own launch shape, and gives P1 a probe to rerun after the m6 rollout. F8 is built
and tested on the branch only; **m6's production Forge changes only at P1**
(lanes.md v9, RF row).

## The probe

`scripts/forge_parity_probe.py` (standard library only; m6's system python runs it).

```bash
# Safe against production: GET only, no identity, no redirects, 10 s timeout, ≤ 5 req/s.
python3 scripts/forge_parity_probe.py probe --base http://<thor>:8080 --label thor --out thor.json
python3 scripts/forge_parity_probe.py probe --base http://<m6>:8080 --label mini-m6 --out m6.json
python3 scripts/forge_parity_probe.py compare thor.json m6.json   # exit 0 and "ON PAR"
```

- **Read mode (default)** follows the control plane's production read rules
  (`exp/prod-read-allowlist.json`). It reads only routes that never write: the OpenAPI
  document (the 14 required routes), `/health` (judged on the Forge plugins only: m6 runs
  without ting, mimir, ravn and observatory on purpose), `/instance`, both artifact feeds
  and the notification feed. It makes no per-session reads, because they reach the owner
  lookup (`GET /sessions/{id}` reconciles with an UPDATE).
- **Write mode (`--write`)** runs the behaviour checks and refuses any host that is not
  loopback:
  - register a project;
  - create a session with `coordination` and a `dispatch_id`, then replay the create, which
    must return the same session;
  - read the session's project, detach it and re-attach it;
  - set a description;
  - read attachments and the session's notifications;
  - delete the session.

## Evidence (2026-10-22, branch `exp/cp-forge-artifacts`)

Two scratch Forges on Thor ran from the same branch, each in its host's launch shape:

| Shape | Launch | Database |
|---|---|---|
| **mini-m6** | m6's `forge-run.sh` exactly: `niuu platform up --mounts <m6's 20 mounts>`, `mode: mini`, `PROJECTS__INSTANCE_ID=mini-m6`, ting/mimir/ravn/observatory off, `LocalProcessPodManager` | embedded PG 17 **without pgvector or pgcrypto**, as on m6 |
| **Thor** | Thor's `start_platform.py` shape: `RootServer` with every plugin, `PROJECTS__INSTANCE_ID=thor` | external PG 17 |

The reports are in `docs/operator/f8-parity/`:

- `compare-branch.txt`: Thor shape vs m6 shape, `--write`: **ON PAR**, 29 of 29 checks.
- `compare-6c31bfe1.txt`: Thor shape vs m6 shape at **`6c31bfe1`** (m6's live build): **NOT ON
  PAR**. The probe catches the gap: `/instance` (F7), `/artifact-publications` and
  `/artifact-bursts` (F1), `PUT /sessions/{id}/description` and `GET /sessions/{id}/attachments`
  are absent (404).

**Attach is not the gap at `6c31bfe1`.** `GET` and `PUT /sessions/{id}/project`, a create
with `coordination` + `dispatch_id` (and its replay), and attaching an m6 session to a Thor
project **through Thor's guild gateway** (with and without `role`) all work against
`6c31bfe1` in m6's shape. The 501 recorded in ux `data-needs.md` (and design §V11/§V12)
doesn't follow from that code and config. If live m6 still answers 501 at P1, the cause is
in m6's live state, not missing routes. P1 reruns the probe and one attach through Thor's
gateway.

## m6 rollout at P1 (not before)

As in `forge-mini-m6` practice: new release clone + venv + the release's own
`build/pginstall` + the web dist; repoint `forge-run.sh`'s `R` and `NIUU_BUILD_VERSION`, and
kickstart the LaunchAgent. Run through m6-coordinator. Config changes:

```yaml
artifacts:
  notices_enabled: false   # RF: m6 gets F1 and F1n with notices off
# host_identity: not needed. The host falls back to PROJECTS__INSTANCE_ID (mini-m6) and the
# tailnet name to pod_manager.server_public_host (mini-m6.tail737f2a.ts.net).
```

Migrations since `6c31bfe1` are `000087`–`000089`, none of which needs an extension. After the
kickstart, run the read probe on both hosts and compare, then one attach through Thor's gateway.
