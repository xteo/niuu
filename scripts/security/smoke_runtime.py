"""Boot the image with disposable PostgreSQL and verify real Forge/Guild HTTP routes.

All resources have a unique test prefix. This never connects to host databases or
existing Guild nodes and never starts a native coding session.
"""

import argparse
import json
import secrets
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from pathlib import Path

BOOTSTRAP = """
import asyncio, asyncpg, os
from niuu.service_databases import bootstrap_sql_for_service
from cli.resources import migration_dir, ordered_migration_files
from niuu.adapters.postgres_schema import apply_startup_migrations
async def main():
    options = dict(host='postgres', user='postgres', password=os.environ['DATABASE__PASSWORD'])
    conn = await asyncpg.connect(**options, database='postgres')
    for name in ('volundr', 'guild', 'niuu_shared', 'bifrost'):
        await conn.execute('CREATE DATABASE ' + name)
    await conn.close()
    for name, service in (('guild', 'guild'), ('niuu_shared', 'niuu-shared')):
        conn = await asyncpg.connect(**options, database=name)
        if name == 'niuu_shared':
            await apply_startup_migrations(conn, ordered_migration_files(migration_dir()))
        for statement in bootstrap_sql_for_service(service):
            await conn.execute(statement)
        await conn.close()
asyncio.run(main())
"""


def docker(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], text=True, capture_output=True, check=check)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("revision")
    args = parser.parse_args()
    prefix = "forge-image-test-" + uuid.uuid4().hex[:10]
    db, api = prefix + "-db", prefix + "-api"
    password = secrets.token_hex(24)
    docker("network", "create", prefix)
    try:
        with tempfile.TemporaryDirectory(prefix=prefix) as directory:
            work = Path(directory)
            db_env = work / "db.env"
            db_env.write_text("POSTGRES_PASSWORD=" + password + "\n")
            client_env = work / "client.env"
            client_env.write_text("DATABASE__PASSWORD=" + password + "\n")
            docker(
                "run",
                "-d",
                "--name",
                db,
                "--network",
                prefix,
                "--network-alias",
                "postgres",
                "--env-file",
                str(db_env),
                "pgvector/pgvector:pg17",
            )
            deadline = time.monotonic() + 90
            while docker("exec", db, "pg_isready", "-U", "postgres", check=False).returncode:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Disposable PostgreSQL did not become ready")
                time.sleep(1)
            docker(
                "run",
                "--rm",
                "--network",
                prefix,
                "--env-file",
                str(client_env),
                "--entrypoint",
                "python",
                args.image,
                "-c",
                BOOTSTRAP,
            )
            config = {
                "mode": "mini",
                "server": {"host": "0.0.0.0", "port": 8080},
                "database": {"mode": "external"},
                "plugins": {
                    "enabled": {
                        name: False
                        for name in (
                            "ting",
                            "mimir",
                            "ravn",
                            "observatory",
                            "personas",
                            "audit",
                        )
                    }
                },
                "pod_manager": {
                    "adapter": "volundr.adapters.outbound.local_process.LocalProcessPodManager",
                    "workspaces_dir": "/tmp/niuu/workspaces",
                    "state_file": "/tmp/niuu/forge-state.json",
                },
                "local_mounts": {"enabled": True, "mini_mode": True},
            }
            config_path = work / "config.json"
            config_path.write_text(json.dumps(config))  # JSON is valid YAML.
            # The parent directory remains 0700 on the host. The single mounted
            # test file must be readable by the image's non-root UID even when
            # the invoking audit shell has umask 077.
            config_path.chmod(0o644)
            docker(
                "run",
                "-d",
                "--name",
                api,
                "--network",
                prefix,
                "-p",
                "127.0.0.1:0:8080",
                "--mount",
                f"type=bind,src={config_path},dst=/etc/niuu/config.yaml,readonly",
                "--env-file",
                str(client_env),
                "-e",
                "NIUU_DATABASE_MODE=external",
                "-e",
                'RESIDENT_RUNTIMES={"controllers":[],"session_controllers":[],"profiles":[]}',
                "-e",
                "DATABASE__HOST=postgres",
                "-e",
                "DATABASE__USER=postgres",
                "-e",
                "DATABASE__NAME=volundr",
                "-e",
                "INTEGRATIONS__DATABASE_NAME=niuu_shared",
                "-e",
                "HOME=/tmp/niuu",
                "--entrypoint",
                "/opt/venv/bin/niuu",
                args.image,
                "platform",
                "up",
                "--skip-preflight",
                "--host-profile",
                "full",
            )
            info = json.loads(docker("inspect", api).stdout)[0]
            port = info["NetworkSettings"]["Ports"]["8080/tcp"][0]["HostPort"]
            base = f"http://127.0.0.1:{port}"
            deadline = time.monotonic() + 90
            while True:
                try:
                    with urllib.request.urlopen(base + "/health", timeout=3) as response:
                        health = json.load(response)
                    break
                except (OSError, urllib.error.URLError):
                    state = json.loads(docker("inspect", api).stdout)[0]["State"]
                    if not state["Running"]:
                        raise RuntimeError("Forge image exited before becoming healthy") from None
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Forge image did not become healthy") from None
                    time.sleep(1)
            assert health["revision"] == args.revision, health
            assert health["failed_plugins"] == [], health
            for path in (
                "/api/v1/forge/version",
                "/api/v1/forge/sessions",
                "/api/v1/niuu/instances",
                "/api/v1/credentials/user",
                "/config.json",
                "/volundr/",
            ):
                with urllib.request.urlopen(base + path, timeout=10) as response:
                    body = response.read()
                    assert response.status == 200 and body, path
                print(f"Passed {path}")
            print("Isolated Forge/Guild/credentials/UI runtime smoke passed")
    except Exception:
        # Test logs use synthetic configuration only; retain privately on failure.
        logs = Path(tempfile.mkdtemp(prefix=prefix + "-logs-"))
        for name in (db, api):
            result = docker("logs", name, check=False)
            content = (result.stdout + result.stderr).replace(password, "[test credential]")
            (logs / (name + ".log")).write_text(content)
            # This isolated test has no operator config or provider credentials.
            # Keep diagnostics useful on disposable CI runners as well.
            print(f"{name} startup diagnostics:\n" + "\n".join(content.splitlines()[-80:]))
        print(f"Isolated smoke logs: {logs}")
        raise
    finally:
        docker("rm", "-f", api, db, check=False)
        docker("network", "rm", prefix, check=False)


if __name__ == "__main__":
    main()
