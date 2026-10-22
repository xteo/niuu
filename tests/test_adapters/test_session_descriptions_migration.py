"""Migration 000087 ships identically from the repo, the packaged CLI and the Helm chart."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NAME = "000087_session_descriptions"
COLUMNS = (
    "short_description TEXT",
    "description TEXT",
    "short_description_updated_at TIMESTAMPTZ",
    "description_updated_at TIMESTAMPTZ",
)


@pytest.mark.parametrize("direction", ["up", "down"])
def test_three_copies_match(direction):
    name = f"{NAME}.{direction}.sql"
    source = (ROOT / "migrations" / name).read_text()
    assert (ROOT / "src/cli/migrations/volundr" / name).read_text() == source
    chart = (ROOT / "charts/volundr/templates/migrations-configmap.yaml").read_text()
    start = chart.index(f"  {name}: |\n") + len(f"  {name}: |\n")
    lines = []
    for line in chart[start:].splitlines():
        if line and not line.startswith("    "):
            break
        lines.append(line[4:])
    assert "\n".join(lines).strip() == source.strip()
    assert start < chart.rindex("{{- end }}")


def test_up_adds_four_nullable_idempotent_columns_and_down_drops_them():
    up = (ROOT / "migrations" / f"{NAME}.up.sql").read_text()
    down = (ROOT / "migrations" / f"{NAME}.down.sql").read_text()
    for column in COLUMNS:
        assert f"ALTER TABLE sessions ADD COLUMN IF NOT EXISTS {column};" in up
        assert f"DROP COLUMN IF EXISTS {column.split()[0]};" in down
    assert "NOT NULL" not in up and "DEFAULT" not in up


def test_follows_the_migrations_it_was_written_after():
    numbers = sorted(p.name[:6] for p in (ROOT / "migrations").glob("*.up.sql"))
    assert numbers.count(NAME[:6]) == 1
    assert NAME[:6] > "000086"
