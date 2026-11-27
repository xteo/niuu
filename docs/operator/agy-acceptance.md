# Gemini (agy) session acceptance

A person runs this on a live Forge: Gemini sessions on a personal Google login take messages
typed by a person only (`agy.human_input_only`), so no agent or script may send it.

## Before you start

1. Launch a new Gemini session (web Advanced launch, engine **Google Antigravity (Gemini)**,
   model Gemini 4 Neon) on a scratch folder you do not mind it writing to. Give it no first
   prompt.
2. Have the session open in the Forge web app or the Lexi app, with the terminal view at hand.

## The message to paste

Paste this as the first message, in one piece:

```text
Acceptance run for this Forge session. Do the steps in order, and start each answer with its step number. Do not skip a step: if one fails, say what failed and go on.

1. Shell: run `uname -a` and `pwd`, and give me both outputs.
2. Files: create notes/acceptance.md containing the line "first line", then read it back, then edit it so the line reads "edited line", and show me the final file.
3. Web search: search the web for the latest Antigravity CLI release notes and give me the version and one change, with the link.
4. Forge MCP: call the forge MCP server's environment tool and tell me which host this session runs on. Then call its get_session tool for this session and tell me the session's name.
5. Other MCP servers: list every MCP server you have. If one I connected is missing, say so.
6. A question for me: ask me, as a multiple-choice question with the options "Tea" and "Coffee", which I prefer, and wait for my answer before going on.
7. File for me: write a short report of steps 1 to 6 to notes/acceptance-report.md and present it to me with present-file.
8. Long step: run `for i in $(seq 1 120); do echo tick $i; sleep 1; done` and tell me when it finishes.
```

## What the person does and checks

| Step | You do | Expected in the app |
|---|---|---|
| 1 Shell | nothing | The command shows as a tool call that turns done with its output; no approval card (YOLO). |
| 2 Files | nothing | Tool calls for the write, read and edit; the final file shows "edited line". No card. |
| 3 Web search | nothing | A search tool call with results; the reply names a version and a link. |
| 4 Forge MCP | nothing | `call_mcp_tool` calls for forge `environment` and `get_session`, each with its result (not "No output captured"); host Thor (or the host you used) and this session's name. No card (YOLO). |
| 5 MCP list | read the chat | forge is listed. On a mini-mode Forge (Thor, mini-m6) the chat also shows one line saying Gemini started without the linear MCP server because its access token is not on this host. |
| 6 Question | answer the card | A question card with Tea and Coffee appears (Forge web and Lexi app); your answer reaches Gemini and it says which you chose. |
| 7 File | open the card | A file card for acceptance-report.md that opens in the preview. |
| 8 Long step | after about 10 s of ticks, press **Stop** (interrupt) | The turn ends as interrupted; the session is idle and accepts a new message. |

Then, one at a time:

| Check | You do | Expected |
|---|---|---|
| Effort | change the effort (for example xhigh to medium) | The terminal's status line shows the new effort; send "What did step 6 ask?" and the reply still remembers the conversation. |
| Resume after a broker restart | restart the session from Forge, then send "What was step 2's final line?" | The session comes back with its history, and the reply says "edited line". |
| Attachment | attach an image or text file to a message and ask "What is in the attachment?" | The reply describes the attachment. |
| Not a person | from another session, send this one a message (Forge MCP `send_message`) | Refused with `human_input_only`; nothing appears in this session. |
| Usage | open the session details | Token counts for the turns, from agy. |

## If a step fails

- A card that never appears, or a tool stuck as running: open the terminal view. If agy shows a
  menu, the session should also show a line "agy is waiting on a menu Forge cannot answer" with
  the menu's text; answer it in the terminal and report the text.
- Note the session id and the step, and pass them on with the time.
