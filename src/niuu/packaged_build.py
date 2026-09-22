"""Immutable identity included by the public image build, without a Git checkout."""

import json
from pathlib import Path

from pydantic import BaseModel, Field


class PackagedBuild(BaseModel):
    revision: str = Field(pattern=r"^[0-9a-f]{40}$")
    ref: str
    version: str
    source_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


def packaged_build() -> PackagedBuild | None:
    path = Path(__file__).with_name("_build.json")
    if not path.exists():
        return None
    # A damaged release manifest must not silently identify itself as a dev build.
    return PackagedBuild.model_validate(json.loads(path.read_text()))
