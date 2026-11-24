# Forge session icons

Epic LEX-2628, server ticket LEX-2629. A session can register one square icon for
itself, and every device reads it through the Forge it already talks to. The iPhone
shows pinned sessions as an icon grid (LEX-2630). Built like
[session descriptions](forge-session-descriptions.md).
Feature flag: `session_icons: true` on `GET /api/v1/forge/feature-flags`.

## Contract

| | |
|---|---|
| Input | PNG, JPEG or HEIC (decoded as HEIF), **square** (width = height), 64 to 2048 px on a side, at most 2 MiB (2,097,152 bytes) |
| Stored | One 512×512 PNG, plus 256 and 128 px renditions resampled from the upright source. Smaller inputs are scaled up, never stretched. EXIF orientation is applied, then every byte of metadata is dropped (no EXIF, text chunks or ICC profile). Transparency is kept. |
| ETag | The sha256 (hex) of the 512 px PNG. Encoding is deterministic, so setting the same image again changes nothing. |

The values live in `niuu.domain.session_icon`. They are the published contract the
phone and Mac code against: changing one is a contract change.

## API

All routes sit next to the presented-file routes, on Forge and on the Guild facade
(which forwards to the owning node and answers 501 when that node predates them).

| Call | Answer |
|---|---|
| `PUT /api/v1/forge/sessions/{id}/icon` (body: the image bytes, any content type) | `200 {"icon_etag": "<sha256>", "size": 512}`; `422` with what to fix ("icon must be 1:1 (square); this image is 200x100", wrong type, side out of range); `413` over 2 MiB |
| `DELETE /api/v1/forge/sessions/{id}/icon` | `204`; clearing a session with no icon is a no-op |
| `GET /api/v1/forge/sessions/{id}/icon?size=512\|256\|128` | the PNG (default 512) with `ETag: "<sha256>"`, `Cache-Control: private, max-age=86400`; `304` when `If-None-Match` names the current ETag; `404` when the session has no icon; `422` for another size |

Every session record (`GET /sessions`, `GET /sessions?fields=summary`,
`GET /sessions/{id}`) and every `session_updated` frame on the fleet SSE stream carries
`icon_etag` (`null` when none). Clients fetch the icon only when the etag changes and
cache by `(host, session, etag)`; there is no reason to poll the icon route.

A change of etag (set, replace, clear) bumps the session's `updated_at` and publishes one
`session_updated` frame. Writing the icon that is already stored moves nothing.

**Auth.** The same as other session routes: operators with access to the session, and
the session's own Forge session token (`forge:notify`, bound to its own session) for
`PUT`/`DELETE`. A session token may read any of its owner's icons
(`forge:session:read`), and is refused on another session's `PUT`/`DELETE` (403).

## Setting it from a session

1. **Forge MCP** tool `set_session_icon` on the `forge` server, offered to every session
   with no extra grant. Exactly one of:
   - `{"path": "<file in the workspace or home>"}`
   - `{"file_id": "pf_…"}` — an image already shown with `present-file`
   - `{"clear": true}`

   The session's own broker reads the file (the same roots as `present-file`) and sends
   the bytes to Forge, so Forge's checks decide and its message comes back as the tool
   error. The Forge-hosted HTTP MCP endpoint cannot see a session's files, so there
   `set_session_icon` only clears.

2. **Shell** `forge-icon <path>` / `forge-icon --clear`, a PATH shim installed beside
   `present-file` and `forge-describe` for every engine. It posts to the broker's
   loopback Forge MCP route (`FORGE_ICON_URL`, exported when the Forge MCP is enabled).
   Exit codes: 2 usage, 3 not available, 1 refused.

The tool text, the session capability text and the `forge-notify` skill all say: set it
once when the session starts, if it has a clear identity (a project, a person, a
product); never generate a new icon every turn.

## Storage

Migration `000098_session_icons` adds `sessions.icon_etag` and the `session_icons`
table (one row per size: `session_id`, `size`, `etag`, `png`, `updated_at`; primary key
`(session_id, size)`, `ON DELETE CASCADE`). `SessionRepository.set_icon` writes the
session row and the renditions in one transaction; the generic `update` never writes
`icon_etag` and returns the stored value, so a stale in-memory `Session` cannot revert
it. Icons survive restart and archive and go with the session's delete. Decoding uses
Pillow with `pillow-heif` for HEIC.
