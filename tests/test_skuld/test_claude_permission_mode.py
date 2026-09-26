"""A Forge node chooses how unattended Claude sessions run (``claude_permission_mode``).

Thor and DGX Spark run YOLO (``bypassPermissions``); other nodes set ``auto`` once in
their node config file instead of on every session request.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from skuld.broker import Broker
from skuld.config import SkuldSettings
from skuld.transports.persistent_subprocess import PersistentSubprocessTransport
from skuld.transports.remote_control import RemoteControlTransport
from skuld.transports.tmux_interactive import TmuxInteractiveTransport

TMUX = "skuld.transports.tmux_interactive.TmuxInteractiveTransport"


@pytest.fixture(autouse=True)
def _no_yaml_config(monkeypatch):
    """Real node config files on the test host must not leak into these tests."""
    monkeypatch.setitem(SkuldSettings.model_config, "yaml_file", [])
    monkeypatch.delenv("SKULD__CLAUDE_PERMISSION_MODE", raising=False)


def _mode(argv: list[str]) -> str:
    return argv[argv.index("--permission-mode") + 1]


class TestSetting:
    def test_default_is_bypass_permissions(self) -> None:
        assert SkuldSettings().claude_permission_mode == "bypassPermissions"

    def test_node_config_file_selects_auto(self, tmp_path: Path, monkeypatch) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("claude_permission_mode: auto\n")
        monkeypatch.setitem(SkuldSettings.model_config, "yaml_file", [config])

        assert SkuldSettings().claude_permission_mode == "auto"

    def test_env_overrides_node_config_file(self, tmp_path: Path, monkeypatch) -> None:
        config = tmp_path / "config.yaml"
        config.write_text("claude_permission_mode: auto\n")
        monkeypatch.setitem(SkuldSettings.model_config, "yaml_file", [config])
        monkeypatch.setenv("SKULD__CLAUDE_PERMISSION_MODE", "bypassPermissions")

        assert SkuldSettings().claude_permission_mode == "bypassPermissions"

    @pytest.mark.parametrize("bad", ["", "yolo", "BypassPermissions", "default"])
    def test_rejects_modes_claude_does_not_accept(self, bad: str) -> None:
        with pytest.raises(ValidationError, match="claude_permission_mode"):
            SkuldSettings(claude_permission_mode=bad)


class TestTransports:
    def test_tmux_passes_the_node_mode(self, tmp_path: Path) -> None:
        transport = TmuxInteractiveTransport(
            str(tmp_path), skip_permissions=True, claude_permission_mode="auto"
        )

        assert _mode(transport._interactive_argv()) == "auto"

    def test_tmux_defaults_to_bypass_permissions(self, tmp_path: Path) -> None:
        transport = TmuxInteractiveTransport(str(tmp_path), skip_permissions=True)

        assert _mode(transport._interactive_argv()) == "bypassPermissions"

    def test_tmux_without_skip_permissions_passes_no_mode(self, tmp_path: Path) -> None:
        transport = TmuxInteractiveTransport(
            str(tmp_path), skip_permissions=False, claude_permission_mode="auto"
        )

        assert "--permission-mode" not in transport._interactive_argv()

    def test_persistent_subprocess_passes_the_node_mode(self) -> None:
        transport = PersistentSubprocessTransport(
            "/tmp", skip_permissions=True, claude_permission_mode="auto"
        )

        assert _mode(transport._build_command()) == "auto"

    def test_remote_control_follows_the_node_mode(self) -> None:
        transport = RemoteControlTransport(
            "/tmp", skip_permissions=True, claude_permission_mode="auto"
        )

        assert _mode(transport._build_command()) == "auto"

    def test_remote_control_override_still_wins(self) -> None:
        transport = RemoteControlTransport(
            "/tmp",
            skip_permissions=True,
            claude_permission_mode="auto",
            remote_control_permission_mode="plan",
        )

        assert _mode(transport._build_command()) == "plan"


class TestBrokerComposition:
    def test_broker_hands_the_node_mode_to_the_tmux_transport(self, tmp_path: Path) -> None:
        (tmp_path / "ws").mkdir()
        settings = SkuldSettings(
            session={"id": "s-1", "workspace_dir": str(tmp_path / "ws"), "model": "m"},
            transport_adapter=TMUX,
            skip_permissions=True,
            claude_permission_mode="auto",
            port=18765,
            host="127.0.0.1",
            forge_mcp={"runtime_dir": str(tmp_path / "rt")},
        )

        transport = Broker(settings=settings)._create_transport()

        assert isinstance(transport, TmuxInteractiveTransport)
        assert _mode(transport._interactive_argv()) == "auto"
