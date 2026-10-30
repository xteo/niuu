# Promotion note: Forge MCP grants and preview size (LEX-1506, LEX-1655)

**Status: PREPARED, not applied.** The release trees are built on both hosts.
Nothing live has changed: the databases were only read and the configurations
were validated from scratch copies.

This release replaces the prepared LEX-1536 release
(`docs/operator/lexi-promotion-lex1536.md`). It contains that release, so the
`speak` intent goes live in the same switch. Use that note's mechanics, checks and
rollback unchanged, with the names and sha below.

| | Thor (Home Forge, `:8080`) | mini-m6 (`100.92.69.62:8080`) |
|---|---|---|
| Runs today | `forge-consolidated-20261026` (59c974c8a), clean tree | `forge-m6-consolidated-20261026` (59c974c8a), clean tree |
| Promote to | `forge-grants-ef6dead3` = `lexi/dev-api-integration` **ef6dead37** (`ef6dead37ecb5968de25ae14e95b734d0bc1d6a5`) | `forge-m6-grants-ef6dead3`, same sha |
| Built | `~/.local/share/niuu/releases/forge-grants-ef6dead3`: venv and web dist; `build_identity` gives `dirty: False` | `~/.local/share/niuu/releases/forge-m6-grants-ef6dead3`: venv, `pginstall` symlink, web dist rsynced; `dirty: False` |
| Web staged | `~/.local/share/niuu-forge-web/releases/forge-grants-ef6dead3` (live `config.json`/`config.live.json` copied, `demoMode: false`) | served from the release |
| Migrations at boot | **1 executed** (`000094`), as in the LEX-1536 note §2 | the same, with `ting/` unchanged |
| Config change | **none**. The new keys use their defaults, checked against each host's live file. | **none** |
| Rollback target | `forge-consolidated-20261026` | `forge-m6-consolidated-20261026` |

## What goes live

- **LEX-1536:** `speak` on notifications (see that note).
- **LEX-1506:**
  - `GET`/`PUT /api/v1/forge/sessions/{id}/forge-mcp`. It is operator-only and
    audited; see `docs/operator/forge-mcp.md`.
  - `forge_mcp.role_grants`, by default `coordinator: [lifecycle, message]`. Every
    session filed as a project coordinator gets both grants **at its next start**.
    Running sessions keep their current credential until then.
- **LEX-1655:** `?max_edge=` on the tool-result preview, with the headers
  `X-Preview-Max-Edge` and `X-Preview-Edge`. The default stays 400 px.

## Web on Thor

Thor's live web is still `forge-ux-cleanup-31906a0a-midnight-questions-27b4ff14`;
B3 did not swap it. The staged build is the consolidated app. The coordinator
decides whether to swap it in this switch (B3 §4.1 step 6) or to leave it as it is.
The API changes need no web change.

## Extra smoke (after the LEX-1536 rows)

Run these against the scratch session. Set `F=$A/api/v1/forge` and `S=<scratch id>`.

| # | Check | Command | Expect |
|---|---|---|---|
| G1 | read grants | `curl -s $F/sessions/$S/forge-mcp` | `grants: []` and `applies_at: "next_start"` |
| G2 | set grants, audited | `curl -s -X PUT $F/sessions/$S/forge-mcp -H 'content-type: application/json' -d '{"grants":["message"]}'`, then `curl -s "$F/../audit/events?event_type=volundr.session.forge_mcp_grants_changed&limit=1"` | `grants: ["message"]`; one audit event for `$S` |
| G3 | session token refused | from inside the scratch session, its broker's token on `PUT …/forge-mcp` | `403` |
| G4 | applies at restart | stop and start `$S`, then call the Forge MCP `environment` in it | `grants` includes `message` |
| P1 | preview size | pick an image tool result `T`, then `curl -sD- -o /dev/null "$F/sessions/<id>/tool-result/$T/preview?max_edge=1200"` and the same with `max_edge=2000` | `200` with `X-Preview-Max-Edge: 1200`, then `422` |

If G2's audit read answers 404 on the Guild host, read the audit events on the
embedded Volundr instead. The row is written either way, and the G2 `PUT` itself
proves the write succeeded.
