"""Unified Forge/Guild ASGI host using externally managed infrastructure.

Unlike ``platform up``, this factory never starts or stops PostgreSQL, Docker,
or a host supervisor. Plugin settings and adapters use the existing config models.
"""

import os

from fastapi import FastAPI

from cli.config import CLISettings
from cli.registry import PluginRegistry
from niuu.app import _local_service_host, build_root_app


def create_app() -> FastAPI:
    settings = CLISettings()
    if settings.database.mode != "external":
        raise ValueError("The container host requires database.mode=external in NIUU_CONFIG")
    registry = PluginRegistry()
    registry.discover_entry_points()
    registry.discover_config(settings.plugins.extra)
    registry.apply_config(settings.plugins.enabled)
    missing = {"guild", "volundr"} - registry.plugins.keys()
    if missing:
        raise ValueError(f"The Forge image requires enabled plugins: {', '.join(sorted(missing))}")
    public_host = settings.server.external_host or settings.server.host
    # Match RootServer's process bootstrap: hosted plugin Settings consume these
    # aliases for node ownership, broker callbacks and public connection URLs.
    os.environ.update(
        {
            "NIUU_SERVER_HOST": settings.server.host,
            "NIUU_SERVER_PUBLIC_HOST": public_host,
            "NIUU_SERVER_PORT": str(settings.server.port),
            "VOLUNDR__URL": (
                f"http://{_local_service_host(settings.server.host)}:{settings.server.port}"
            ),
        }
    )
    return build_root_app(
        registry=registry,
        host=settings.server.host,
        port=settings.server.port,
        public_host=public_host,
    )
