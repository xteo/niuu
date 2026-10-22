"""LEX-1406: actual Forge project API rows reach CP through its existing poller."""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from tests.test_niuu.test_rest_volundr import _client, _headers, _instance
from tests.test_projects.test_side_quest_defaults import HOST, LOCAL, defaults, project_api

pytestmark = pytest.mark.integration


@pytest.mark.parametrize("use_legacy_path", [False, True])
def test_project_patch_and_clear_reach_cp_after_one_poll_each(tmp_path, use_legacy_path):
    configured = os.environ.get("CP_PROJECT_DEFAULTS_FIXTURE_COMMAND")
    if not configured:
        pytest.skip("requires frontend's project-defaults-consumer.fixture.ts command")
    command = json.loads(configured)
    assert isinstance(command, list) and all(isinstance(arg, str) for arg in command)
    legacy = tmp_path / "legacy"
    selected = tmp_path / "selected"
    legacy.mkdir()
    selected.mkdir()
    embedded, _ = project_api(tmp_path)
    api = _client(
        [
            _instance(LOCAL, base_url="embedded://local-forge", config={"transport": "embedded"}),
        ],
        embedded_forge_app=embedded,
    )
    created = api.post(
        "/api/v1/forge/projects", json={"name": "API defaults exercise"}, headers=_headers()
    )
    assert created.status_code == 201
    project_id = created.json()["id"]
    path = f"/api/v1/forge/projects/{project_id}?instance_id={LOCAL}"
    projects = f"/api/v1/forge/projects?instance_id={LOCAL}"
    rounds = [api.get(projects, headers=_headers()).json()]
    assert "side_quest_defaults" not in rounds[0][0]
    saved = api.patch(
        path, json={"revision": 1, "side_quest_defaults": defaults(selected)}, headers=_headers()
    )
    assert saved.status_code == 200
    assert saved.json()["instance_id"] == LOCAL
    rounds.append(api.get(projects, headers=_headers()).json())
    cleared = api.patch(path, json={"revision": 2, "side_quest_defaults": None}, headers=_headers())
    assert cleared.status_code == 200
    rounds.append(api.get(projects, headers=_headers()).json())
    result = subprocess.run(
        command,
        input=json.dumps(
            {
                "project_id": project_id,
                "rounds": rounds,
                **({"legacy_path": str(legacy)} if use_legacy_path else {}),
            }
        ),
        text=True,
        capture_output=True,
        check=True,
        timeout=30,
    )
    consumed = json.loads(result.stdout)["rounds"]
    assert len(consumed) == 3
    expected_defaults = [defaults(legacy) if use_legacy_path else None, defaults(selected), None]
    for item, expected in zip(consumed, expected_defaults, strict=True):
        assert item["poll"]["projects"]["accepted"] == 1
        project = item["project"]
        assert project["project"]["side_quest_defaults"] == expected
        config_project = next(
            row["project"]
            for row in item["configuration"]["projects"]
            if row["project"]["id"] == project_id
        )
        assert config_project["side_quest_defaults"] == expected
        composer = project["composer"]["side_quest"]
        if expected is None:
            assert composer["available"] is False
            assert composer["source"]["local_path"] == ""
        else:
            assert composer["available"] is True
            assert composer["host"] == HOST
            assert composer["source"]["local_path"] == expected["local_path"]
