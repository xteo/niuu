"""NotificationsConfig defaults, validation and composition."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from pydantic import ValidationError

from volundr.config import NotificationDispatcherConfig, NotificationsConfig, Settings
from volundr.domain.models import Session


def test_defaults_are_safe():
    config = NotificationsConfig()
    assert config.enabled and config.reply_ready.enabled
    assert (config.default_page_size, config.max_page_size) == (50, 200)
    assert config.sinks == []  # no external delivery by default
    assert config.dispatcher.max_attempts >= 1
    assert Settings().notifications == config


@pytest.mark.parametrize(
    "values",
    [
        {"default_page_size": 300},
        {"reply_ready": {"title_chars": 201}},
        {"reply_ready": {"body_chars": 4001}},
        {"sinks": [{"adapter": "a.B"}]},
        {"sinks": [{"name": "ops"}]},
        {"sinks": [{"name": "ops", "adapter": "nodots"}]},
        {"sinks": [{"name": "Ops Hook", "adapter": "a.B"}]},
        {"sinks": [{"name": "integration", "adapter": "a.B"}]},
        {"sinks": [{"name": "ops", "adapter": "a.B"}, {"name": "ops", "adapter": "c.D"}]},
    ],
)
def test_invalid_configuration_fails_loudly(values):
    with pytest.raises(ValidationError):
        NotificationsConfig(**values)


def test_backoff_must_be_ordered():
    with pytest.raises(ValidationError, match="backoff"):
        NotificationDispatcherConfig(backoff_base_seconds=10, backoff_max_seconds=5)


def test_composition_builds_service_from_config():
    from volundr.main import _create_notification_service

    settings = Settings(
        notifications={
            "sinks": [
                {"name": "ops", "label": "Ops", "adapter": "a.B", "url": "https://x"},
                {"name": "page", "adapter": "a.C"},
            ]
        }
    )
    service = _create_notification_service(
        settings, MagicMock(), broadcaster=MagicMock(), integration_repository=None
    )
    assert [(s.name, s.label) for s in service._sinks] == [("ops", "Ops"), ("page", "page")]
    assert service._engine_resolver(Session(name="s", session_definition="skuldCodex")) == "codex"
    assert service._reply_title_chars == settings.notifications.reply_ready.title_chars

    disabled = Settings(notifications={"enabled": False})
    assert (
        _create_notification_service(
            disabled, MagicMock(), broadcaster=MagicMock(), integration_repository=None
        )
        is None
    )
