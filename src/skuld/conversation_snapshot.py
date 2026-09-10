"""Fit legacy complete snapshots and explicitly negotiated recent-history windows."""

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


def prepare_recent_snapshot(frame: dict, *, max_bytes: int, max_turns: int) -> dict:
    """Project a bounded recent window; the full transcript remains the paging source.

    Offsets refer to the original conversation, including an in-progress trailing
    turn. An unusually large single turn is an explicit preview, never presented
    as a complete replacement for its stored source.
    """
    if max_bytes <= 0 or max_turns <= 0:
        raise ValueError("Recent snapshot budgets must be positive")
    source = frame["turns"]
    skipped = max(0, len(source) - max_turns)
    recent = {
        **frame,
        "turns": elide_turns(source[skipped:]),
        "total_turns": frame.get("total_turns", len(source)),
        "window_offset": frame.get("window_offset", 0) + skipped,
        "recent_window": True,
        "detail": SHALLOW_DETAIL,
    }
    while len(recent["turns"]) > 1 and snapshot_byte_size(recent) > max_bytes:
        recent["turns"] = recent["turns"][1:]
        recent["window_offset"] += 1
    if snapshot_byte_size(recent) <= max_bytes:
        return recent
    if not recent["turns"]:
        raise ConversationSnapshotTooLargeError("Recent snapshot metadata exceeds byte budget")

    turn = dict(recent["turns"][0])
    recent["turns"] = [turn]
    recent["history_preview"] = True
    turn["history_preview"] = True
    text_budget = max_bytes // 4
    for key in ("content", "reasoning"):
        if isinstance(turn.get(key), str):
            turn[key] = _text_tail(turn[key], text_budget)
    parts = [dict(part) for part in (turn.get("parts") or [])]
    original_part_count = len(parts)
    turn["parts"] = parts
    while len(parts) > 1 and snapshot_byte_size(recent) > max_bytes:
        parts = parts[1:]
        turn["parts"] = parts
    if parts and snapshot_byte_size(recent) > max_bytes:
        last = dict(parts[-1])
        if last.get("type") in ("text", "thinking", "reasoning"):
            for key in ("text", "thinking", "content"):
                if isinstance(last.get(key), str):
                    last[key] = _text_tail(last[key], text_budget)
            turn["parts"] = [last]
        if snapshot_byte_size(recent) > max_bytes:
            # The original tool/attachment remains available through REST. Do not
            # fabricate a successful result or an actionable partial question.
            turn["parts"] = []
    turn["preview_omitted_parts"] = original_part_count - len(turn["parts"])
    # JSON escaping can expand a control character to six wire bytes. Measure
    # the actual envelope after each trim rather than assuming UTF-8 length fits.
    while snapshot_byte_size(recent) > max_bytes and text_budget > 0:
        text_budget //= 2
        for key in ("content", "reasoning"):
            if isinstance(turn.get(key), str):
                turn[key] = _text_tail(turn[key], text_budget)
        for part in turn["parts"]:
            if part.get("type") in ("text", "thinking", "reasoning"):
                for key in ("text", "thinking", "content"):
                    if isinstance(part.get(key), str):
                        part[key] = _text_tail(part[key], text_budget)
    if snapshot_byte_size(recent) > max_bytes:
        raise ConversationSnapshotTooLargeError("Recent turn metadata exceeds byte budget")
    return recent


def _text_tail(text: str, max_bytes: int) -> str:
    """Keep the newest text without splitting a UTF-8 character."""
    if max_bytes <= 0:
        return ""
    return text.encode("utf-8")[-max_bytes:].decode("utf-8", errors="ignore")
