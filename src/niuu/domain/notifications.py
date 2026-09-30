"""Shared contract for Forge session notifications.

A notification starts life as a durable ``conversation.turn`` in a session's event
log (written by the Skuld broker), or as a system event raised by Forge itself.
Forge projects both into its cursor-ordered notification feed. Skuld builds turns
with :func:`build_notification_turn`; Forge reads them back with
:func:`draft_from_log_payload`. Keeping both sides here means the turn shape, the
limits and the deterministic ids cannot drift between producer and projector.

A draft may carry one image reference (:class:`NotificationImageRef`). Forge copies
the referenced bytes once when it records the notification and serves its own
durable copy at :data:`NOTIFICATION_IMAGE_PATH`.
"""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from enum import StrEnum
from typing import Any
from urllib.parse import urlencode, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# Protocol limits. They bound what a model may persist per notification; they are
# part of the wire contract shared by Skuld, Forge and clients, not tunables.
MAX_TITLE_CHARS = 200
MAX_BODY_CHARS = 4000
MAX_LINKS = 10
MAX_LINK_LABEL_CHARS = 200
MAX_LINK_URL_CHARS = 2048
MAX_CORRELATION_ID_CHARS = 200

# Image protocol limits: what a producer may attach and what Forge will copy. Like
# the text limits above they are wire contract, not tunables.
MAX_IMAGE_ALT_CHARS = 300
MAX_IMAGE_URL_CHARS = MAX_LINK_URL_CHARS
#: A present-file id exactly as the Skuld broker issues it (``pf_`` + 32 hex digits).
PRESENTED_FILE_ID_PATTERN = r"^pf_[0-9a-f]{32}$"
#: The largest image Forge copies for one notification.
MAX_IMAGE_BYTES = 10 * 1024 * 1024
#: The most pixels Forge decodes for one image (a decompression-bomb bound).
MAX_IMAGE_PIXELS = 40_000_000
#: Image types a notification image may have. The fetched bytes must be declared as
#: one of them AND decode as one of them; Forge stores the decoded type.
NOTIFICATION_IMAGE_MIME_TYPES: frozenset[str] = frozenset(
    {"image/png", "image/jpeg", "image/gif", "image/webp"}
)
#: Where Forge serves a notification's stored image; ``?variant=thumb`` is the thumbnail.
NOTIFICATION_IMAGE_PATH = "/api/v1/forge/notifications/{notification_id}/image"
NOTIFICATION_IMAGE_THUMB_VARIANT = "thumb"

NOTIFICATION_TOOL_NAME = "forge_notification"
NOTIFICATION_TURN_KIND = "notification"

# Fixed namespace: ids derived from a dedupe key are stable across retries, hosts
# and releases, so a replayed producer frame always maps to the same notification.
NOTIFICATION_NAMESPACE = uuid.UUID("5f0c8a4e-4a55-4f4e-9d1b-6f7267652d6e")


class NotificationKind(StrEnum):
    MILESTONE = "milestone"
    DECISION = "decision"
    ATTENTION = "attention"
    REPLY_READY = "reply_ready"
    ERROR = "error"
    INFO = "info"


# Kinds a model may raise through the Forge MCP. reply_ready is derived by Forge
# from the durable final output and can never be claimed by a model.
AGENT_KINDS: frozenset[NotificationKind] = frozenset(
    {
        NotificationKind.MILESTONE,
        NotificationKind.DECISION,
        NotificationKind.ATTENTION,
        NotificationKind.ERROR,
        NotificationKind.INFO,
    }
)


class NotificationSeverity(StrEnum):
    INFO = "info"
    SUCCESS = "success"
    WARNING = "warning"
    CRITICAL = "critical"


_SEVERITY_ORDER = (
    NotificationSeverity.INFO,
    NotificationSeverity.SUCCESS,
    NotificationSeverity.WARNING,
    NotificationSeverity.CRITICAL,
)


def severity_rank(severity: NotificationSeverity | str) -> int:
    """Return a comparable rank; unknown values rank lowest."""
    try:
        return _SEVERITY_ORDER.index(NotificationSeverity(severity))
    except ValueError:
        return 0


class NotificationSource(StrEnum):
    AGENT = "agent"
    SYSTEM = "system"
    OPERATOR = "operator"


class NotificationLinkKind(StrEnum):
    URL = "url"
    FILE = "file"
    PR = "pr"
    SESSION = "session"
    ARTIFACT = "artifact"


class NotificationLink(BaseModel):
    label: str = Field(min_length=1, max_length=MAX_LINK_LABEL_CHARS)
    url: str | None = Field(default=None, max_length=MAX_LINK_URL_CHARS)
    kind: NotificationLinkKind = NotificationLinkKind.URL
    file_id: str | None = Field(default=None, max_length=MAX_LINK_LABEL_CHARS)


class NotificationImageSourceKind(StrEnum):
    FILE_ID = "file_id"
    URL = "url"


class NotificationImageRef(BaseModel):
    """An image a producer attaches: a present-file id OR an https URL, plus alt text.

    Forge copies the bytes once when it records the notification (through the
    session's broker for ``file_id``, by HTTP GET for ``url``) and serves its own
    durable copy; the reference itself is never handed to readers as the image.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    file_id: str | None = Field(default=None, pattern=PRESENTED_FILE_ID_PATTERN)
    url: str | None = Field(default=None, min_length=1, max_length=MAX_IMAGE_URL_CHARS)
    alt: str | None = Field(default=None, max_length=MAX_IMAGE_ALT_CHARS)

    @field_validator("url")
    @classmethod
    def _https_url(cls, value: str | None) -> str | None:
        if value is None:
            return None
        parsed = urlsplit(value)
        if parsed.scheme != "https" or not parsed.hostname:
            raise ValueError("image url must be an absolute https:// URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("image url must not carry credentials")
        return value

    @field_validator("alt")
    @classmethod
    def _normalize_alt(cls, value: str | None) -> str | None:
        if value is None:
            return None
        return " ".join(value.split()) or None

    @model_validator(mode="after")
    def _exactly_one_source(self) -> NotificationImageRef:
        if (self.file_id is None) == (self.url is None):
            raise ValueError("image needs exactly one of file_id (from present-file) or url")
        return self

    @property
    def source_kind(self) -> NotificationImageSourceKind:
        if self.file_id is not None:
            return NotificationImageSourceKind.FILE_ID
        return NotificationImageSourceKind.URL

    @property
    def source_ref(self) -> str:
        """The file id or URL, whichever this reference carries."""
        return self.file_id if self.file_id is not None else str(self.url)

    def wire(self) -> dict[str, str]:
        """The JSON form without absent fields (``{"file_id"}`` or ``{"url"}``, ``alt``)."""
        return self.model_dump(mode="json", exclude_none=True)


def notification_image_url(
    notification_id: uuid.UUID | str,
    *,
    thumbnail: bool = False,
    instance_id: str | None = None,
) -> str:
    """The Forge-relative URL of a notification's stored image (or its thumbnail).

    ``instance_id`` is added only by a Guild facade, so a fleet-wide client reaches
    the node that holds the image; a single Forge returns the plain URL.
    """
    path = NOTIFICATION_IMAGE_PATH.format(notification_id=notification_id)
    params: list[tuple[str, str]] = []
    if thumbnail:
        params.append(("variant", NOTIFICATION_IMAGE_THUMB_VARIANT))
    if instance_id:
        params.append(("instance_id", instance_id))
    if not params:
        return path
    return f"{path}?{urlencode(params)}"


class NotificationDraft(BaseModel):
    """What a producer asks to publish; Forge adds identity, ownership and order."""

    kind: NotificationKind
    severity: NotificationSeverity = NotificationSeverity.INFO
    title: str = Field(min_length=1, max_length=MAX_TITLE_CHARS)
    body: str = Field(default="", max_length=MAX_BODY_CHARS)
    links: list[NotificationLink] = Field(default_factory=list, max_length=MAX_LINKS)
    correlation_id: str | None = Field(default=None, max_length=MAX_CORRELATION_ID_CHARS)
    image: NotificationImageRef | None = None

    @field_validator("title")
    @classmethod
    def _strip_title(cls, value: str) -> str:
        stripped = " ".join(value.split())
        if not stripped:
            raise ValueError("title must not be blank")
        return stripped

    @field_validator("body")
    @classmethod
    def _strip_body(cls, value: str) -> str:
        return value.strip()


def draft_payload(draft: NotificationDraft) -> dict[str, Any]:
    """The JSON a producer sends for a draft (a turn's notification, a submit body).

    ``image`` appears only when one is attached, so drafts without an image keep the
    exact shape older producers wrote and older Forge releases read.
    """
    payload = draft.model_dump(mode="json", exclude={"image"})
    if draft.image is not None:
        payload["image"] = draft.image.wire()
    return payload


#: The role that may read every notification in its own tenant (Forge's admin role).
NOTIFICATION_ADMIN_ROLE = "volundr:admin"


def notification_visible_to(
    *,
    user_id: str,
    roles: Iterable[str],
    tenant_id: str | None,
    owner_id: str | None,
    notification_tenant_id: str | None,
) -> bool:
    """Whether a reader may see a notification: its owner, or an admin of its tenant.

    Untenanted notifications are visible to every admin, matching how Forge scopes
    untenanted sessions. The feed query, the SSE filter and the Guild facade all
    apply this one rule so a reader never sees a different set live and on reload.
    """
    if owner_id and owner_id == user_id:
        return True
    if NOTIFICATION_ADMIN_ROLE not in roles:
        return False
    return not notification_tenant_id or notification_tenant_id == tenant_id


def notification_id(dedupe_key: str) -> uuid.UUID:
    """Deterministic notification id for a dedupe key."""
    return uuid.uuid5(NOTIFICATION_NAMESPACE, dedupe_key)


def turn_dedupe_key(session_id: uuid.UUID | str, turn_id: str, kind: NotificationKind) -> str:
    """Dedupe key for a notification anchored to one transcript turn."""
    return f"turn:{session_id}:{turn_id}:{NotificationKind(kind).value}"


def build_notification_turn(
    draft: NotificationDraft,
    *,
    turn_id: str,
    session_id: uuid.UUID | str,
    created_at: str,
) -> dict:
    """Build the ConversationTurn-shaped dict a broker appends for an agent notification.

    Older clients that do not know the card render the ``content`` text and a
    generic tool card; the turn never carries ``final_output`` so it does not
    change the session's unread reply state.
    """
    nid = notification_id(turn_dedupe_key(session_id, turn_id, draft.kind))
    payload = draft_payload(draft)
    payload["notification_id"] = str(nid)
    return {
        "id": turn_id,
        "role": "assistant",
        "content": draft.title,
        "parts": [
            {
                "type": "tool_use",
                "id": turn_id,
                "name": NOTIFICATION_TOOL_NAME,
                "input": payload,
            }
        ],
        "created_at": created_at,
        "metadata": {"kind": NOTIFICATION_TURN_KIND, "notification": payload},
        "participant_id": None,
        "participant_meta": None,
        "thread_id": None,
        "visibility": "public",
    }


def draft_from_log_payload(payload: dict) -> tuple[str, NotificationDraft] | None:
    """Return ``(turn_id, draft)`` for a notification turn frame, else ``None``.

    Malformed notification turns are ignored rather than raised: the log is the
    source of truth and one bad frame must never block projecting the batch.
    """
    if not isinstance(payload, dict) or payload.get("type") != "conversation.turn":
        return None
    turn = payload.get("turn")
    if not isinstance(turn, dict):
        return None
    metadata = turn.get("metadata")
    if not isinstance(metadata, dict) or metadata.get("kind") != NOTIFICATION_TURN_KIND:
        return None
    turn_id = turn.get("id")
    raw = metadata.get("notification")
    if not isinstance(turn_id, str) or not turn_id or not isinstance(raw, dict):
        return None
    try:
        draft = NotificationDraft.model_validate(
            {key: value for key, value in raw.items() if key != "notification_id"}
        )
    except ValueError:
        return None
    if draft.kind not in AGENT_KINDS:
        return None
    return turn_id, draft


def summarize_reply(content: str, *, title_chars: int, body_chars: int) -> tuple[str, str]:
    """Title (first non-empty line) and body (bounded excerpt) for a reply_ready event.

    When the title carries the whole first line, the body continues after it so the
    two never repeat each other; a one-line reply therefore has an empty body. When
    the first line had to be shortened, the body keeps the full text.
    """
    text = content.strip()
    lines = text.splitlines()
    first_index = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first_index is None:
        return "Reply ready", ""
    headline = " ".join(lines[first_index].strip().lstrip("#>*- ").split())
    title = _truncate(headline, title_chars) or "Reply ready"
    if title != headline:
        return title, _truncate(text, body_chars)
    rest = "\n".join(lines[first_index + 1 :]).strip()
    return title, _truncate(rest, body_chars)


def _truncate(text: str, limit: int) -> str:
    if limit <= 0:
        return ""
    if len(text) <= limit:
        return text
    return text[: max(limit - 1, 0)].rstrip() + "…"
