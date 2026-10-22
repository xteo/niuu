# Forge host parity (F8: mini-m6 on par with Thor)

mini-m6 runs `6c31bfe1`, an older Forge than Thor (`11afe41d`). F8 proves that the control
plane's branch serves the same attach, artifact and notification routes on both hosts, each
in its own launch shape, and gives P1 a probe to rerun after the rollout. F8 is built and
tested on the branch only; **m6's production Forge changes only at P1** (lanes.md v9, RF).

## The probe

`scripts/forge_parity_probe.py` uses the standard library only, so m6's system python runs it.
Reuse check: `scripts/forge_live.py` creates billable provider sessions and needs httpx, and
`scripts/extract_openapi.py` builds a hand-picked set of node routers with mocks rather than
what a host serves under its mount list, so neither could carry a cross-host comparison.

```bash
python3 scripts/forge_parity_probe.py probe --base http://<host>:<port> --label thor --out thor.json
python3 scripts/forge_parity_probe.py probe --base http://<host>:<port> --label mini-m6 --out m6.json
python3 scripts/forge_parity_probe.py openapi recorded-openapi.json --label thor-live --out live.json
python3 scripts/forge_parity_probe.py compare thor.json m6.json [--exclude "GET /api/v1/forge/…"]
```

- **Read mode (default) is statically zero-write, but it is not on the production
  allow-list.** `exp/prod-read-allowlist.json` is profile boot-0, with no routes. Running it
  against production Thor or m6 needs the allow-list's owner (the judge and cp-platform) to
  add these routes first.
  - **What it sends:** GET only, with no identity headers, no `dev*` params and no proxy.
    It never follows a redirect, times out after 10 s and makes at most one request per
    200 ms.
  - **What it reads:** the OpenAPI document, `/health` (judged on the Forge plugins only, since
    m6 runs without ting, mimir, ravn and observatory on purpose), `/instance`, and the two
    artifact feeds and the notification feed.
  - **Pinning:** each feed is read with `scope=local` and must answer with `/instance`'s
    `instance_id` and `host`.
  - **Why these routes are zero-write:** with no identity, `extract_principal` returns the
    dev-user principal and provisions nothing (allow-all mode), or answers 401 (token or Envoy
    mode). The feeds and `/instance` only SELECT, or are static.
  - **No per-session reads:** they reach the owner lookup, which writes.
- **Write mode (`--write --expect-instance-id <id>`)** runs the behaviour checks on a scratch
  Forge only.
  - **Refusals:** a host that isn't loopback, ports 8080, 8280, 8380, 9500 and 9600, and any
    Forge whose `/instance` id isn't the expected one. Nothing is written before the id
    matches.
  - **Checks:**
    - register a project;
    - create a session with `coordination` and a `dispatch_id`, then replay the create,
      which must return the same session;
    - read the session's project, detach it and re-attach it;
    - set a description;
    - read attachments and the session's notifications.
  - **Clean-up:** stop and archive the session, then archive the project. The clean-up steps
    are checks too.
- **`compare`** needs two or more reports. It is on par only when:
  - every host passes every check the others ran, with the same status;
  - every Forge route (`/api/v1/forge/*`, every method) any host serves is served by all of
    them (minus explicit `--exclude`s);
  - live hosts report distinct instance ids and hosts.

## Evidence (2026-10-22), `docs/operator/f8-parity/`

The scratch Forges ran on Thor, on loopback ports 41180–41183, each in a host's launch shape:

| Shape | Launch | Database |
|---|---|---|
| **mini-m6** | m6's `forge-run.sh` exactly: `niuu platform up --mounts <m6's 20 mounts>`, `mode: mini`, `PROJECTS__INSTANCE_ID=mini-m6`, ting/mimir/ravn/observatory off, `LocalProcessPodManager` | embedded PG 17 **without pgvector or pgcrypto**, as on m6 |
| **Thor** | Thor's `start_platform.py` shape: `RootServer` with every plugin, `PROJECTS__INSTANCE_ID=thor` | external PG 17 |

1. **The branch, Thor shape vs m6 shape, `--write`: ON PAR**, with 39 of 39 checks the same
   (`compare-branch.txt`).
2. **Live Thor (`11afe41d`, the judge's recorded OpenAPI) vs the branch in m6's shape: NOT ON
   PAR** (`compare-live-thor-vs-branch-m6.txt`). The branch serves 10 routes live Thor doesn't
   yet (F1, F7 and others). Live Thor serves exactly **one** Forge route the branch lacks:
   **`GET /notifications/{notification_id}/image`**, the release-only notification images
   (`11afe41d` is not an ancestor of `exp/control-plane`). **P1 needs that release commit
   merged into `exp/control-plane` (cp-platform's X1, the branch sync) before the rollout.**
   Otherwise m6 is still not on par with Thor, and Thor itself loses the route at P1.
3. **Live Thor vs live m6 (both recorded): NOT ON PAR** (`compare-live.txt`). That is today's gap.

## Attach: what the m6 501 is (`m3-*.json`)

Scratch m6 sessions were attached through Thor's guild gateway:

| Case | Thor `11afe41d` → m6 `6c31bfe1` | branch → branch |
|---|---|---|
| attach; Undo (detach); attach with `role`; attach with a cross-host (Thor) parent | 200 | 200 |
| attach with a local parent that isn't a project member | **501** "Update this session's Forge host…" | **422** "Referenced local session is not a project member" |
| `PUT /sessions/{id}/description` | **501** (the route is missing at `6c31bfe1`) | 200 |

So a well-formed attach works against m6 today. A 501 is either the description route
(missing at `6c31bfe1`), or a **masked domain refusal**: both revisions' gateways turned
*any* 422 into "update the host" when `role` or `parent` was sent, and the control plane
always sends `role`. m6 is itself a gateway in front of its node, so its own `6c31bfe1`
gateway masks the error before Thor's sees it.

**The fix is on the branch** (`rest_volundr.py` `_rejects_unknown_fields`): 501 only when the
host's request validation refused `role` or `parent` as unknown (`extra_forbidden` at
`["body", <field>]`). Any other 422 is the host's own answer and passes through. Tests:
`tests/test_niuu/test_rest_volundr_project_assignment.py`.

**The F8 item "m6 accepts `coordination` with `dispatch_id` on create"** is satisfied by the
scratch evidence at the revision m6 will run: the m6 shape creates, replays (same session) and
keeps the coordination. Write mode never runs against production; P1's own check is the read
probe plus one attach through Thor's gateway.

## m6 rollout at P1 (not before)

Follow the `forge-mini-m6` practice:
1. a new release clone, its venv, the release's own `build/pginstall` and the web dist;
2. repoint `forge-run.sh`'s `R` and `NIUU_BUILD_VERSION`, and kickstart the LaunchAgent.

Run it through m6-coordinator. Config:

```yaml
artifacts:
  notices_enabled: false   # RF: m6 gets F1 and F1n with notices off
host_identity:
  tailnet_name: mini-m6.tail737f2a.ts.net   # the default (server_public_host) is the same today
# The host falls back to PROJECTS__INSTANCE_ID (mini-m6).
```

The migrations since `6c31bfe1` are `000087`–`000089`; none needs an extension. After the
kickstart:
1. the read probe on both hosts (once the allow-list carries its routes), then `compare`;
2. one attach through Thor's gateway, with `role`.
