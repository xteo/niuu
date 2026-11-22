# Skuld support for Antigravity and Gemini 4 Neon

Status: phase 1 research, 22 Nov 2026. Branch `skuld-antigravity-gemini`, cut from `5ccf9ea3c`.

## Recommendation

Add a `skuldGemini` session that runs Google's `agy acp` over ACP, reusing the shape of the Grok
transport, and **authenticate it only with a Gemini API key or a Google Cloud project**.

**Do not connect Forge to a Google AI Pro or Ultra login.** Google's terms forbid it for this use,
Google says it enforces this with automated suspensions, and a suspension covers the account's
Antigravity, Gemini CLI and Code Assist access. So the subscription cannot offset this cost. Forge
usage will be billed per token.

One decision is needed before phase 2: which paid credential to use (see [Decision](#decision)).

## What was not verified

- `agy` is **not installed** on Thor and nothing was run against it. Google's terms treat
  downloading it as accepting them, so that step waits for the decision. All protocol statements
  below come from Google's documentation.
- Whether `agy acp` reports **token usage** is undocumented. Headless `stream-json` does report it.
  This is the first thing slice 0 must establish.
- Whether a `systemPrompt` passed in `session/new` is honoured is unknown.
- Neon's context through `agy` on an API key is unconfirmed: the API says 1M, the Antigravity model
  page says "up to 512K".
- No independent speed figure (tokens per second) was found. "Fast" is Google's description.
- Which Google plan Damien holds is not on record. It does not change the recommendation.

## 1. What Antigravity and Gemini 4 Neon are

**Gemini 4 Neon** is the correct name. Model id `gemini-4-neon`, generally available since
18 Nov 2026 ([announcement](https://blog.google/innovation-and-ai/models-and-research/gemini-models/introducing-gemini-4-neon/),
[what's new](https://aistudio.google.com/docs/whats-new-gemini-4-neon),
[model card](https://deepmind.google/models/model-cards/gemini-4-neon/)).

- Context: 1,048,576 tokens shared by input, thinking and output. 65,536 output tokens per
  synchronous request, more through long decode.
- Thinking levels: `low`, `medium`, `high`, `xhigh`. The API defaults to `high`, Antigravity to
  `medium`, so Skuld must always set it explicitly.
- Quality, by Google's account: #3 on the Artificial Analysis Intelligence Index (61, at `xhigh`),
  #2 on the Vals Index, #2 on steerability on the Agent Arena Index.
- No free API tier.

**Antigravity** is Google's agent product: app, IDE extensions, SDK and the Go CLI `agy`. It replaced
Gemini CLI for free, Pro and Ultra users on 18 June 2026
([transition notice](https://github.com/google-gemini/gemini-cli/discussions/27274)).

### Price

Neon is good value for its class, but it is **not the cheap Gemini option**
([pricing](https://ai.google.dev/gemini-api/docs/pricing)). USD per million tokens:

| Model | Input | Cached input | Output | Example session* |
|---|---:|---:|---:|---:|
| Gemini 4 Carbon, to 30 Nov | 2.00 | 0.10 | 10.00 | $3.43 |
| Gemini 4 Carbon, from 1 Dec | 4.00 | 0.20 | 20.00 | $6.85 |
| **Gemini 4 Neon** | **3.00** | **0.15** | **15.00** | **$5.14** |
| Gemini 4 Hydrogen | 1.50 | 0.075 | 7.50 | $2.57 |
| Gemini 3.8 Flash, to 31 Dec | 0.75 | 0.075 | 3.75 | $1.82 |

\* Illustrative, not measured: 100 model steps at 150K context, 95% cache hits, 500 output tokens per
step. Thinking is billed as output. Argon is priced the same as Carbon.

The current $2/day trial limit covers the proof below, not daily work on Neon.

## 2. Ways to drive it headless

| Path | Streaming | Tools | Permissions | Resume | Model / effort | Usage | Fit |
|---|---|---|---|---|---|---|---|
| **`agy acp`** | Yes | Yes | Relayed to client | Load, resume, list, close | Per session | Undocumented | **Best** |
| `agy --input-format stream-json` | Yes | Yes, visible | Policy only, denials are soft | New process with `--conversation` | At launch | **Yes, cumulative** | Fallback design if ACP lacks usage |
| Gemini CLI | Yes | Yes | Yes | Yes | Yes | Yes | Being superseded for most users |
| Direct Gemini API | Yes | We build them | We build them | We build it | Yes | Yes | Means writing an agent loop |
| OpenCode or Pi with a Google key | Yes | Yes | Varies | Yes | Yes | Yes | Neither is installed on Thor, and it would not be Google's agent |

`agy acp` also supports steering a running turn, cancel, images, MCP servers and elicitation forms
([ACP guide](https://antigravity.google/docs/ide/extensions/acp/),
[headless guide](https://antigravity.google/docs/cli/headless/)).

## 3. Auth and billing

### Subscription login is ruled out

- [Antigravity terms §6](https://antigravity.google/terms): "Using third party software, tools, or
  services to access the Service ... is a breach of this Agreement", with suspension or termination
  of "Antigravity and/or Gemini CLI accounts".
- [FAQ](https://antigravity.google/docs/faq/): wrapping `agy` inside third-party agents or
  applications "is not supported under consumer plans".
- [Google's answer on issue #1493](https://github.com/google-antigravity/antigravity-cli/issues/1493):
  a human-driven custom ACP client is allowed, but using `agy acp` as "a backend worker inside a
  third-party coding agent, autonomous agent harness, or background orchestrator" is not, and
  "there is no carve-out for single-account or personal use".

Forge sessions are started and messaged by coordinators, Ting and other agents, so Forge falls on the
wrong side of that line. A "human-only" Forge mode would be hard to guarantee and is not worth
risking the account. Appeals are [limited to one per account](https://discuss.ai.google.dev/t/appeal-for-antigravity-account-suspension-google-ai-pro-via-jio-plan-for-using-zed-editor-gemini-acp/198955/1).

Damien can still use his subscription directly in the Antigravity app, or in `agy` himself.

### Supported credentials

| | Gemini API key | Google Cloud project |
|---|---|---|
| ACP method | `gemini-api-key` | `agent-platform` (ADC) |
| Billing | AI Studio, prepaid from $5 | Cloud Billing, can use Cloud credits or commitments |
| Setup | A few minutes | Project, billing, API, IAM, `gcloud` (not on Thor) |
| Terms | Gemini API | Google Cloud |
| Spend control | Project cap (experimental, about 10 minutes of overrun) | Budgets and quotas |

Paid API traffic is not used to improve Google's products. New paid projects do **store requests by
default** since 26 Oct 2026, so logging should be turned off in AI Studio for code work
([changelog](https://ai.google.dev/gemini-api/docs/changelog),
[billing](https://ai.google.dev/gemini-api/docs/billing)).

## 4. How it fits Skuld

Grok is the closest precedent: `src/skuld/transports/grok.py` runs a persistent ACP agent and
normalizes events for the broker. A Gemini runtime touches:

- `src/skuld/transports/gemini.py`: the new ACP transport
- `src/niuu/config_models.py`: the `skuldGemini` definition
- `src/skuld/config.py`, `src/skuld/transports/__init__.py`, `src/skuld/forge_mcp/host.py`: registration
- `src/bifrost/config.py`, `src/niuu/domain/reasoning.py`: models and efforts
- `src/volundr/config.py`: a `google` API-key integration injecting `GEMINI_API_KEY`
- `charts/`: the session definition and the `agy` binary in the image

**Share the ACP framing, but do not copy these Grok behaviours**, which break the repo's no-fallback
and no-hidden-limit rules or do not apply:

- Best-effort auth preflight that logs and continues.
- Silent remapping of retired model ids.
- Token usage estimated from characters. Gemini usage must be real, or be shown as unavailable.
- `resumeHint`, which does not actually resume. `agy` has real `session/resume`.

Gemini-specific requirements:

1. **Never fall through to a personal login.** `agy` reuses cached credentials, so the transport runs
   it with an isolated home and config, explicitly selects the configured auth method, and fails the
   session if that is not `gemini-api-key` or `agent-platform`.
2. **Keep the key away from the agent where possible.** `session_process_env()` passes
   `GEMINI_API_KEY` through, so the model's shell tools can read it. Start with a dedicated, capped
   key. A later slice can route through a proxy using `GOOGLE_GEMINI_BASE_URL`.
3. Launch with `--disable-telemetry`, and declare no client `fs` capabilities (the cause of Grok's
   hung tools).

## 5. Slices

| # | Slice | Proof | API cost |
|---|---|---|---|
| 0 | Install `agy` in an isolated home, capture real ACP traffic | Recorded handshake, turn, tool, usage, resume | < $0.50 |
| 1 | `skuldGemini`: launch, stream, tool, usage | Unit tests on recorded frames, live turn on a candidate Forge | < $1 |
| 2 | Credential integration, personal-login guard | Tests that a cached login is refused | none |
| 3 | Model catalog and efforts | Catalog and validation tests | none |
| 4 | Resume, cancel, steering, permissions, MCP | Transport tests, live restart | < $1 |
| 5 | Chart and image support, operator doc | Chart tests | none |

Phase 2 of the brief is slices 0 and 1. Nothing touches the live Forge, and rollout needs Damien's go.

## 6. Risks

| Risk | Mitigation |
|---|---|
| Account suspension through a consumer login | API or Cloud auth only, enforced in the transport |
| Cost runs away in long sessions | Dedicated project, spend cap, default to Hydrogen for routine work |
| ACP has no usage data | Use `stream-json` for usage or report it as unavailable, never estimate |
| `agy` changes quickly (1.5.x, frequent releases) | Pin the version, keep recorded-frame tests |
| API key readable by the agent | Dedicated capped key, then the proxy slice |
| Code retained in AI Studio logs | Turn off logging for the project |

## Decision

**Which credential should Forge use for Gemini?**

- **A. New dedicated Gemini API key with a spend cap (recommended).** Fastest, with clear cost limits.
- **B. Google Cloud project.** Better if Damien has Cloud credits or wants Cloud terms. More setup.
- **C. Stop here.** Keep using the subscription only in Google's own apps.

Choosing A or B also approves installing `agy` on Thor under the matching terms.

For A, create a key in a new AI Studio project, set a low monthly cap, turn off logging, then store it
without echoing it:

```bash
mkdir -p ~/.config/niuu-skuld-gemini && chmod 700 ~/.config/niuu-skuld-gemini
( umask 077; read -rsp 'Gemini API key: ' k; printf '%s' "$k" > ~/.config/niuu-skuld-gemini/api-key; unset k; echo )
```
