# User preference and delivery checkpoint

14 September 2026, after the initial review.

The user requested the Markdown directly in this session as a remotely openable
document using normal local review, rather than a GitHub link. **Prefer in-session
documents for future deliverables.** Git remains durable engineering evidence,
not the default human document-opening experience. This preference is being handed
to the project coordinator; it is not a product implementation acceptance.

## Observed delivery paths

- Installed `present-file` exists in this worktree's `.skuld-tools/bin`, but reports
  `not available in this session` because its injected URL is absent.
- The existing own-session `POST /api/present-file` through the Thor session proxy
  returned HTTP 500. Own broker logs report `present-file: staging copy failed:
  FileNotFoundError(2, 'No such file or directory')`.
- Own `GET /api/conversation/history` showed no emitted `present_file` turn after
  that failure. No blind retry, synthetic attachment/tool event, config repair,
  directory repair or service restart was performed.
- Normal own-session `GET /api/files/download`, workspace root and report path,
  returned HTTP 200. Downloaded bytes exactly match the committed Markdown. This
  provides a remotely openable/downloadable session file without GitHub, but does
  **not** prove the native attachment card/local viewer works on the user's device.

Source contracts: `src/skuld/transports/tool_shims.py:147–197`;
`src/skuld/broker_api.py:678–807`; `src/skuld/file_routes.py:151–173`.
These explain the paths; the failed live gateway is not assumed identical to this
reviewed source. The precise missing staging component was not repaired/inferred
from the abbreviated error alone.

Private request/response, own log/filter and byte-verification evidence are under
the runner's `.local/audit/report-*-file-*.json`,
`presentation-error-logs.json`, `presentation-conversation-check.json`, and
`report-session-download-proof.json`.

Session file: workspace `docs/forge/codex-subagents-workflow-review-20260914.md`.
The user can access the same file from the session Files surface. No production
code was changed. Runtime owner may triage the broken attachment staging later
under its existing safety constraints; this checkpoint authorizes no deployment.
