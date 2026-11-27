# Google Antigravity (agy) sessions

The `skuldGemini` session definition runs Google's Antigravity CLI (`agy`) in tmux,
the way `skuldClaudeInteractive` runs Claude Code. Forge shows its replies, tool calls,
approval menus and questions, and reports `agy`'s own token counts.

Transport: `skuld.transports.tmux_agy.TmuxAgyTransport`. Design and recorded behaviour:
[docs/design/skuld-antigravity-gemini.md](../design/skuld-antigravity-gemini.md).

## Set up a host

1. Install `agy` into its own home, so its login and settings stay apart from yours:

   ```bash
   AGY_HOME=~/.local/share/skuld-agy/home
   mkdir -p "$AGY_HOME/bin" && chmod 700 "$AGY_HOME"
   curl -fsSL https://antigravity.google/cli/install.sh -o /tmp/agy-install.sh   # read it first
   env -i HOME="$AGY_HOME" PATH=/usr/bin:/bin bash /tmp/agy-install.sh --dir "$AGY_HOME/bin"
   ```

2. Sign in once, as the person whose Google account the sessions will use:

   ```bash
   cd "$AGY_HOME" && env -i HOME="$AGY_HOME" PATH=/usr/bin:/bin TERM=xterm-256color \
     AGY_CLI_DISABLE_AUTO_UPDATE=true "$AGY_HOME/bin/agy"
   ```

   Choose Google OAuth, finish the browser step, then exit. On a host without a keyring
   `agy` keeps the token in a file under `$AGY_HOME/.gemini`.

3. Configure Skuld (config file or `SKULD__AGY__*`):

   | Key | Meaning |
   |---|---|
   | `agy.binary` | Path to the `agy` executable. |
   | `agy.home` | The `HOME` above. Required; set it in the platform environment (`SKULD__AGY__HOME`), see step 4. |
   | `agy.expected_version` | The `agy --version` Skuld was tested with. Required: `agy` updates itself, and a new build can redraw the menus Skuld reads, so any other version refuses to start. |
   | `agy.mode` | `auto`, `accept-edits` or `plan`. |
   | `agy.allow_session_mcp_servers` | Without YOLO, in `auto` mode: the session's own MCP servers run without asking (default `true`). See Permissions. |
   | `agy.signed_out_grace_s` | How long agy's sign-in menu may show at start before the home counts as signed out (default 10 s). |
   | `agy.human_input_only` | Default `true`. See below. |
   | `agy.turn_max_seconds` | Longest turn without a Stop hook; reaching it ends the turn with an error. |
   | Others | Polling, timeouts and pane size; see `AgyRuntimeConfig` in `src/skuld/config.py`. |

4. Give the Forge platform the agy settings in its environment, so every session broker
   it starts inherits them. On a systemd Forge, put them on the `ExecStart` line of the
   release drop-in, so a rollback removes them with the release:

   ```
   SKULD__AGY__HOME=<the home from step 1>  SKULD__AGY__BINARY=<home>/bin/agy
   SKULD__AGY__EXPECTED_VERSION=1.5.16      SKULD__AGY__HUMAN_INPUT_ONLY=true
   ```

5. In Forge, connect **Google Antigravity (host sign-in)** under AI providers, naming the
   same home. The launcher offers Gemini only to people who have connected it. The
   connection does not deliver the home to the session on a mini-mode Forge: that Forge
   has no secret injector, so provider credentials never become session variables, and a
   session without `SKULD__AGY__HOME` in the platform environment fails with
   `agy.home is not set`.

   Skuld launches `agy` with `AGY_CLI_DISABLE_AUTO_UPDATE=true` (agy ignores `1`). Updates
   started by hand still happen; after one, re-test and raise `agy.expected_version`.

## Permissions: YOLO by default

`skuldGemini` launches agy in YOLO mode (`skipPermissions: true` in the definition, Damien's
call on 27 Nov 2026): `--dangerously-skip-permissions`, which agy's `/config` shows as
"Tool Permission: always-proceed (overridden by dangerously-skip-permissions)". It covers
shell commands, file edits, web requests and every MCP tool. Two settings it does not
cover are passed for the launch only (`--settings`, which agy never saves to the home):
`allowNonWorkspaceAccess: true` (files outside the workspace) and
`artifactReviewPolicy: "always-proceed"` (plans and artifacts do not wait for review). agy's
status bar shows only the mode and model (`auto · Gemini 4 Neon · xhigh`), not YOLO.

To ask before tools instead, set the definition's `skipPermissions: false`. Then, in `auto`
mode, the session's own MCP servers still run without asking (a launch-only rule
`mcp(<name>/*)`; `agy.allow_session_mcp_servers: false` turns that off), and every other menu
agy draws asks through Forge.

Whatever agy still asks reaches Forge the way a Claude Code or Codex prompt does, as a
permission card with agy's own rows (command and MCP approval menus, recorded from
1.5.16); the answer picks the matching row. Questions Gemini asks (`ask_question`) are
question cards. A numbered menu Skuld cannot read shows one line in the session with its
text, is logged at error level and counts on `unread_menus`; answer it in the terminal view.

## Human input only

Google's terms let a personal Google login be driven by a person, not by another agent or
an automated loop. With `agy.human_input_only: true` (the default) the broker refuses every
message whose sender is not the user or the owner on a voice call:

- messages from other sessions (Forge MCP `send_message`), automations, Ting and unknown
  senders get a `human_input_only` error and are not recorded;
- a bare `steer_active_turn` and room relays are refused, since they carry no sender;
- the chronicle's end-of-session summary prompt is not sent;
- a session whose first prompt came from anyone but the user does not start.

Forge's REST API still accepts `"sender": "user"` from any caller holding the owner's
credential, so a script using a personal access token can pass as the user. Do not point
such scripts at these sessions. Set `human_input_only: false` only for an API-key login.

## MCP servers

agy gets the session's MCP servers through one launcher script per server name in
`agy.home/.gemini/config/mcp_config.json`. The real description stays in the session's own
`agy` environment, so the shared file holds no URL, token or broker address.

- stdio servers (the Forge MCP among them) run as themselves.
- Streamable-HTTP servers (Linear, for example) run through the launcher's bridge, which
  reads the auth header from the session's credential file or variable on every request.
- A server that cannot run is left out, and the session still starts. The chat shows one
  line naming each such server and why, the broker logs it at error level, and it counts on
  `mcp_servers_left_out` and `mcp_left_out:<name>`. Reasons: a legacy SSE server; a
  credential file or variable that is not there (on a mini-mode Forge, integration tokens
  are not projected to `/run/secrets/mcp/...`, so an OAuth-connected Linear is left out
  there); two servers with the same name; or an entry with neither command nor URL.

## What runs where

- `agy` runs with `HOME=agy.home`, so commands the agent runs see that home too. Link the
  files the agent needs there (for example `~/.gitconfig`).
- Skuld writes one entry, `skuld-forge-bridge`, into `agy.home/.gemini/config/hooks.json`,
  and one per MCP server into `mcp_config.json`. Each runs a small script copied to
  `agy.home/.gemini/skuld/`, which reaches the session's own broker through variables
  that only that session's `agy` has. An `agy` you start by hand in the same home runs
  the hooks as no-ops and cannot start the Forge MCP server.

## Known limits

- Multi-select questions are answered in the terminal; Forge refuses to drive them.
- The HTTP bridge does not open the optional standalone GET stream, so a server message sent
  outside any request is not relayed. Replies and messages streamed on a request are.
- An interrupted turn reports no tokens: `agy` writes no transcript row for it.
- Subagents run in their own conversations; Skuld counts their hooks but does not show them.
- Live text streams to the terminal view only; the chat view gets each finished step.
