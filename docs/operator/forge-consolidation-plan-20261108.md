# Forge consolidation plan: one top-of-tree setup (2026-11-08)

Plan only. Nothing here has been executed; every step waits for Damien's yes. Facts were read
on Thor and the mini on 2026-11-08 between 14:45Z and 15:10Z, read-only. No secrets are in
this document; credential files are named by path only.

## The short answer

There is no technical need for two Forge **codebases**, and the setup should be unified now:
one top-of-tree (ToT) release, one promotion kit, one Guild. There is one real reason, today,
for two Forge **instances** (Home and Work as separate processes and databases): **data
separation of NVIDIA work from personal and Lexi work**. Home has no access boundary that
would keep its own sessions and agents (Lexi, OpenClaw through gbrain) from reading NVIDIA
transcripts, and the runners would need to reach Home's API, which today admits any caller
on the tailnet as the dev user. Billing, network and Horde credentials do not need separate
servers.

Recommendation, in order:

1. **Now, cheap, reversible:** move Work onto the same ToT release as Home (it is 623 commits
   behind), and join Home and Work in one Guild: one session list, one notification stream,
   one rollout kit. Retire Spark and the leftover units.
2. **Only if Damien wants a single server:** first build the access separation listed under
   (2) below, then attach the Horde runners to Home, keep Work read-only for 2 weeks, retire
   it. Never merge the databases.

> **Note, 2026-11-08 (after the LOADGAP round 2 close at 17:15 London):** A1's earliest clean
> window is after Damien has reviewed PRs #5262 and #5357. Until then, lanes P (5e4cea90) and
> R (d7b7c24b) keep running, and kse-node11 and work-kitbuild32-gke48-05 stay up.

## Today

| | Home Forge | Work Forge | Spark Forge |
|---|---|---|---|
| Unit / port | `volundr-forge.service`, `0.0.0.0:8080` | `volundr-forge-work.service`, `0.0.0.0:8280` | on Spark, `100.127.141.74:8080` |
| Release | `forge-span-30f20f4f` (ToT; `d6d7a0f31` pending) | `forge-work-4b40e677`, 2026-09-30, 623 commits behind | `forge-guild-ux-20260922` |
| Sessions | Lexi, iOS lanes, research; Guild with mini-m6 and Spark | 55, 5 running: Horde runners under horde-coordinator | 1, none running |
| Compute | `LocalProcessPodManager` (tmux on Thor) | `VmPodManager` + `niuu_hgx` `HgxRunnerSlotProvider` / `HgxProcessSessionRuntime`; 11 active hosts in `~/.config/niuu-forge-work/runners.json` (10 `gke-newhouse-48gb` pets, `work-build-kit-01` cattle) | none |
| Databases | `volundr-pg` (one Postgres container) | the same Postgres, `*_work` databases | its own |
| Web | `:8080` serves its own web; `niuu-forge-web` `:5173` (tailnet `:3001`) serves an older build `forge-attn-c42970e6` | `niuu-forge-work-web` `:5283` (tailnet `:3021`) | its own |

The Work code is not special: `forge/runner-provisioning`, `forge/vm-status-busy`,
`forge/runner-questions` and `work/release-20260929` are all in `lexi/dev-api-integration`,
and `VmPodManager` is in it. Work's value is its runner adapter and its live runner sessions.

## (1) Is there a real need for two servers?

| Concern | Today | Needs a separate server? |
|---|---|---|
| **Data separation, NVIDIA vs personal** | Separate databases on the same Postgres and the same host, user and disk. horde-coordinator: "Home sessions and agents (Lexi, OpenClaw via gbrain) must not read NVIDIA content. The ledger for NVIDIA work has `promotion_policy: never`." Home allows anonymous dev access (`auth.allow_anonymous_dev: true`), and its sessions read Forge freely through the Forge MCP and REST. | **Yes, today**, as a separate instance (not a separate machine). It becomes per-session or per-project only once Forge enforces a read boundary: NVIDIA sessions in their own project or tenant, which Home's own sessions, agents and the Lexi apps cannot list, read or message. That boundary does not exist yet. |
| **Billing (Anthropic)** | In practice per **session or workspace**, not per server. Thor's `~/.claude/settings.json` names `apiKeyHelper` (`~/.config/claude-nvidia/api-key-helper.sh`, the NVIDIA-org key). But its call log (`helper-calls.log`, each caller's folder) shows that only a few workspaces use it. On 11-08 every call (19) came from `/home/thor`, that is horde-coordinator. On 11-06/07 there were 71 calls from `/home/thor`, plus a few from `~/tmp/lex1950/proj`, `~/research/trickle-2026-11-04` and `~/build/kit-main-20261105`. The Lexi Forge sessions (ios-coordinator, niuu-consolidation, lexi, lexi-todo) never call it: they run on Damien's subscription. Runners use their own host setup. | **No, either way.** Which account a session bills to is already chosen per session or workspace, and stays so on one server. |
| **Billing / credentials (Codex)** | Work holds its own NVIDIA Codex grant (`~/.niuu-work/credentials`, `MiniModeCodexCredentialBroker`); runners get access-only tokens through Forge's credential route. Home has its own credential store (`~/.niuu/credentials`). | **No server**, but one Forge must choose the credential per session or profile and never show NVIDIA secrets to Home sessions (horde-coordinator: "scoped to the runner profile and never visible to Home sessions"). |
| **Horde credentials** | The HGX token file (from `hgx login`, not refreshable; Work's reads give 500 when it expires), the Tailscale issuer and admin token, runner SSH keys, and the runners' GitHub token for dfagnou. All are files on Thor that any Forge on Thor can be pointed at. | **No.** Config paths only, scoped to the runner profile. |
| **Network** | Runners reach Thor over Tailscale (`tag:forge-runner`, granted to `thor:8280`). Kit builds need GKE and Artifactory, which the runners already have. | **No server**, but an **ingress** is needed: runners must not get Home's whole API, which is anonymous-dev today. They need their own authenticated entry (session-bound `forge_session` tokens, route allow-list) on a separate port, with the tailnet grant moved to that port only. |
| **Audit / ledger** | The runners use Horde Ledger, independent of Forge. | **No.** |

## (2) The gap to run the HGX adapter on Home ToT

**Code**
- **Routing compute per session.** ToT has one pod manager per Forge. Home needs both: local
  tmux sessions and runner sessions chosen by `workload_config.compute_profile` (or by session
  definition). This is a new routing pod manager, the largest code item.
- **The runner ingress.** A second listener that serves only the runner callback routes, admits
  only `forge_session` tokens of runner sessions, and is the only port the `tag:forge-runner`
  grant reaches. `forge_session` tokens and the deny-by-default route allow-list exist; the
  local process pod manager is the only one that delivers them today (see
  `.claude/rules/architecture.md`), so `VmPodManager` delivery is part of this item.
- **The read boundary** for NVIDIA sessions (project or tenant scope on list, read, message,
  notifications, Forge MCP, gbrain session wells and the Lexi apps). Needed only for the single
  server. It is the largest risk, because every read path must honour it.
- **`niuu_hgx` on ToT.** The package (`~/repos/niuu-nvmodules`, branch `hgx-process-sessions`;
  Work runs the export `niuu-hgx-541f1cb`, and the checkout is 3 commits ahead at `80b9836`) was
  written against the 2026-09-30 code. Its provider and runtime ports must be checked against
  ToT on a scratch Forge first. The release build also needs a step that installs the module
  non-editable into the release venv.
- **Runner Skuld pin.** Runners launch `venv-4b40e677` (`venv_python` in Work's config). A ToT
  Forge needs `runner-host.py update <id> --commit <sha>` on every active runner, in the
  documented order: re-pin every active runner, then `venv_python`, then restart.

**Config** (all already in `~/.config/niuu-forge-work/config.yaml`, to be carried over):
`pod_manager` (`VmPodManager`, pool `work-aws`), `compute` (provider, auth and runtime
adapters, `hosts_file`, `profiles`), `integrations.module_manifest_files`, the
`codex_credential_broker`, `auth.default_tenant_id: default` (Ting on Work needs it), the
`skuldCodex` / `skuldClaude` runner definitions, the reaper timer
(`niuu-forge-work-reaper.timer`), and session liveness. horde-coordinator also needs a reaper
and liveness policy that never idle-stops runner sessions (14 days).

**Credentials** (paths, no values): the HGX token file, `~/.niuu-work/credentials` (the Work
Codex grant), `~/.config/forge-provisioner/tailscale-oauth.json` (auth keys),
`~/.config/tailscale/admin-api-token` (policy), and the runners' GitHub token. Each would be
scoped to the runner profile.

**Others' tools:** about 15 of horde-coordinator's fleet tools in `~/fleet-ops/fleet`
(`fsend.py`, `loadgap_watch.py`, the `kse_*` scripts and the watchers) hardcode
`127.0.0.1:8280`. horde-coordinator switches them when given a date.

## (3) Steps, each reversible

**A. Unify the setup (recommended now; no session moves):**
1. **Work onto ToT.** Stage a `forge-work-<sha>` release from `lexi/dev-api-integration` with
   Work's config and `niuu_hgx` installed, and prove it on a scratch Forge with one runner slot.
   Then do an API-only switch of `volundr-forge-work` with the promotion kit: brokers and runner
   sessions stay up, 120 s auto-rollback, and the rollback is the current `forge-work-733d8dd1`
   drop-in. Then re-pin runners one at a time, only idle ones, and only with horde-coordinator's
   yes. Reverse: the rollback drop-in, and `runner-host.py update --commit 4b40e677`.
2. **Guild-join Work and Home.** Add the other instance to each side's `niuu.instances`
   (`allow_plaintext` is already on). The Lexi apps then see one session list and one
   notification stream. Reverse: remove the entry. Damien decides first whether Home should
   see NVIDIA sessions at all: this step shows them in the Home list, which is the separation
   question again. The alternative is a one-way join (Work sees Home only).
3. **Retire Spark.** One idle session, no runners. Archive its database with the dump tooling,
   stop its unit, remove it from Home's Guild. Reverse: start the unit, re-add it.
4. **Retire leftovers on Thor:**
   - `volundr-forge-candidate` (`:8180`, dead): disable it.
   - `niuu-forge-candidate-web` (`:5183`, tailnet `:3011`): repoint `:3011` to Home's own web,
     then stop it.
   - `niuu-forge-web` (`:5173`, tailnet `:3001`, an older build): repoint `:3001` to Home's own
     web, or swap the build, then stop it.
   - The `lexiexp-stack` podman experiment: archive and stop.
   - Reverse each by re-enabling the unit or the serve mapping.

**B. Single server (only after Damien's yes on separation, and after the code items above):**
5. Build and prove routing compute, the runner ingress, profile-scoped credentials, and the
   read boundary, on a scratch Forge.
6. **New** Horde sessions start on Home. Existing Work lanes are never moved mid-task.
   horde-coordinator: "Moving live lanes mid-task is the risk, not the cut-over itself", and
   sessions are never stopped unless Damien stops them.
7. Work goes read-only (no new sessions) after LOADGAP closes, for 2 weeks; its 55 sessions
   stay browsable.
8. Retire Work: stop `volundr-forge-work`, `niuu-forge-work-web`, the reaper and
   `folio-forge-bridge@forge-work`; archive the `*_work` databases. Reverse until the archive
   is deleted: start the units again.

## (4) horde-coordinator's terms (asked read-only at 14:5xZ; answered by a2c32d25)

> **Don't interrupt:** `overnight-perf-20261105` (5e4cea90), LOADGAP lane P on kse-node12, active.
> `kse-node11-pr3205` (d7b7c24b), lane R on kse-node11, shows idle between steps but is in use.
> They are fixing CI on Kit PR #5357 now. Assume not before T-108/T-110 close (at least today and
> likely tomorrow); I'll message you when they are free.
> `kse-node12` (217a0cd2), `kse-node10` and `kit-referee` are idle, but Damien's rule is that
> sessions are never stopped unless he stops them. Keep their context; a move needs his yes per
> session. Hosts: keep `work-kitbuild32-gke48-05` and at least one KSE node (Damien, 30 Oct).
> Never touch `work-kitbuild-gke48-01` (his Kit "Master").
>
> **Windows:** New runners on Home: any time for *new* sessions once item 4 is in place. Stopping
> new sessions on Work: after LOADGAP closes.
>
> **Separation (Damien's call):** The NVIDIA transcripts, notifications and briefs would sit next
> to personal and Lexi sessions on Home. Home sessions and agents (Lexi, OpenClaw via gbrain) must
> not read NVIDIA content. The ledger for NVIDIA work has `promotion_policy: never`.
> Credentials: the HGX token, the Work Codex grant, and the runners' GitHub token for dfagnou.
> Per-session or per-profile settings could work if the secrets are scoped to the runner profile
> and never visible to Home sessions. Network: Kit builds need GKE (Artifactory); the runners are
> already reached over Tailscale from Thor, so that is not a blocker.
>
> **Needed from Home first:** the niuu-nvmodules Horde adapter and runner profiles; lifecycle and
> message grants for horde-coordinator on those runners; a reaper and liveness policy that never
> idle-stops runner sessions (14 d); the FAILED-session restart trap handled; the transcript
> archive. About 15 fleet tools in `~/fleet-ops/fleet` hardcode Work at `127.0.0.1:8280`; I can
> switch them when you give me a date.

## (5) Effort and risk

| Step | Effort | Risk |
|---|---|---|
| A1 Work onto ToT | 1–2 days: the `niuu_hgx` compatibility check, a scratch proof, the switch, runner re-pins one by one | Medium. 623 commits of drift in the runner path; mitigated by the scratch proof, the API-only switch and auto-rollback. Running runner sessions are not moved. |
| A2 Guild join | 1 hour | Low technically; it surfaces NVIDIA sessions on Home unless one-way, so decide separation first |
| A3 Spark retire | 1 hour | Low (one idle session) |
| A4 Leftovers | 1–2 hours | Low; the tailnet `:3001` and `:3011` mappings must be repointed first |
| B5 Routing, ingress, credentials | 3–5 days with reviews | Medium: new security-relevant code (runner ingress, credential scoping) |
| B5 Read boundary | 1–2 weeks, touching every read path (REST, Forge MCP, notifications, gbrain wells, apps) | High: one missed path leaks NVIDIA content to personal agents |
| B6–B8 Move and retire | Days, paced by LOADGAP and Damien's per-session yes | Low once B5 holds, because live lanes never move |

**Bottom line:** do A now. It gives one codebase, one rollout and one view, and it removes
the 623-commit gap and Spark. Do B only if one server is worth a real access-separation
project. Until then the second instance is the separation, and it costs little: the same
host, the same Postgres, the same release and kit.
