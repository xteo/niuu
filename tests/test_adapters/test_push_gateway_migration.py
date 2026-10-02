"""The push gateway migration ships identically in all three places, idempotently."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NAME = "000073_push_gateway"


def _chart_block(name: str) -> str:
    chart = (ROOT / "charts/volundr/templates/migrations-configmap.yaml").read_text()
    start = chart.index(f"  {name}: |\n") + len(f"  {name}: |\n")
    lines = []
    for line in chart[start:].splitlines():
        if line and not line.startswith("    "):
            break
        lines.append(line[4:])
    assert start < chart.rindex("{{- end }}"), "migration must sit inside the enabled guard"
    return "\n".join(lines).strip()


@pytest.mark.parametrize("direction", ["up", "down"])
def test_migration_is_identical_in_all_three_locations(direction):
    name = f"{NAME}.{direction}.sql"
    source = (ROOT / "migrations" / name).read_bytes()
    assert (ROOT / "src/cli/migrations/volundr" / name).read_bytes() == source
    assert _chart_block(name) == source.decode().strip()


def test_it_is_the_newest_volundr_migration():
    names = sorted(path.name for path in (ROOT / "src/cli/migrations/volundr").glob("*.up.sql"))
    assert names[-1] == f"{NAME}.up.sql"


def test_up_migration_only_uses_idempotent_ddl():
    sql = (ROOT / "migrations" / f"{NAME}.up.sql").read_text()
    assert re.findall(r"CREATE (?:TABLE|(?:UNIQUE )?INDEX)\b(?! IF NOT EXISTS)", sql) == []
    assert re.findall(r"ADD COLUMN\b(?! IF NOT EXISTS)", sql) == []
    assert re.findall(r"DROP CONSTRAINT\b(?! IF EXISTS)", sql) == []
    for column in ("kind", "environment", "activity_id", "last_push_at", "push_due"):
        assert f"ADD COLUMN IF NOT EXISTS {column} " in sql
    for table in ("forge_push_gateway_state", "forge_push_deliveries"):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in sql


def test_a_token_is_unique_per_kind_not_per_owner():
    sql = (ROOT / "migrations" / f"{NAME}.up.sql").read_text()
    assert "DROP CONSTRAINT IF EXISTS device_tokens_owner_id_token_key" in sql
    assert "CREATE UNIQUE INDEX IF NOT EXISTS idx_device_tokens_token_kind" in sql
    assert "ON device_tokens (token, kind)" in sql
    # Rows two owners registered for one token are reduced to the newest first.
    assert sql.index("DELETE FROM device_tokens older") < sql.index("CREATE UNIQUE INDEX")


def test_existing_tokens_become_production_alert_tokens():
    sql = (ROOT / "migrations" / f"{NAME}.up.sql").read_text()
    assert "kind TEXT NOT NULL DEFAULT 'alert'" in sql
    assert "environment TEXT NOT NULL DEFAULT 'production'" in sql


def test_down_migration_restores_the_old_shape():
    down = (ROOT / "migrations" / f"{NAME}.down.sql").read_text()
    assert down.count("DROP TABLE IF EXISTS") == 2
    assert "DELETE FROM device_tokens WHERE kind <> 'alert'" in down
    assert "ADD CONSTRAINT device_tokens_owner_id_token_key UNIQUE (owner_id, token)" in down
    # Other kinds go before the per-owner constraint returns and the column leaves.
    assert down.index("DELETE FROM device_tokens") < down.index("ADD CONSTRAINT")
    assert down.index("ADD CONSTRAINT") < down.index("DROP COLUMN IF EXISTS kind")
