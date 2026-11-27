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

- Only the present-file roots: the session workspace and the session home.
  Anything else, a symlink that leaves them included, is **403**.
- Refused with **403** even inside the roots (`skuld.host_path_links`, a constant on
  purpose): directories `.ssh`, `.gnupg`, `.aws`, `.kube`, `.password-store`,
  `Keychains` at any depth; names starting `.env`, `credentials`, `.credentials`,
  `id_rsa`, `id_dsa`, `id_ecdsa`, `id_ed25519`; `.netrc`, `.pgpass`, `.git-credentials`,
  `.npmrc`, `.pypirc`; suffixes `.pem`, `.key`, `.p12`, `.pfx`, `.keychain`,
  `.keychain-db`; `.codex/auth.json`. Checked on the link and on its target,
  case-insensitively.
- Directories, devices and FIFOs: **403**. Over `max_presented_file_bytes`: **413**.

## Answers

- `200 {"file_id", "name", "size", "mime", "path", "display_path", "host", "source": "link"}`.
  `path` is the absolute resolved path, `display_path` the same with `~` for home.
  Open it with `GET /sessions/{id}/files/presented/{file_id}`.
- `403 {"detail": "<path> is outside the session roots"}` (or names the refusal).
- `404 {"detail": "no such file on <host>: <resolved path>"}`.
- `413` over the size cap; `422` when the path can't be decoded.

`file_id` is derived from the session, the resolved path, its mtime and size: the same
file at the same version always gets the same id. The file is **served from its place**,
not staged: no conversation turn, no durable upload, so a link never appears in the
session's documents, Show Documents or Unread. Serving checks the file again; if it
changed since it was resolved the answer is **410** and the app resolves the link again.
The id map is in memory, so after a broker restart an old id is 404 until resolved again.

## Capability

`resolve_path: true` (alias `present_by_path`) on the broker's `GET /api/capabilities`,
its WebSocket `capabilities` frame, Forge's `GET /feature-flags` and, mirrored from the
node, the Guild facade's.
