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
| Merge into `exp/control-plane`, record sha | pending | |
| Send sha to cp-platform; exp Forge redeploy | blocked | no `message` grant (see below) |
| F2 live proof: URL serves from exp Forge over the tailnet, from mini-m6 | pending | after deploy |
| LEX-1398 comment | pending | |

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
- **Merge conflicts to expect** (whoever lands second resolves them):
  - both lanes add migration `000088_*`; Forge's ledger is per filename, so only the number
    clashes; renumber theirs to 000089;
  - both lanes edit `NotificationService._candidate`;
  - both lanes vendor files into `tests/fixtures/control_plane/` (the README.md is the clash);
  - both lanes touch `pyproject.toml` only if they also exclude that folder from ruff.

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
- no cleanup of durable files on session delete;
- background upload retries are lost if the broker restarts before they succeed, with no rescan;
- two concurrent PUTs of different bytes under one id could interleave (only the session's own
  broker can PUT, with a fresh random id).

## Blockers

- **No cross-session message grant** (`forge_mcp.grants` lacks `message`). I can't send the sha to
  cp-platform or agree the boundary with cp-forge-artifacts myself. The coordinator has to relay
  it, or grant `message`.
