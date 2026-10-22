"""LEX-1406 document constraint, including opt-in disposable PostgreSQL proof."""

import asyncio
import json
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
import pytest_asyncio
import yaml

from tests.test_adapters.test_message_delivery_postgres_integration import (
    isolated_pool as postgres_pool_fixture,
)
from volundr.adapters.outbound.postgres_projects import PostgresProjectRepository
from volundr.domain.project_ports import ProjectConflictError
from volundr.domain.projects import ForgeProject, ProjectSideQuestDefaults

ROOT = Path(__file__).resolve().parents[2]
MIGRATIONS = ROOT / "migrations"
NAME = "000091_project_side_quest_defaults"
CONSTRAINT = "forge_projects_side_quest_defaults_check"
VALID = {"host": "thor-exp", "local_path": "/home/exp/workspaces/side-quest"}
isolated_pool = postgres_pool_fixture


def _sql(direction):
    return (MIGRATIONS / f"{NAME}.{direction}.sql").read_text()


@pytest.mark.parametrize("direction", ["up", "down"])
def test_packaged_and_chart_migrations_match(direction):
    filename = f"{NAME}.{direction}.sql"
    source = _sql(direction)
    assert (ROOT / "src/cli/migrations/volundr" / filename).read_text() == source
    chart = (ROOT / "charts/volundr/templates/migrations-configmap.yaml").read_text()
    data = chart[chart.index("\ndata:\n") + 1 : chart.rindex("{{- end")]
    assert yaml.safe_load(data)["data"][filename].rstrip("\n") == source.rstrip("\n")


def test_migration_number_is_unique():
    assert len(list(MIGRATIONS.glob(f"{NAME[:6]}_*.up.sql"))) == 1


@pytest_asyncio.fixture
async def project_pool(isolated_pool):
    await isolated_pool.execute("CREATE TABLE sessions (id UUID PRIMARY KEY)")
    await isolated_pool.execute((MIGRATIONS / "000073_forge_projects.up.sql").read_text())
    return isolated_pool


async def _insert(pool, document):
    project_id = uuid4()
    await pool.execute(
        "INSERT INTO forge_projects(id,document) VALUES ($1,$2::jsonb)",
        project_id,
        json.dumps(document),
    )
    return project_id


@pytest.mark.integration
async def test_upgrade_preserves_legacy_missing_and_explicit_null_without_backfill(project_pool):
    documents = [
        {"name": "legacy"},
        {"name": "cleared", "side_quest_defaults": None},
        {"name": "configured", "side_quest_defaults": VALID},
    ]
    for document in documents:
        await _insert(project_pool, document)
    before = await project_pool.fetch("SELECT * FROM forge_projects ORDER BY id")
    await project_pool.execute(_sql("up"))
    await project_pool.execute(_sql("up"))
    assert await project_pool.fetch("SELECT * FROM forge_projects ORDER BY id") == before
    assert (
        await project_pool.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname=$1 "
            "AND conrelid='forge_projects'::regclass",
            CONSTRAINT,
        )
        == 1
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "document",
    [
        {},
        {"side_quest_defaults": None},
        {"side_quest_defaults": VALID},
        {"side_quest_defaults": {"host": "a", "local_path": "/"}},
        {"side_quest_defaults": {"host": "A_9.-", "local_path": "/two words/日本語"}},
        {"side_quest_defaults": {"host": "a" * 100, "local_path": "/" + "a" * 2047}},
    ],
    ids=["missing", "null", "configured", "minimum", "posix-unicode", "maximum"],
)
async def test_check_accepts_supported_document_states(project_pool, document):
    await project_pool.execute(_sql("up"))
    project_id = await _insert(project_pool, document)
    assert (
        json.loads(
            await project_pool.fetchval(
                "SELECT document FROM forge_projects WHERE id=$1", project_id
            )
        )
        == document
    )


@pytest.mark.integration
@pytest.mark.parametrize(
    "value",
    [
        False,
        1,
        "thor-exp",
        [],
        {},
        {"host": "thor-exp"},
        {"local_path": VALID["local_path"]},
        {**VALID, "extra": True},
        {**VALID, "host": None},
        {**VALID, "host": 3},
        {**VALID, "host": False},
        {**VALID, "host": []},
        {**VALID, "host": {}},
        {**VALID, "host": ""},
        {**VALID, "host": "a" * 101},
        {**VALID, "host": "thor exp"},
        {**VALID, "host": "thor/exp"},
        {**VALID, "host": "thor:exp"},
        {**VALID, "host": "thör-exp"},
        {**VALID, "host": "thor-exp\n"},
        {**VALID, "local_path": None},
        {**VALID, "local_path": 3},
        {**VALID, "local_path": False},
        {**VALID, "local_path": []},
        {**VALID, "local_path": {}},
        {**VALID, "local_path": ""},
        {**VALID, "local_path": "relative/folder"},
        {**VALID, "local_path": "~/folder"},
        {**VALID, "local_path": "C:\\folder"},
        {**VALID, "local_path": "/folder\\child"},
        {**VALID, "local_path": "/" + "a" * 2048},
        *({**VALID, "local_path": "/folder" + chr(code)} for code in [1, 9, 10, 13, 31, 127]),
    ],
    ids=[
        "boolean",
        "number",
        "string",
        "array",
        "empty-object",
        "missing-path",
        "missing-host",
        "extra-key",
        "null-host",
        "numeric-host",
        "boolean-host",
        "array-host",
        "object-host",
        "empty-host",
        "long-host",
        "space-host",
        "slash-host",
        "colon-host",
        "unicode-host",
        "newline-host",
        "null-path",
        "numeric-path",
        "boolean-path",
        "array-path",
        "object-path",
        "empty-path",
        "relative-path",
        "tilde-path",
        "windows-path",
        "backslash-path",
        "long-path",
        "control-1",
        "tab",
        "newline",
        "carriage-return",
        "control-31",
        "delete",
    ],
)
async def test_check_rejects_malformed_defaults_on_insert_and_update(project_pool, value):
    await project_pool.execute(_sql("up"))
    invalid = {"side_quest_defaults": value}
    with pytest.raises(asyncpg.CheckViolationError) as insert_error:
        await _insert(project_pool, invalid)
    assert insert_error.value.constraint_name == CONSTRAINT
    project_id = await _insert(project_pool, {"side_quest_defaults": VALID})
    with pytest.raises(asyncpg.CheckViolationError) as update_error:
        await project_pool.execute(
            "UPDATE forge_projects SET document=$2::jsonb WHERE id=$1",
            project_id,
            json.dumps(invalid),
        )
    assert update_error.value.constraint_name == CONSTRAINT


@pytest.mark.integration
async def test_postgres_jsonb_rejects_nul_before_constraint(project_pool):
    await project_pool.execute(_sql("up"))
    with pytest.raises(asyncpg.UntranslatableCharacterError):
        await _insert(project_pool, {"side_quest_defaults": {**VALID, "local_path": "/bad\0"}})


@pytest.mark.integration
async def test_downgrade_strips_only_new_top_level_field_and_is_repeatable(project_pool):
    await project_pool.execute(_sql("up"))
    for defaults in [None, VALID]:
        await _insert(
            project_pool,
            {
                "name": "keep",
                "nested": {"side_quest_defaults": "keep"},
                "side_quest_defaults": defaults,
            },
        )
    await _insert(project_pool, {"name": "legacy"})
    before = await project_pool.fetch("SELECT * FROM forge_projects ORDER BY id")
    await project_pool.execute(_sql("down"))
    await project_pool.execute(_sql("down"))
    after = await project_pool.fetch("SELECT * FROM forge_projects ORDER BY id")
    for previous, current in zip(before, after, strict=True):
        document = json.loads(previous["document"])
        document.pop("side_quest_defaults", None)
        assert json.loads(current["document"]) == document
        assert {k: v for k, v in current.items() if k != "document"} == {
            k: v for k, v in previous.items() if k != "document"
        }
    assert (
        await project_pool.fetchval(
            "SELECT count(*) FROM pg_constraint WHERE conname=$1 "
            "AND conrelid='forge_projects'::regclass",
            CONSTRAINT,
        )
        == 0
    )
    await _insert(project_pool, {"side_quest_defaults": "no longer checked"})


@pytest.mark.integration
async def test_repository_roundtrip_retains_missing_null_and_object_states(project_pool):
    await project_pool.execute(_sql("up"))
    repo = PostgresProjectRepository(project_pool)
    inputs = [
        ForgeProject(name="Legacy"),
        ForgeProject(name="Cleared", side_quest_defaults=None),
        ForgeProject(name="Configured", side_quest_defaults=ProjectSideQuestDefaults(**VALID)),
    ]
    for project in inputs:
        created = await repo.register(project)
        fresh = await PostgresProjectRepository(project_pool).get(created.id)
        assert fresh.model_dump() == project.model_dump()
        assert ("side_quest_defaults" in fresh.model_fields_set) == (
            "side_quest_defaults" in project.model_fields_set
        )
        renamed = fresh.model_copy(update={"name": "Renamed", "revision": 2})
        await repo.update(renamed, expected_revision=1)
        stored = json.loads(
            await project_pool.fetchval(
                "SELECT document FROM forge_projects WHERE id=$1",
                project.id,
            )
        )
        assert ("side_quest_defaults" in stored) == (
            "side_quest_defaults" in project.model_fields_set
        )
        assert stored.get("side_quest_defaults") == project.model_dump().get("side_quest_defaults")


@pytest.mark.integration
async def test_defaults_compare_and_swap_has_one_winner_and_clear_persists(project_pool):
    await project_pool.execute(_sql("up"))
    repo = PostgresProjectRepository(project_pool)
    project = await repo.register(ForgeProject(name="Legacy"))
    updated = project.model_copy(
        update={
            "side_quest_defaults": ProjectSideQuestDefaults(**VALID),
            "revision": 2,
        }
    )
    cleared = project.model_copy(update={"side_quest_defaults": None, "revision": 2})
    results = await asyncio.gather(
        repo.update(updated, expected_revision=1),
        repo.update(cleared, expected_revision=1),
        return_exceptions=True,
    )
    assert sum(isinstance(result, ForgeProject) for result in results) == 1
    assert sum(isinstance(result, ProjectConflictError) for result in results) == 1
    winner = next(result for result in results if isinstance(result, ForgeProject))
    assert (await repo.get(project.id)).model_dump() == winner.model_dump()
    await repo.update(
        winner.model_copy(update={"side_quest_defaults": None, "revision": 3}),
        expected_revision=2,
    )
    recovered = await PostgresProjectRepository(project_pool).get(project.id)
    assert recovered.side_quest_defaults is None
    assert "side_quest_defaults" in recovered.model_fields_set
    assert await project_pool.fetchval(
        "SELECT document->'side_quest_defaults' = 'null'::jsonb FROM forge_projects WHERE id=$1",
        project.id,
    )
