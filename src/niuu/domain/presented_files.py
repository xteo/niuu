"""Wire names shared by the broker, the facade and Forge for durable presented files (F2)."""

#: A present-file id: ``pf_`` and 32 hex digits (the broker mints it).
PRESENTED_FILE_ID_PATTERN = r"^pf_[0-9a-f]{32}$"

#: Forge's default cap on one durable presented file (``presented_files.max_bytes``).
PRESENTED_FILE_DEFAULT_MAX_BYTES = 50 * 1024 * 1024

#: The header carrying a presented file's display name on the durable upload (URL-quoted).
PRESENTED_FILE_NAME_HEADER = "x-presented-file-name"

#: A presented file is agent-supplied: browsers render it and never run it on Forge's origin
#: (HTML or SVG script, sniffed types). Every route that serves one sends these, the public
#: facade included (judge R16-1).
PRESENTED_FILE_SAFETY_HEADERS = {
    "x-content-type-options": "nosniff",
    "content-security-policy": "sandbox",
}

#: The durable copy never changes under its id; the live broker's copy is served only while
#: the session runs, so it is never cached.
PRESENTED_FILE_DURABLE_CACHE = "private, max-age=86400, immutable"
PRESENTED_FILE_LIVE_CACHE = "private, no-store"
