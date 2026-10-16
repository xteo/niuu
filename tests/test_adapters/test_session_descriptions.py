"""Agent-maintained session descriptions (LEX-1264): contract, writes, auth and events."""

from __future__ import annotations

import uuid

import pytest
from pydantic import ValidationError

from niuu.domain.session_description import (
    DESCRIPTION_MAX_CHARS,
    SHORT_DESCRIPTION_MAX_CHARS,
    SessionDescriptionPatch,
    normalize_description,
    normalize_short_description,
)
from tests.conftest import InMemorySessionRepository
from tests.support.forge_session import OWNER, PREFIX, TENANT, bearer, build_forge_app
from volundr.adapters.inbound.rest import SessionCreate, SessionResponse
from volundr.adapters.outbound.broadcaster import InMemoryEventBroadcaster
from volundr.domain.models import EventType, Session

OWNER_HEADERS = {"x-auth-user-id": OWNER, "x-auth-tenant": TENANT}
JSON_RPC = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
FIELDS = (
    "short_description",
    "description",
    "short_description_updated_at",
    "description_updated_at",
)


class RecordingBroadcaster(InMemoryEventBroadcaster):
    def __init__(self) -> None:
        super().__init__()
        self.events = []

    async def publish(self, event) -> None:
        self.events.append(event)
        await super().publish(event)

    def updates(self) -> list[dict]:
        return [e.data for e in self.events if e.type is EventType.SESSION_UPDATED]


@pytest.fixture
def broadcaster():
    return RecordingBroadcaster()


@pytest.fixture
def forge(broadcaster):
    return build_forge_app(broadcaster=broadcaster)


def _describe(forge, session_id, body, headers=OWNER_HEADERS):
    return forge.client.put(
        f"{PREFIX}/sessions/{session_id}/description", json=body, headers=headers
    )


class TestNormalization:
    def test_short_folds_to_one_line(self) -> None:
        assert normalize_short_description("  Fixing\n\tthe   CI \r\n run ") == "Fixing the CI run"

    def test_short_truncates(self) -> None:
        value = normalize_short_description("x" * (SHORT_DESCRIPTION_MAX_CHARS + 50))
        assert value == "x" * SHORT_DESCRIPTION_MAX_CHARS

    def test_truncation_never_leaves_trailing_space(self) -> None:
        text = "a" * (SHORT_DESCRIPTION_MAX_CHARS - 1) + " bcd"
        assert normalize_short_description(text) == "a" * (SHORT_DESCRIPTION_MAX_CHARS - 1)

    def test_long_keeps_markdown_lines(self) -> None:
        assert normalize_description("\n## Status\n- a\n- b\n\n") == "## Status\n- a\n- b"

    def test_long_truncates(self) -> None:
        value = normalize_description("y" * (DESCRIPTION_MAX_CHARS + 1))
        assert value is not None and len(value) == DESCRIPTION_MAX_CHARS

    @pytest.mark.parametrize("blank", ["", "   ", "\n\t \n", None])
    def test_blank_is_unset(self, blank) -> None:
        assert normalize_short_description(blank) is None
        assert normalize_description(blank) is None

    def test_patch_tells_omitted_from_null(self) -> None:
        assert SessionDescriptionPatch.model_validate({}).changes() == {}
        assert SessionDescriptionPatch.model_validate({"description": None}).changes() == {
            "description": None
        }
        assert SessionDescriptionPatch.model_validate(
            {"short_description": " a\nb "}
        ).changes() == {"short_description": "a b"}

    @pytest.mark.parametrize("body", [{"summary": "x"}, {"short_description": 5}])
    def test_patch_rejects_unknown_keys_and_wrong_types(self, body) -> None:
        with pytest.raises(ValidationError):
            SessionDescriptionPatch.model_validate(body)


class TestRepository:
    async def test_generic_update_never_overwrites_a_fresher_description(self) -> None:
        repo = InMemorySessionRepository()
        session = await repo.create(Session(name="s"))
        stale = await repo.get(session.id)
        stamp = stale.created_at
        await repo.update_description(session.id, {"short_description": "Fresh"}, stamp)
        saved = await repo.update(stale.model_copy(update={"message_count": 3}))
        assert saved.short_description == "Fresh" and saved.message_count == 3
        assert (await repo.get(session.id)).short_description == "Fresh"

    async def test_unchanged_value_keeps_its_stamp(self) -> None:
        repo = InMemorySessionRepository()
        session = await repo.create(Session(name="s"))
        first = await repo.update_description(
            session.id, {"short_description": "A"}, session.created_at
        )
        later = first.short_description_updated_at.replace(year=2099)
        again = await repo.update_description(session.id, {"short_description": "A"}, later)
        assert again.short_description_updated_at == first.short_description_updated_at
        assert again.updated_at == first.updated_at

    async def test_missing_session(self) -> None:
        repo = InMemorySessionRepository()
        assert await repo.update_description(uuid.uuid4(), {"description": "x"}, None) is None


class _RacingRepository(InMemorySessionRepository):
    """The agent describes the session between a lifecycle path's read and its write."""

    racing = False

    async def get(self, session_id):
        session = await super().get(session_id)
        if self.racing and session is not None:
            await self.update_description(
                session_id, {"short_description": "Fresh"}, session.created_at
            )
        return session


class TestStaleCopiesNeverPublishAStaleDescription:
    async def test_usage_report(self) -> None:
        from tests.conftest import InMemoryTokenTracker
        from volundr.domain.models import ModelProvider, SessionStatus
        from volundr.domain.services.token import TokenService

        repo, broadcaster = _RacingRepository(), RecordingBroadcaster()
        session = await repo.create(Session(name="s", status=SessionStatus.RUNNING))
        service = TokenService(InMemoryTokenTracker(), repo, broadcaster=broadcaster)
        repo.racing = True
        await service.record_usage(session.id, 5, ModelProvider.CLOUD, "m")
        assert broadcaster.updates()[-1]["short_description"] == "Fresh"
        assert broadcaster.updates()[-1]["tokens_used"] == 5

    async def test_stop(self) -> None:
        from tests.conftest import MockPodManager
        from volundr.domain.models import SessionStatus
        from volundr.domain.services.session import SessionService

        repo, broadcaster = _RacingRepository(), RecordingBroadcaster()
        session = await repo.create(Session(name="s", status=SessionStatus.RUNNING))
        service = SessionService(repo, MockPodManager(), broadcaster=broadcaster)
        repo.racing = True
        await service.stop_session(session.id)
        assert [frame["short_description"] for frame in broadcaster.updates()] == [
            "Fresh",
            "Fresh",
        ]


class TestRestContract:
    async def test_records_always_carry_all_four_keys(self, forge) -> None:
        session = await forge.session("plain")
        record = forge.client.get(f"{PREFIX}/sessions/{session.id}", headers=OWNER_HEADERS).json()
        assert {key: record[key] for key in FIELDS} == dict.fromkeys(FIELDS)
        listed = forge.client.get(f"{PREFIX}/sessions", headers=OWNER_HEADERS).json()
        assert all(set(FIELDS) <= set(item) for item in listed)

    async def test_partial_write_normalizes_and_stamps(self, forge, broadcaster) -> None:
        session = await forge.session("own")
        response = _describe(forge, session.id, {"short_description": "  Wiring\n the   route  "})
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["id"] == str(session.id)
        assert body["short_description"] == "Wiring the route"
        assert body["short_description_updated_at"] is not None
        assert body["description"] is None and body["description_updated_at"] is None
        [frame] = broadcaster.updates()
        assert frame["short_description"] == "Wiring the route"
        assert frame["short_description_updated_at"] == body["short_description_updated_at"]
        assert {key: frame[key] for key in FIELDS[1::2]} == {
            "description": None,
            "description_updated_at": None,
        }

    async def test_omitted_key_is_kept_and_null_clears(self, forge) -> None:
        session = await forge.session("own")
        _describe(forge, session.id, {"short_description": "One", "description": "## Two"})
        kept = _describe(forge, session.id, {"description": "## Three"}).json()
        assert kept["short_description"] == "One" and kept["description"] == "## Three"
        cleared = _describe(forge, session.id, {"short_description": None}).json()
        assert cleared["short_description"] is None and cleared["description"] == "## Three"
        assert cleared["short_description_updated_at"] != kept["short_description_updated_at"]

    async def test_blank_is_stored_as_null(self, forge) -> None:
        session = await forge.session("own")
        _describe(forge, session.id, {"short_description": "One"})
        blank = _describe(forge, session.id, {"short_description": " \n "}).json()
        assert blank["short_description"] is None

    async def test_truncates_long_values(self, forge) -> None:
        session = await forge.session("own")
        body = _describe(
            forge,
            session.id,
            {"short_description": "s" * 500, "description": "d" * (DESCRIPTION_MAX_CHARS * 2)},
        ).json()
        assert len(body["short_description"]) == SHORT_DESCRIPTION_MAX_CHARS
        assert len(body["description"]) == DESCRIPTION_MAX_CHARS

    async def test_unchanged_value_does_not_bump_the_stamp_or_publish(
        self, forge, broadcaster
    ) -> None:
        session = await forge.session("own")
        first = _describe(forge, session.id, {"short_description": "Same"}).json()
        second = _describe(forge, session.id, {"short_description": "  Same "}).json()
        assert second["short_description_updated_at"] == first["short_description_updated_at"]
        assert second["updated_at"] == first["updated_at"]
        assert len(broadcaster.updates()) == 1

    async def test_empty_body_changes_nothing(self, forge, broadcaster) -> None:
        session = await forge.session("own")
        response = _describe(forge, session.id, {})
        assert response.status_code == 200 and response.json()["short_description"] is None
        assert broadcaster.updates() == []

    @pytest.mark.parametrize("body", [{"summary": "x"}, {"description": ["x"]}])
    async def test_bad_bodies_are_422(self, forge, body) -> None:
        session = await forge.session("own")
        assert _describe(forge, session.id, body).status_code == 422

    async def test_unknown_session_is_404(self, forge) -> None:
        assert _describe(forge, uuid.uuid4(), {"description": "x"}).status_code == 404

    async def test_create_seeds_the_descriptions(self, forge, broadcaster) -> None:
        response = forge.client.post(
            f"{PREFIX}/sessions",
            json={"name": "seeded", "short_description": " Triage\nLEX-1 ", "description": " "},
            headers=OWNER_HEADERS,
        )
        assert response.status_code == 201, response.text
        body = response.json()
        assert body["short_description"] == "Triage LEX-1"
        assert body["short_description_updated_at"] == body["created_at"]
        assert body["description"] is None and body["description_updated_at"] is None
        created = next(e for e in broadcaster.events if e.type is EventType.SESSION_CREATED)
        assert created.data["short_description"] == "Triage LEX-1"

    def test_create_model_keeps_unsent_keys_unset(self) -> None:
        assert {"short_description", "description"}.isdisjoint(
            SessionCreate(name="a").model_fields_set
        )

    def test_response_maps_the_stamps(self) -> None:
        session = Session(name="s", short_description="x")
        stamped = session.model_copy(update={"short_description_updated_at": session.created_at})
        response = SessionResponse.from_session(stamped)
        assert response.short_description == "x"
        assert response.short_description_updated_at == session.created_at.isoformat()
        assert response.description_updated_at is None


class TestSessionToken:
    async def _pair(self, forge):
        own = await forge.session("own")
        peer = await forge.session("peer")
        return own, peer, (await forge.start(own)).token

    async def test_own_token_may_describe_its_own_session(self, forge) -> None:
        own, _, token = await self._pair(forge)
        response = _describe(forge, own.id, {"short_description": "Mine"}, headers=bearer(token))
        assert response.status_code == 200, response.text
        assert (await forge.sessions.get(own.id)).short_description == "Mine"

    async def test_own_token_may_not_describe_a_peer(self, forge) -> None:
        _, peer, token = await self._pair(forge)
        response = _describe(forge, peer.id, {"short_description": "x"}, headers=bearer(token))
        assert response.status_code == 403
        assert "bound to another session" in response.json()["detail"]
        assert (await forge.sessions.get(peer.id)).short_description is None

    async def test_generic_update_stays_denied_to_the_token(self, forge) -> None:
        own, _, token = await self._pair(forge)
        response = forge.client.put(
            f"{PREFIX}/sessions/{own.id}", json={"name": "renamed"}, headers=bearer(token)
        )
        assert response.status_code == 403

    async def test_describe_session_over_the_forge_mcp(self, forge, broadcaster) -> None:
        own, _, token = await self._pair(forge)
        message = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {
                "name": "describe_session",
                "arguments": {"short_description": "Via MCP", "description": "## Notes"},
            },
        }
        response = forge.client.post(
            f"{PREFIX}/mcp", json=message, headers={**JSON_RPC, **bearer(token)}
        )
        result = response.json()["result"]
        assert result["isError"] is False, result
        structured = result["structuredContent"]
        assert structured["session_id"] == str(own.id)
        assert structured["short_description"] == "Via MCP"
        assert structured["short_description_updated_at"] is not None
        stored = await forge.sessions.get(own.id)
        assert stored.description == "## Notes"
        assert broadcaster.updates()[-1]["description"] == "## Notes"
