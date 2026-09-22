"""Install pinned scanners from their official, checksum-verified release archives."""

import argparse
import hashlib
import platform
import tarfile
import urllib.request
from pathlib import Path

RELEASES = {
    "gitleaks": ("gitleaks/gitleaks", "8.30.1"),
    "trufflehog": ("trufflesecurity/trufflehog", "3.97.5"),
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    args = parser.parse_args()
    arch = {"aarch64": "arm64", "arm64": "arm64", "x86_64": "amd64"}[platform.machine()]
    args.destination.mkdir(parents=True, exist_ok=True)
    for name, (repo, version) in RELEASES.items():
        # Gitleaks calls its amd64 release x64.
        release_arch = "x64" if name == "gitleaks" and arch == "amd64" else arch
        asset = f"{name}_{version}_linux_{release_arch}.tar.gz"
        base = f"https://github.com/{repo}/releases/download/v{version}/"
        checksums = urllib.request.urlopen(base + f"{name}_{version}_checksums.txt").read()
        expected = next(
            line.split()[0].decode()
            for line in checksums.splitlines()
            if line.split()[-1].decode() == asset
        )
        data = urllib.request.urlopen(base + asset).read()
        if hashlib.sha256(data).hexdigest() != expected:
            raise RuntimeError(f"Checksum mismatch for {asset}")
        archive = args.destination / asset
        archive.write_bytes(data)
        with tarfile.open(archive) as tar:
            member = tar.getmember(name)
            tar.extract(member, path=args.destination, filter="data")
        archive.unlink()
        print(f"Installed {name} {version} ({arch}); release checksum verified")


if __name__ == "__main__":
    main()
