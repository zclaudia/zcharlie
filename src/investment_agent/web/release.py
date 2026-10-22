"""Backend-delivered UI release contract, independent of APK releases."""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import TypedDict

from investment_agent.web.static_assets import static_version

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


class ReleaseManifest(TypedDict):
    schema_version: int
    release_id: str
    supported_bridge_versions: list[int]


def web_release() -> ReleaseManifest:
    # Include templates as well as assets: a template-only local deployment must
    # also refresh. Production's APP_VERSION covers Python/API-only deployments.
    source = "\0".join(
        (os.environ.get("APP_VERSION") or "dev", static_version(), static_version(TEMPLATES_DIR))
    )
    return {
        "schema_version": 1,
        "release_id": hashlib.sha256(source.encode()).hexdigest()[:24],
        # v0 = APKs predating capability discovery; keep the scheme/header
        # transport available. v1 is additive, never rename its commands/fields.
        "supported_bridge_versions": [0, 1],
    }
