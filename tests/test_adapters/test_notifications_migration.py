"""Migration 000069 ships identically in all three places and is idempotent SQL."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NAME = "000069_forge_notifications"


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


def test_up_migration_only_uses_idempotent_ddl():
    sql = (ROOT / "migrations" / f"{NAME}.up.sql").read_text()
    creates = re.findall(r"CREATE (?:TABLE|INDEX)\b(?! IF NOT EXISTS)", sql)
    assert creates == []
    for table in (
        "forge_notifications",
        "forge_notification_read_states",
        "forge_notification_rules",
        "forge_notification_deliveries",
    ):
        assert f"CREATE TABLE IF NOT EXISTS {table} (" in sql
    down = (ROOT / "migrations" / f"{NAME}.down.sql").read_text()
    assert down.count("DROP TABLE IF EXISTS") == 4
    # Children first, so the down migration never trips a foreign key.
    assert down.index("forge_notification_deliveries") < down.index("forge_notifications;")
