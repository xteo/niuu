# Repository-published project documents

Isolated source increment over fixed11e9e0c. Not deployed, not yet consumed by the native Project UI, and not a replacement for authenticated message provenance. The earlier steering/context integration pin is unchanged in its own worktree.

## Intent

A project needs readable progress and durable knowledge without filling its conversation or launch prompt with engineering logs. The project owns the records and instructions for maintaining them. Forge only provides a generic, bounded, versioned text-document read surface. It does not interpret objectives, review claims, assign work states, calculate completion, choose a coordinator, or invent a workflow engine.

## Publication and reads

The **committed** `project.json` may include an optional `documents` array. Each descriptor has a unique stable `id`, readable `title` and checkout-relative `.md` or `.txt` `path`. A project may publish its existing `context/WORK.md`, decision index or knowledge index; names and document contents are project policy, not Forge defaults. No manifest/project record is changed by this implementation.

- `GET /projects/{project_id}/documents` returns `project_id`, Git `revision`, and the ordered descriptors from that commit. It lists the declared publications; it does not claim every body has already been successfully read.
- `GET /projects/{project_id}/documents/{document_id}` returns the descriptor, exact UTF-8 `content`, `byte_size`, SHA256 and Git revision.
- Optional `revision` on the second read requires the same HEAD as the list. A changed head returns409 with an explicit refresh remedy, never silently combines a stale descriptor with new content.
- Only a published ID can be requested. The API does not accept an arbitrary workspace path or Git revision expression. Unpublished IDs are404; invalid configuration/body is422; inaccessible checkout is503 through the existing project error contract.
- The shared mesh facade forwards both GET forms to the selected instance and preserves revision/error semantics. It does not switch hosts after a failure.
- Missing committed manifest or absent `documents` means no publications. An explicitly configured malformed descriptor, wrong project ID, duplicate ID/path or unreadable body is an error, not an empty successful read.

## Safety and data fidelity

The existing project owner/tenant gate runs before filesystem access, and configured checkout roots still apply. This does not strengthen the identity infrastructure or make caller/account claims authenticated authorship.

Read the manifest and body from one immutable Git commit, not from working-tree files. `git ls-tree` checks regular blob mode and exact literal path; symlinks, submodules, directories and missing bodies are rejected. The object length is checked before reading. `cat-file blob` reads exact object bytes without checkout/smudge filters; replacement objects are disabled. No shell, checkout, commit, repository mutation, network Git request or provider call is involved. Dirty/untracked files are not published or staged. Text preserves CRLF and Unicode bytes; invalid UTF-8/NUL or oversize data fails rather than truncating.

Git subprocess stdout and runtime are bounded, including metadata. Timeout/cancellation/output overflow terminates and reaps only that owned subprocess. Git stderr is not returned as API text. Descriptor validation errors do not echo arbitrary invalid manifest values.

The existing8KiB context budget remains unchanged. Independent `ProjectsConfig.document_bytes` (default65536) and `document_count` (default32) bound on-demand publication. These flow through package configuration and dynamic workspace composition; Helm renders the same snake-case settings. There are no bare environment reads or new service credentials.

## Implementation / overlap

- Domain publication models and ProjectWorkspace port; real Git workspace implementation using `adapters/outbound/project_documents.py`.
- Existing project REST router and owner/tenant lookup; additive mesh operation allowlist.
- Package config/composition and Helm config values.
- No changes to `skuld/broker.py`, `transports/codex_ws.py`, `transports/tmux_interactive.py`, message delivery, project prompt snapshot semantics or active runtime worktrees.
- Configuration/composition and shared facade are integration surfaces: reconcile the exact eventual committed diff with the runtime runner via the coordinator. This source increment does not move the fixed11e9e0c rollout input or authorize deployment.

## Evidence and limits

Real temporary Git checkouts exercise exact committed bytes despite dirty manifest/body edits, revision conflicts, safe paths, symlink/missing/invalid/oversize content, duplicates, optional absence, root boundaries and cancellation/timeout cleanup. API tests cover project ownership and errors; mesh tests cover selected host/revision and no fallback. No real PostgreSQL/provider/live-host tests are claimed.

Initial reader/domain run:73passed. Expanded project suite first run:95passed/1failed due a test incorrectly assuming the existing mesh strips `instance_id`. Inspection confirmed that selector is preserved; the test now asserts existing behavior rather than changing global routing to fit the assertion. Final expanded project suite:101passed, no warnings reported; new reader coverage93% (statement+branch combined for that module only). Ruff passes on all owned files. Helm rendered non-default document limits32768/7; the resulting config was parsed and validated against ProjectsConfig. Whole-repository coverage/acceptance is not claimed.

Private evidence: `/home/thor/projects/lexi/.local/coordination/project-coordination-ux-20260913/documents/`. Native consumption, actual rendered project records, operator integration, and remote-author authentication remain subsequent work. A saved Markdown claim is still a project record, not server-verified work completion.
