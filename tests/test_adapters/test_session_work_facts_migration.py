"""Migration 000088 (G10, F12d) ships identically from the repo, the packaged CLI and the chart."""

from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NAME = "000088_session_work_facts"


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


def test_up_is_idempotent_and_down_reverses_it():
    up = (ROOT / "migrations" / f"{NAME}.up.sql").read_text()
    down = (ROOT / "migrations" / f"{NAME}.down.sql").read_text()
    assert "ADD COLUMN IF NOT EXISTS last_work_at TIMESTAMPTZ;" in up
    assert "ADD COLUMN IF NOT EXISTS open_tool_count INTEGER NOT NULL DEFAULT 0;" in up
    assert "DROP COLUMN IF EXISTS last_work_at;" in down
    assert "DROP COLUMN IF EXISTS open_tool_count;" in down


def test_number_is_unique():
    numbers = sorted(p.name[:6] for p in (ROOT / "migrations").glob("*.up.sql"))
    assert numbers.count(NAME[:6]) == 1
