"""Acceptance must distinguish RPC acknowledgment from actual agent execution."""

import argparse
import asyncio
import json
from unittest.mock import AsyncMock, Mock

import pytest

from scripts.forge_pi_probe import PiProbe, assert_agent_turn


def completed_turn():
    return [
        {"type": "message_update"},
        {"type": "tool_execution_end", "toolCallId": "read-1"},
        {
            "type": "agent_end",
            "messages": [
                {
                    "role": "assistant",
                    "stopReason": "stop",
                    "content": [
                        {"type": "text", "text": "PI_FORGE_PROBE=42"},
                    ],
                },
            ],
        },
    ]


def test_agent_acceptance_requires_actual_execution():
    assert_agent_turn(completed_turn())
    with pytest.raises(AssertionError, match="No agent_end"):
        assert_agent_turn([{"type": "response", "command": "prompt", "success": True}])


@pytest.mark.parametrize("reason", ["error", "aborted"])
def test_failed_or_aborted_turn_never_passes(reason):
    events = completed_turn()
    events[-1]["messages"][0]["stopReason"] = reason
    with pytest.raises(AssertionError, match="agent failed"):
        assert_agent_turn(events)


@pytest.mark.parametrize(
    "missing,expected",
    [
        ("tool_execution_end", "did not execute a tool"),
        ("message_update", "did not stream"),
    ],
)
def test_partial_trace_never_passes(missing, expected):
    with pytest.raises(AssertionError, match=expected):
        assert_agent_turn([e for e in completed_turn() if e["type"] != missing])


def test_empty_completion_never_passes():
    with pytest.raises(AssertionError, match="No completed assistant"):
        assert_agent_turn([{"type": "agent_end", "messages": []}])


def runtime_with_frames(frames):
    reader = asyncio.StreamReader()
    reader.feed_data(
        b"".join(json.dumps(frame, ensure_ascii=False).encode() + b"\n" for frame in frames)
    )
    reader.feed_eof()
    runtime = PiProbe(argparse.Namespace(timeout=1), [])
    runtime.process = Mock(stdout=reader, stdin=Mock(drain=AsyncMock()))
    return runtime


@pytest.mark.asyncio
async def test_command_correlates_reply_and_preserves_interleaved_events():
    runtime = runtime_with_frames(
        [
            {"type": "response", "id": "other", "success": True},
            {"type": "message_update", "text": "line\u2028separator\u2029intact"},
            {"type": "response", "id": "probe-1", "success": True, "data": {"answer": 42}},
        ]
    )
    assert await runtime.command("get_state") == {"answer": 42}
    assert len(runtime.frames) == 3
    assert runtime.frames[1]["text"] == "line\u2028separator\u2029intact"


@pytest.mark.asyncio
async def test_rejected_command_is_not_success():
    runtime = runtime_with_frames(
        [
            {"type": "response", "id": "probe-1", "success": False, "error": "No model"},
        ]
    )
    with pytest.raises(RuntimeError, match="No model"):
        await runtime.command("prompt", message="work")


@pytest.mark.asyncio
async def test_eof_and_non_object_frames_fail():
    with pytest.raises(RuntimeError, match="closed stdout"):
        await runtime_with_frames([]).read()
    with pytest.raises(ValueError, match="non-object"):
        await runtime_with_frames([[]]).read()
