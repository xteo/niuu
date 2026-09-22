"""Write source identity before wheel installation; takes only public build metadata."""

import argparse
import hashlib
import json
import re
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--ref", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args()
    if not re.fullmatch(r"[0-9a-f]{40}", args.revision):
        parser.error("revision must be the full source commit SHA")
    root = Path.cwd()
    digest = hashlib.sha256()
    for path in sorted((root / "src").rglob("*.py")):
        digest.update(str(path.relative_to(root)).encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    (root / "src/niuu/_build.json").write_text(
        json.dumps(
            {
                "revision": args.revision,
                "ref": args.ref,
                "version": args.version,
                "source_sha256": digest.hexdigest(),
            },
            sort_keys=True,
        )
        + "\n"
    )


if __name__ == "__main__":
    main()
