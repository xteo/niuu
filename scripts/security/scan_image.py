"""Scan every image layer offline; print only finding metadata, never matched values."""

import argparse
import hashlib
import json
import subprocess
import tempfile
from pathlib import Path


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image")
    parser.add_argument("--scanner", default="trufflehog")
    parser.add_argument(
        "--baseline", type=Path, default=Path("scripts/security/image-scan-reviewed.json")
    )
    args = parser.parse_args()
    approved = json.loads(args.baseline.read_text())
    known = {(x["detector"], x["path"], x["sha256"]) for x in approved}
    with tempfile.TemporaryDirectory(prefix="forge-image-scan-") as private:
        report = Path(private) / "findings.jsonl"
        log = Path(private) / "scanner.log"
        with report.open("w") as stdout, log.open("w") as stderr:
            result = subprocess.run(
                [
                    args.scanner,
                    "docker",
                    "--image",
                    f"docker://{args.image}",
                    "--json",
                    "--no-update",
                    "--no-verification",
                    "--no-ignore-tag",
                    "--fail-on-scan-errors",
                    "--concurrency=4",
                ],
                stdout=stdout,
                stderr=stderr,
            )
        if result.returncode:
            raise RuntimeError(
                f"Image scanner failed with exit {result.returncode}; no publication"
            )
        findings = [json.loads(line) for line in report.read_text().splitlines()]
        unknown = []
        for finding in findings:
            metadata = finding["SourceMetadata"]["Data"]["Docker"]
            key = (
                finding["DetectorName"],
                metadata["file"],
                hashlib.sha256(finding["Raw"].encode()).hexdigest(),
            )
            if key not in known:
                unknown.append(key)
        for detector, path, fingerprint in sorted(set(unknown)):
            print(json.dumps({"detector": detector, "path": path, "sha256": fingerprint}))
        if unknown:
            raise SystemExit(f"{len(unknown)} image findings require review; no publication")
        print(f"Image layer scan passed ({len(findings)} exact reviewed example occurrences)")


if __name__ == "__main__":
    main()
