"""Check the installed wheel/UI and both build identity APIs without a checkout."""

import argparse
import json
import subprocess

CHECK = r"""
from pathlib import Path
from niuu.build_identity import build_identity
from niuu.build_info import build_info
from cli.resources import migration_dir, web_dist_dir
import json
import hashlib
identity = build_identity()
info = build_info()
assert identity['revision'] == info['git_sha_full']
assert identity['source_sha256'] != hashlib.sha256(b'').hexdigest()
assert list(migration_dir().glob('*.up.sql'))
assert (web_dist_dir() / 'index.html').is_file()
assert not (web_dist_dir() / 'config.live.json').exists()
assert not Path('/app/.git').exists()
assert not Path('/app/config.yaml').exists()
from niuu.container import create_app
assert callable(create_app)
print(json.dumps(identity))
"""


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("revision")
    args = parser.parse_args()
    result = subprocess.check_output(
        [
            "docker",
            "run",
            "--rm",
            "--network=none",
            "--entrypoint",
            "python",
            args.image,
            "-c",
            CHECK,
        ],
        text=True,
    )
    identity = json.loads(result)
    if identity["revision"] != args.revision:
        raise RuntimeError("Packaged identity does not match the source commit")
    info = json.loads(subprocess.check_output(["docker", "inspect", args.image]))[0]
    if info["Config"]["User"] != "65532:65532":
        raise RuntimeError("Forge image must run without root privileges")
    if info["Config"]["Labels"]["org.opencontainers.image.revision"] != args.revision:
        raise RuntimeError("OCI identity does not match the packaged identity")
    print("Packaged identity, migrations, web UI and non-root configuration passed")


if __name__ == "__main__":
    main()
