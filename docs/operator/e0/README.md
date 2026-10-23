# E0 artifact replay

`replay.py` reuses `tests.support.artifacts.manifest_log_rows` to append the captured
publication identities and timestamps to one exp exercise session. This is synthetic
exercise evidence, not a fresh provider publish. The manifest records no title for most
publications. The builder now omits those titles; it copies an explicitly captured,
nonblank title in full, including a legitimate one-character title.

The original E0 run recorded in `e0-evidence.json` appended 122 publications for 12
provider keys to session `da8a686a-8866-4224-82e7-6fe5c03174bf`. At that time the
builder invented the title `T` for every publication. Those rows can override real
artifact titles when a consumer combines publications by provider key. This is a
replay-data defect; the Forge extractor does not truncate titles to one character.

The corrected builder prevents that defect in future exercises. Repeating the same
log append cannot repair existing durable rows: their session sequence is already
recorded. Preserve the original evidence and source rows. The companion CP repair
recognizes only this historical E0 session's exact manifest occurrences and treats
their synthetic title as unknown in its derived artifact view; other titles remain
unchanged. Do not run a broad replacement for the string `T`.

The existing replay command is a write to the selected exp exercise session. It is
not a repair command and should not be rerun to validate the title correction. Use
the offline tests in `tests/test_domain/test_artifact_replay_titles.py`.
