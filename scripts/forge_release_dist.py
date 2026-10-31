#!/usr/bin/env python3
"""Give a Forge release its web app, and refuse a release without one.

A Forge API serves its web app from its own release, ``web-next/apps/niuu/dist``.
Promotion ten (2026-10-31) was staged with that folder empty, because web-next had
not changed and the build was skipped; its cutover served ``/`` as 404 and rolled
back. So:

``place RELEASE --previous PREVIOUS``
    Leaves a built dist alone. Otherwise, when web-next is unchanged between the
    two releases' revisions, copies the previous release's dist. When it changed,
    fails: build it (``pnpm build`` in web-next).
``check RELEASE``
    Fails unless the dist has ``index.html`` and a non-empty ``assets`` folder. Run
    it at the end of staging and again in the pre-GO preflight.

Exit 0 on success, 1 with the reason on stderr otherwise.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

DIST = Path("web-next/apps/niuu/dist")


class DistError(RuntimeError):
    """The release cannot serve its web app."""


def problem(release: Path) -> str | None:
    """Why ``release`` cannot serve its web app, or None when it can."""
    dist = release / DIST
    if not (dist / "index.html").is_file():
        return f"{dist} has no index.html"
    assets = dist / "assets"
    if not assets.is_dir() or not any(assets.iterdir()):
        return f"{assets} is missing or empty"
    return None


def check(release: Path) -> None:
    reason = problem(release)
    if reason is not None:
        raise DistError(f"release {release.name} has no web app: {reason}")


def _revision(release: Path) -> str:
    return subprocess.run(
        ["git", "-C", str(release), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def web_changed(release: Path, previous: Path) -> bool:
    """Whether web-next differs between the two releases' commits."""
    diff = subprocess.run(
        [
            "git",
            "-C",
            str(release),
            "diff",
            "--quiet",
            _revision(previous),
            "HEAD",
            "--",
            "web-next",
        ],
        capture_output=True,
        text=True,
    )
    if diff.returncode not in (0, 1):
        raise DistError(f"cannot compare web-next with {previous.name}: {diff.stderr.strip()}")
    return diff.returncode == 1


def place(release: Path, previous: Path) -> str:
    if problem(release) is None:
        return f"{release.name}: built dist kept"
    if web_changed(release, previous):
        raise DistError(
            f"web-next changed since {previous.name}: build it in {release / 'web-next'} "
            "(pnpm install --frozen-lockfile && pnpm build)"
        )
    check(previous)
    dist = release / DIST
    if dist.exists():
        shutil.rmtree(dist)
    shutil.copytree(previous / DIST, dist, symlinks=True)
    check(release)
    return f"{release.name}: web-next unchanged, dist copied from {previous.name}"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = parser.add_subparsers(dest="mode", required=True)
    placing = sub.add_parser("place", help="give the release a dist")
    placing.add_argument("release", type=Path)
    placing.add_argument("--previous", type=Path, required=True)
    checking = sub.add_parser("check", help="fail unless the release has a dist")
    checking.add_argument("release", type=Path)
    args = parser.parse_args(argv)
    try:
        if args.mode == "place":
            print(place(args.release, args.previous))
        else:
            check(args.release)
            print(f"{args.release.name}: web app present")
    except (DistError, subprocess.CalledProcessError) as exc:
        print(f"forge_release_dist: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
