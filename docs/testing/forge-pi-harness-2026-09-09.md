# PI harness integration: initial evidence and acceptance plan

Status: native protocol investigation started; PI is not yet a Forge session engine.
The existing Claude, Codex, Grok and Muse adapters are unchanged. Lexi must not offer
PI as ready until a real adapter and host capability discovery pass the gates below.

## Evidence collected

- Installed the official `@earendil-works/pi-coding-agent` package, version `0.85.1`,
  in an isolated validation directory. The binary requires Node 22.19 or later.
- Its RPC process accepted commands, executed a shell command, wrote `answer.txt`,
  returned `PI_FORGE_PROBE=42`, and exposed the bash message through `get_messages`.
- A restart probe uncovered an important boundary: PI defers writing its session
  file until an assistant message exists. A returned `sessionFile` path alone is
  not proof that a session is recoverable. Forge must persist accepted user input
  independently and represent this early state accurately.
- Thor's configured PI model list was empty and its auth file contained no provider
  entries. Authenticated model execution and native restart acceptance are pending.
- Seventeen harness tests (96% probe coverage) cover response correlation, Unicode framing, rejected commands,
  missing tools/streaming, missing completion, and failed or aborted agent turns.

The protocol is documented in the upstream [PI RPC specification](https://github.com/badlogic/pi-mono/blob/main/packages/coding-agent/docs/rpc.md).
The investigation uses LF-delimited records with request IDs, distinct command
acceptance and agent completion, native steering/abort controls, and session files.

## Reproduce the native probe

Install the pinned runtime using the operator's normal package installation process,
then run:

```bash
python scripts/forge_pi_probe.py --output-dir /tmp/forge-pi-rpc --isolated
```

This is explicitly a native RPC smoke check. The JSON report keeps agent execution,
restart, Forge database replay, and iOS replay false. It never labels a shell-only
probe as a successful model session.

After authenticating a provider through PI's own login flow, select an actual model
from PI's catalog and pass its `provider/model` identifier with `--model`. The probe
then requires streamed model output, a real tool execution, an assistant completion,
an actual session file, and equal messages after restarting the native process.
`frames.json`, `result.json`, and `stderr.log` retain the evidence. Do not commit
credentials or private user conversations.

## Implementation and acceptance gates

1. **Runtime adapter.** Add a dynamically configured PI transport implementing the
   shared CLI port. It must own process startup/shutdown, command timeouts, bounded
   frame reads, stderr draining, and pending-request failure on EOF. Do not report a
   prompt acknowledgment as an executed user message or a completed turn.
2. **Stable transcript identity.** Map assistant message/content indices, tool-call
   IDs, tool results, and timing into the common event schema. Preserve text/tool/text
   order and apply final authoritative messages without duplicating streamed text.
   Store raw runtime evidence alongside normalized events for incident review.
3. **Interaction.** Verify steering during a long tool, queued follow-up, abort with
   queued input, model changes, and extension questions. Correlate each answer to the
   original request. Advertise only controls that are implemented and verified.
4. **Recovery.** Persist Forge session identity and the native session file separately.
   Exercise crashes before the first assistant message, during streaming, and after
   tool completion. Reconnect twice and assert no lost or duplicated user messages,
   tool calls, results, or final answers. Import pre-existing native history through
   the normal external-session recovery service, not an iOS-only cache.
5. **Host discovery.** Register PI and its real available models per host. Missing
   executable, missing credentials, and rejected models must be explicit errors.
   Start on one authenticated host, then repeat on Spark; do not assume shared auth.
6. **Live corpus.** Run the existing workspace, intentional tool failure/recovery,
   search, long-output, follow-up, and restart scenarios with unique markers. Add a
   Unicode separator case, repeated identical user text, compaction, and a native
   branch/fork. Subagents and web search require real configured PI capabilities;
   an unavailable extension is recorded as unsupported, not passed.
7. **Database and iOS.** Compare the native trace, Forge event log, persisted replay,
   WebSocket replay, and simulator transcript. Test an empty cache, offline reopen,
   reconnect mid-stream, and two hosts with colliding native IDs. Run simulator-only
   acceptance before enabling the PI creation option in Lexi.

Authenticated native execution is the next gate. The native probe is a starting
point for that work; it does not implement or certify the Forge adapter.
