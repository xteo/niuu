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
   | `agy.home` | The `HOME` above. Required. |
   | `agy.expected_version` | The `agy --version` Skuld was tested with. Required: `agy` updates itself, and a new build can redraw the menus Skuld reads, so any other version refuses to start. |
   | `agy.mode` | `auto`, `accept-edits` or `plan`. |
   | `agy.human_input_only` | Default `true`. See below. |
   | `agy.turn_max_seconds` | Longest turn without a Stop hook; reaching it ends the turn with an error. |
   | Others | Polling, timeouts and pane size; see `AgyRuntimeConfig` in `src/skuld/config.py`. |

4. In Forge, connect **Google Antigravity (host sign-in)** under AI providers and give the
   `agy` home from step 1. It stores only that path (no token) and hands it to the
   person's sessions as `SKULD__AGY__HOME`; the launcher offers Gemini once it is connected.

   Skuld launches `agy` with `AGY_CLI_DISABLE_AUTO_UPDATE=true` (agy ignores `1`). Updates
   started by hand still happen; after one, re-test and raise `agy.expected_version`.

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

## What runs where

- `agy` runs with `HOME=agy.home`, so commands the agent runs see that home too. Link the
  files the agent needs there (for example `~/.gitconfig`).
- Skuld writes one entry, `skuld-forge-bridge`, into `agy.home/.gemini/config/hooks.json`,
  and one per stdio MCP server into `mcp_config.json`. Each runs a small script copied to
  `agy.home/.gemini/skuld/`, which reaches the session's own broker through variables
  that only that session's `agy` has. An `agy` you start by hand in the same home runs
  the hooks as no-ops and cannot start the Forge MCP server.

## Known limits

- Multi-select questions are answered in the terminal; Forge refuses to drive them.
- Only stdio MCP servers are offered to `agy`.
- An interrupted turn reports no tokens: `agy` writes no transcript row for it.
- Subagents run in their own conversations; Skuld counts their hooks but does not show them.
- Live text streams to the terminal view only; the chat view gets each finished step.
