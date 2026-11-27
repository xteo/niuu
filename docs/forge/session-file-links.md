# Opening a file a session named (LEX-2110)

A session's text names files the way a person would: `~/agent/health/note.md`,
`/home/thor/repo/x.py:12`, `` `src/app.py#L10-L20`. ``, `file:///Users/xteo/a%20b.md`.
The app sends the link as written; the session's broker decodes it **on the session's
own host** and answers with a record the presented-file route already serves.

## Routes

| Route | Where |
|---|---|
| `POST {broker}/api/files/resolve` | the broker itself; reached as `POST /s/{id}/api/files/resolve` through the session proxy |
| `POST /api/v1/forge/sessions/{id}/files/resolve` | Forge, and the Guild facade in front of it (forwarded to the session's node) |

Body: `{"path": "<as written>"}`. Owner only (the room-access table lists it for no lower
role), authenticated like the other session file routes.

## Decoding

1. Trim surrounding backticks, quotes, brackets and trailing sentence punctuation.
2. `file://` (or `file://localhost`) URLs: take the path and URL-decode it. A plain
   path with `%XX` escapes tries its literal spelling first, then the decoded one.
3. Strip a `:line`, `:line:col`, `#L12`, `#L12-L20` suffix.
4. `~`, `~/`, `$HOME`, `${HOME}` become the broker's `home_dir` (Thor `/home/thor`,
   mini-m6 `/Users/xteo`). `~user` is 422.
5. A relative path is joined to the session workspace; the result is realpath'd.

## The rule

- Only the present-file roots: the session workspace and the session home, each in its
  written and canonical spelling (a root may itself be a symlink). Anything else, a
  symlink that leaves them included, is **403**.
- Refused with **403** even inside the roots (`skuld.host_path_links`, constants on
  purpose). Names are compared after Unicode NFKC normalisation and casefolding, on the
  link, on its target and on the open file's kernel path. Only the components **below
  the matched root** are judged, so a worktree called `…-session-token-fix` or a home
  `/Users/secretary` is not itself refused.
  - **Home area:** every dot-entry directly under `~`, file or directory
    (`~/.github-pat`, `~/.codex-*/`, `~/.gstack/`, `~/.config/`, `~/.local/`), and
    `~/Library`. Open exceptions (`ALLOWED_HOME_DOT_ENTRIES`): `~/.lexi/flow/`,
    `~/.lexi/config/`, `~/.lexi/notes/`, `~/.claude/CLAUDE.md`, `~/.claude/skills/`,
    `~/.claude/projects/*/memory/`. Every rule below still applies inside them
    (`~/.lexi/flow/secrets/…` stays refused). Files in the workspace skip the home-area
    rule only when every spelling of the workspace sits strictly below the area it is in
    (`~/.niuu/workspaces/<id>`); a workspace that is `~/.config`, a symlink to it, home,
    or holds home, skips nothing.
  - **Substring:** a component containing `secret`, `token` or `credential`. Dot
    components always fall under it. A final name with a code extension (`.py .pyi .ts
    .tsx .js .jsx .mjs .swift .go .rs .java .kt .c .h .cpp .m .mm .rb .cs`) skips it,
    and inside the workspace so do the non-dot directories above it. `.md` and `.rst`
    skip it only inside the workspace. Data, config and extensionless names keep it.
  - **Directories** `.ssh`, `.gnupg`, `.aws`, `.azure`, `.kube`, `.docker`, `.gbrain`,
    `.password-store`, `.mozilla`, `Keychains`, `keyrings` at any depth.
  - **Names** (for every file, code included): starting `.env`, `id_rsa`, `id_dsa`,
    `id_ecdsa`, `id_ed25519`; `.netrc`, `.pgpass`, `.npmrc`, `.pypirc`, `.claude.json`;
    ending `.env`, `.pem`, `.key`, `.p8`, `.p12`, `.pfx`, `.keychain`, `.keychain-db`,
    `_history`; `.codex/auth.json`.
- A file with **more than one hard link: 403**, since it may be a second name for a
  refused file. Expect this for hard-linked trees, for example the files in a uv
  `.venv`'s `site-packages` (uv hard-links them from its cache).
- Directories, devices and FIFOs: **403**. Over `max_presented_file_bytes`: **413**.

## Answers

- `200 {"file_id", "name", "size", "mime", "path", "display_path", "host", "source": "link"}`.
  `path` is the absolute resolved path, `display_path` the same with `~` for home.
  Open it with `GET /sessions/{id}/files/presented/{file_id}`.
- `403 {"detail": "<path> is outside the session roots"}` (or names the refusal).
- `404 {"detail": "no such file on <host>: <resolved path>"}`.
- `413` over the size cap; `422` when the path can't be decoded.

`file_id` is an HMAC, under a random secret the broker draws at start, of the session,
the resolved path, its device, inode, mtime and size: the same file at the same version
gets the same id from the same broker, and an id can't be computed from a path. The file
is **served from its place**, not staged: no conversation turn, no durable upload, so a
link never appears in the session's documents, Show Documents or Unread.

Serving a link (`GET …/files/presented/{file_id}`):
- needs the **owner** room role: a viewer, who may open presented files, gets 403.
  Forge resolves the caller's room role (the session proxy's own resolution, 403 when
  there is none) and stamps it on its call to the broker, replacing anything the client
  sent; the Guild facade forwards to that node, which does the same;
- judges the stored path again literally (never decoded twice), opens it with
  `O_NOFOLLOW`, checks the descriptor is the same inode at the same size and mtime with
  one link, judges the descriptor's kernel path (`/proc/self/fd` on Linux, `F_GETPATH`
  on macOS) against the same rules, and streams **from that descriptor**, at most the
  recorded size (a file that shrinks mid-send is an error, never a short answer). The
  descriptor is closed when the response ends, however it ends, including a client
  that leaves before the first byte;
- answers **410** when the file changed since it was resolved (the app resolves the
  link again), and 403/413 when it no longer passes. Forge and the facade pass these on.

The id map holds one id per resolved path (a newer version replaces the older id). It is
in memory, so after a broker restart an old id is 404 until the link is resolved again.

## Capability

The session's own broker is the authority: `resolve_path: true` (alias
`present_by_path`) on its `GET /api/capabilities` and WebSocket `capabilities` frame.
Forge's `GET /feature-flags` (and the facade, mirrored from the node) says the Forge
route exists. A session whose broker predates the route answers **501** with
`x-forge-error-code: resolve_path_unsupported` (the facade passes the header on), never
a 404 that reads as a missing file.

Forge checks the caller may update the session before it forwards the request.
