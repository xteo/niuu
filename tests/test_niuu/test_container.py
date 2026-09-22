"""The container composes the real host without managing host infrastructure."""

from unittest.mock import MagicMock

import pytest

from cli.config import CLISettings
from niuu import container


def test_container_passes_config_and_public_origin_to_root(monkeypatch):
    settings = CLISettings(
        database={"mode": "external"},
        server={"host": "0.0.0.0", "port": 8080, "external_host": "https://forge.example.net"},
        plugins={"enabled": {"ting": False}},
    )
    registry = MagicMock(plugins={"guild": object(), "volundr": object()})
    root = MagicMock()
    monkeypatch.setattr(container, "CLISettings", lambda: settings)
    monkeypatch.setattr(container, "PluginRegistry", lambda: registry)
    monkeypatch.setattr(container, "build_root_app", root)
    assert container.create_app() is root.return_value
    registry.apply_config.assert_called_once_with({"ting": False})
    root.assert_called_once_with(
        registry=registry,
        host="0.0.0.0",
        port=8080,
        public_host="https://forge.example.net",
    )


def test_container_rejects_embedded_database_mode(monkeypatch):
    monkeypatch.setattr(
        container, "CLISettings", lambda: CLISettings(database={"mode": "embedded"})
    )
    with pytest.raises(ValueError, match="database.mode=external"):
        container.create_app()


def test_container_rejects_missing_required_plugins(monkeypatch):
    monkeypatch.setattr(
        container, "CLISettings", lambda: CLISettings(database={"mode": "external"})
    )
    monkeypatch.setattr(container, "PluginRegistry", lambda: MagicMock(plugins={"guild": object()}))
    with pytest.raises(ValueError, match="volundr"):
        container.create_app()


def test_local_version_route_takes_precedence_over_guild_aggregation(monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from cli.registry import PluginRegistry
    from guild.plugin import GuildPlugin
    from niuu.app import build_root_app
    from volundr.plugin import VolundrPlugin

    local = FastAPI()

    @local.get("/api/v1/forge/version")
    async def local_version():
        return {"service": "forge-api", "git_sha_full": "packaged-local-build"}

    monkeypatch.setenv("NIUU_NO_WEB", "true")
    monkeypatch.setattr(VolundrPlugin, "create_api_app", lambda self, **kwargs: local)
    monkeypatch.setattr(GuildPlugin, "create_api_app", lambda self, **kwargs: FastAPI())
    registry = PluginRegistry()
    registry.register(VolundrPlugin())
    registry.register(GuildPlugin())
    app = build_root_app(registry=registry, host="127.0.0.1", port=8080)
    with TestClient(app) as client:
        response = client.get("/api/v1/forge/version")
    assert response.status_code == 200
    assert response.json()["git_sha_full"] == "packaged-local-build"
