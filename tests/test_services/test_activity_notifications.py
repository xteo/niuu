"""update_activity records an attention notification for each new needs-input request."""

from __future__ import annotations

import pytest

from volundr.domain.models import GitSource, SessionActivityState
from volundr.domain.notification_ports import NotificationRecorder
from volundr.domain.services import SessionService


class RecordingRecorder(NotificationRecorder):
    def __init__(self) -> None:
        self.calls: list[dict] = []

    async def record_attention(self, session, *, state_since, kind, prompt, request_id):
        self.calls.append(
            {
                "session_id": session.id,
                "state_since": state_since,
                "kind": kind,
                "prompt": prompt,
                "request_id": request_id,
            }
        )


@pytest.fixture
def recorder() -> RecordingRecorder:
    return RecordingRecorder()


@pytest.fixture(params=["with_broadcaster", "without_broadcaster"])
def service(request, repository, pod_manager, broadcaster, recorder):
    return SessionService(
        repository=repository,
        pod_manager=pod_manager,
        broadcaster=broadcaster if request.param == "with_broadcaster" else None,
        notification_recorder=recorder,
        provisioning_initial_delay=0,
        provisioning_timeout=1.0,
    )


async def _session(service):
    return await service.create_session(
        name="Blocked",
        model="claude-sonnet-4-20250514",
        source=GitSource(repo="https://github.com/test/repo", branch="main"),
    )


async def test_new_request_records_attention_with_state_since(service, recorder):
    session = await _session(service)
    updated = await service.update_activity(
        session.id,
        SessionActivityState.AWAITING_INPUT,
        {"kind": "permission", "prompt": "Run rm?", "request_id": "perm-1"},
    )
    assert recorder.calls == [
        {
            "session_id": session.id,
            "state_since": updated.activity_state_since,
            "kind": "permission",
            "prompt": "Run rm?",
            "request_id": "perm-1",
        }
    ]


async def test_repeats_and_heartbeats_record_nothing_new(service, recorder):
    session = await _session(service)
    meta = {"kind": "question", "request_id": "q1"}
    await service.update_activity(session.id, SessionActivityState.AWAITING_INPUT, dict(meta))
    await service.update_activity(session.id, SessionActivityState.AWAITING_INPUT, dict(meta))
    await service.update_activity(
        session.id, SessionActivityState.AWAITING_INPUT, {**meta, "heartbeat": True}
    )
    await service.update_activity(session.id, SessionActivityState.ACTIVE, {})
    assert [call["request_id"] for call in recorder.calls] == ["q1"]


async def test_second_question_while_awaiting_is_recorded(service, recorder):
    session = await _session(service)
    await service.update_activity(
        session.id, SessionActivityState.AWAITING_INPUT, {"request_id": "q1"}
    )
    await service.update_activity(
        session.id, SessionActivityState.AWAITING_INPUT, {"request_id": "q2"}
    )
    assert [call["request_id"] for call in recorder.calls] == ["q1", "q2"]
    assert recorder.calls[0]["state_since"] == recorder.calls[1]["state_since"]
    assert recorder.calls[1]["kind"] == "question" and recorder.calls[1]["prompt"] == ""
