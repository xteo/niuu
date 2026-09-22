"""Image identities survive wheel installation and cannot become a Git guess."""

import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from niuu import build_identity, build_info, packaged_build


def test_manifest_identifies_wheel_without_source_or_git(tmp_path, monkeypatch):
    source = tmp_path / "src/niuu"
    source.mkdir(parents=True)
    (source / "example.py").write_text("value = 1\n")
    script = Path(__file__).resolve().parents[2] / "scripts/build_manifest.py"
    sha = "a" * 40
    subprocess.run(
        [
            sys.executable,
            str(script),
            "--revision",
            sha,
            "--ref",
            "forge/test",
            "--version",
            "forge-test",
        ],
        cwd=tmp_path,
        check=True,
    )
    # Install the manifest in a wheel-shaped tree that has neither .git nor src.
    installed = tmp_path / "site-packages/niuu"
    installed.mkdir(parents=True)
    (source / "_build.json").rename(installed / "_build.json")
    monkeypatch.setattr(packaged_build, "__file__", str(installed / "packaged_build.py"))
    monkeypatch.setenv("NIUU_BUILD_REVISION", "wrong-runtime-override")
    monkeypatch.setenv("NIUU_BUILD_SHA", "wrong-runtime-override")
    expected_hash = hashlib.sha256(b"src/niuu/example.py\0value = 1\n\0").hexdigest()
    identity = build_identity.build_identity()
    assert identity == {
        "revision": sha,
        "build": "forge-test",
        "source_sha256": expected_hash,
        "dirty": False,
    }
    build_info.build_info.cache_clear()
    try:
        info = build_info.build_info()
        assert info["git_sha_full"] == sha
        assert info["git_branch"] == "forge/test"
        assert info["git_dirty"] is False
    finally:
        build_info.build_info.cache_clear()


def test_invalid_manifest_fails_instead_of_reporting_development(tmp_path, monkeypatch):
    monkeypatch.setattr(packaged_build, "__file__", str(tmp_path / "packaged_build.py"))
    (tmp_path / "_build.json").write_text(json.dumps({"revision": "bad"}))
    with pytest.raises(ValidationError):
        build_identity.build_identity()
