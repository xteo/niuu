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
