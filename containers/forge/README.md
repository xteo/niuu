# Public Forge image

`ghcr.io/xteo/niuu:forge-ux-improvement` packages the combined Forge/Guild HTTP
host, Skuld Python code, migrations and the Niuu web UI from the Forge branch.
It serves port **8080**. The existing shared-services image and its tags remain
available separately. Publishing this image does not update any running node.

The image runs as UID/GID **65532**. Supply configuration and persistent state at
runtime; source checkouts, node addresses, provider credentials and database data
are not part of the image. Keep the API on your private network or behind your
configured authenticated proxy. Public image visibility does not publish an API.

## Build and publish

The `Public Forge image` workflow builds native Linux amd64 and arm64 images,
checks the source and packaged contents, and promotes the branch tag only after
both architectures pass. A full commit tag is retained for rollback:

```text
ghcr.io/xteo/niuu:forge-ux-improvement-sha-<full-source-commit>
```

Trigger a publication with a commit on `forge/ux-improvement` whose message
contains `[publish-forge]`, or push a `forge-image-*` milestone tag. Builds use only
tracked source (`git archive` locally, clean checkout in CI). The Docker context
also has a deny-by-default allowlist. CI uses `GITHUB_TOKEN` with `packages:write`;
public pulls need no registry login. Prefer the reported **manifest digest** when
installing a release.

## Runtime configuration

Mount your node's configuration at `/etc/niuu/config.yaml` and a writable state
directory at `/data`. Use the existing typed settings and adapter configuration:

- `database.mode: external`: the image does not manage PostgreSQL or Docker.
- `database.host`, `database.port`, `database.user`, `database.password`: database
  connection settings. Each enabled service uses its own logical database;
  `NIUU_DATABASE_NAME_<SERVICE>` selects existing names when needed.
- `plugins.enabled`: select the API plugins for this node. `guild` and `volundr`
  must remain enabled. `plugins.extra` uses the existing adapter registry.
- `server.external_host`: the reachable origin of the node, including HTTPS when
  applicable. Configure it for the actual reverse proxy/Tailscale endpoint.
- `pod_manager`: explicitly select and configure the session runtime backend.
- Identity, credential-store, storage and Guild instance settings remain external.

Provision the logical databases and shared-service schema before starting this
host, using the same migration/bootstrap process as the existing deployment.
Forge applies its packaged, checksummed startup migrations. Test migrations and
back up data before changing a live node. Do not run a second reconciler against
a live node's database merely to test an image.

For an already configured node, the container launch shape is:

```sh
docker run -d --name forge \
  --restart unless-stopped \
  --publish 127.0.0.1:8080:8080 \
  --mount type=bind,src=/etc/niuu/config.yaml,dst=/etc/niuu/config.yaml,readonly \
  --mount type=bind,src=/var/lib/niuu,dst=/data \
  ghcr.io/xteo/niuu@sha256:<verified-manifest-digest>
```

Connect the container to the network on which its configured database and runtime
are reachable. `localhost` inside the container is the container, not the host.
The writable state directory must be accessible to UID/GID 65532 (or your chosen
explicit container user). Provider secrets should use your configured runtime
secret store or protected mounts; never bake them into a derived public image.

`GET /health` returns the immutable source commit, source hash and release name;
`GET /api/v1/forge/version` reports the same commit. The packaged web UI is at
`/volundr/`. A missing required plugin or damaged build manifest fails explicitly;
failed plugin startup makes health return 503.

## Session lifecycle boundary

This is a control-plane artifact, not an automatic migration of host processes.
The current local-process adapter launches Skuld as a child process. Native CLI
executables and workspace mounts must be provided for that adapter; this API
image does not install Claude/Codex CLI tools. Use a configured independent
runtime backend, or qualify a separate runtime image/installation before moving
an existing node. Replacing an API container that owns child processes can stop
those sessions. The independent Docker session controller and safe fleet updater
remain proposed in [the fleet design](../../docs/forge/fleet-container-deployment-design.md).
