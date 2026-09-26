"""Shared authoring guidance for session and feed notification tools and skills."""

COMPACT_NOTIFICATION_GUIDANCE = """\
Activities are compact event markers, not reports. Post one meaningful outcome, \
decision, request or failure per notification.
- Title: carry the key outcome or required action in one plain, specific line \
(aim for at most 72 characters). It must make sense without opening the body.
- Body: optional, one short sentence (aim for at most 160 characters). Add only \
new context or the next action; never repeat the title. Omit it when the title is enough.
- Keep explanations, test inventories, logs, checklists and technical identifiers \
in the conversation or a linked artifact, not in the activity. No multiline \
progress report or pasted final answer. Use `links` for supporting evidence.
- Forge supplies project/session, host, model and time from the session context; \
do not invent that metadata or repeat it in the message.
Examples: title "iOS 2289 is on TestFlight", \
body "Ready to test Activities and project navigation."; \
title "Microphone permission needed", body "Allow microphone access in Settings to continue." \
These are writing targets, not new API length limits; preserve existing records."""
