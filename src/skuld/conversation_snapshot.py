"""Fit reconnect snapshots without dropping conversation turns or lazy tool references."""

import json

from skuld.conversation_shallow import SHALLOW_DETAIL, elide_turns


class ConversationSnapshotTooLargeError(ValueError):
    """Even a complete shallow snapshot exceeds the client's receive budget."""


def snapshot_byte_size(frame: dict) -> int:
    """Match Starlette WebSocket.send_json, used by the broker's reconnect path."""
    return len(json.dumps(frame, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def prepare_conversation_snapshot(frame: dict, *, max_bytes: int) -> dict:
    """Keep small snapshots unchanged; use existing lazy tool placeholders for large ones.

    Clients replace their canonical history from this frame. We therefore never
    window away turns: older deployed iOS clients do not read snapshot offsets.
    Oversized prose must use the REST history path instead of an incomplete seed.
    """
    if max_bytes <= 0:
        raise ValueError("Conversation snapshot byte budget must be positive")
    if snapshot_byte_size(frame) <= max_bytes:
        return frame
    shallow = {**frame, "turns": elide_turns(frame["turns"]), "detail": SHALLOW_DETAIL}
    size = snapshot_byte_size(shallow)
    if size > max_bytes:
        raise ConversationSnapshotTooLargeError(
            f"Complete shallow conversation snapshot is {size} bytes; limit is {max_bytes}"
        )
    return shallow
