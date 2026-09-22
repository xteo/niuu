"""The local Forge version route must precede Guild aggregation."""


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
