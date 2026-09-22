# Forge UX cleanup release

`forge/ux-cleanup` starts at upstream `dev` commit
`98ffad8755f7e9ed742896cc29580a861d8deb7b` (PR #1016). That import already
contains the session UX, previews, bounded history, project assignment, Guild
discovery, unread state, runtime versions, and Claude tmux improvements.

The remaining changes carry forward source privacy cleanup, immutable installed
build identity, and the local Forge version route. Upstream migration numbering,
authentication, history service injection, and Docker architecture are retained.
The previous standalone `containers/forge` host is deliberately not carried over.

## Published artifacts

The `Public Forge images` workflow builds the upstream `containers/niuu` and
`containers/skuld` recipes on native AMD64 and ARM64 runners. It checks the
installed identity, boots an isolated Forge/Guild with disposable PostgreSQL,
and scans every image layer before pushing an architecture. Both images and
architectures must pass before release aliases are promoted:

- `ghcr.io/xteo/niuu:forge-ux-cleanup`
- `ghcr.io/xteo/skuld:forge-ux-cleanup`

Immutable tags use `forge-ux-cleanup-sha-<full-source-commit>` on both images.
Use the same immutable tag for the pair; a branch alias is for evaluation.
Publication is proven by a successful workflow and anonymous manifest reads,
not merely by this document. No node is automatically upgraded by publication.

## Install and update

Use the upstream [Docker-mode workflow](../site/operations/docker-mode.md).
For a new host, download `scripts/install.sh` from the chosen immutable source
commit and run it with `NIUU_REGISTRY=ghcr.io/xteo` and
`NIUU_IMAGE_TAG=forge-ux-cleanup-sha-<that-commit>`. The installer creates a CLI
wrapper that runs from the platform image, so no source checkout is needed.
It pins the matching Skuld image as well. Alternatively configure
`docker.image` and `docker.skuld_image` explicitly in the existing Docker config.

For an existing Docker-mode node, back up its data and `secrets.env`, select the
new immutable pair, and rerun the installer to update its wrapper. Pre-pull both
images, then run `niuu up` in an agreed maintenance window. Retain the prior
digests and database backup; downgrading an image alone cannot undo migrations.
Do not run `niuu down` or delete volumes as an upgrade procedure.

Source/Supervisor installations need a separate migration of their databases,
workspace paths, credentials, public origin, and Guild identity. This release
does not convert them automatically. Existing Skuld sessions do not change
image when the platform changes: refresh them explicitly when safe. Do not
promise active-session survival across a host migration without verifying it.

## Privacy boundaries

The release scans the current source snapshot and final image layers. Known
inert scanner findings are allowlisted by exact fingerprint, never by directory
or detector. Current examples use generic addresses, test keys are generated,
and identifiable screenshots and captured gateway contents are removed.
No live config, account credentials, or private mesh inventory belongs in an
image. Historical public commits remain unchanged; the earlier history audit
and any history purge are separate from this release.
