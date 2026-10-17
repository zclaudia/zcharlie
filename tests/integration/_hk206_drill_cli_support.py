"""Shared subprocess harness for the HK2-06 drill CLI integration tests.

The CLI must behave the same when the caller's environment is hostile: a
``.env`` in the working directory, ``DATABASE_URL`` / ``DATA_DIR`` pointing at
sentinel paths and ``ONLINE_REFRESH_ENABLED=true`` must all be ignored (the
tool builds its own isolated environment under the evidence root).
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
CLI_PATH = REPO_ROOT / "tools" / "hk206_drills.py"


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class DrillCliRun:
    """One completed drill CLI execution plus its hostile-environment probes."""

    def __init__(self, root: Path, completed: subprocess.CompletedProcess[str]) -> None:
        self.root = root
        self.completed = completed
        self.evidence_root = root / "evidence"

    @property
    def summary(self) -> dict[str, Any]:
        return json.loads(
            (self.evidence_root / "drill-summary.json").read_text(encoding="utf-8")
        )

    def evidence_paths(self) -> list[Path]:
        summary = self.summary
        paths = []
        for entry in summary["drills"].values():
            paths.append(self.evidence_root / str(entry["evidence_path"]))
            for key, value in entry.items():
                if key.endswith("_evidence_path"):
                    paths.append(self.evidence_root / str(value))
        return paths


def run_drill_cli(
    directory: Path, source: str, *drills: str
) -> DrillCliRun:
    directory.mkdir(parents=True, exist_ok=True)
    evidence_root = directory / "evidence"
    dotenv = directory / ".env"
    dotenv.write_text(
        "API_KEY=dotenv-must-not-be-read\n"
        "ONLINE_REFRESH_ENABLED=true\n"
        f"DATABASE_URL=sqlite+aiosqlite:///{directory / 'dotenv-database.sqlite'}\n",
        encoding="utf-8",
    )
    database_sentinel = directory / "database-sentinel.sqlite"
    database_sentinel.write_bytes(b"database sentinel: must remain byte-identical\n")
    data_sentinel = directory / "data-sentinel"
    data_sentinel.mkdir()
    (data_sentinel / "keep.txt").write_text("data sentinel\n", encoding="utf-8")
    command = [sys.executable, str(CLI_PATH), "--source", source,
               "--evidence-root", str(evidence_root)]
    command.extend(f"--drill={name}" for name in drills)
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "DATABASE_URL": f"sqlite+aiosqlite:///{database_sentinel}",
        "DATA_DIR": str(data_sentinel),
        "API_KEY": "integration-fake-api-key",
        "LLM_TIER3_API_KEY": "integration-fake-llm-key",
        "ONLINE_REFRESH_ENABLED": "true",
    }
    completed = subprocess.run(
        command,
        cwd=directory,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )
    run = DrillCliRun(directory, completed)
    run.dotenv_sha256 = _sha256(dotenv)
    run.database_sentinel_sha256 = _sha256(database_sentinel)
    run.data_sentinel = {
        path.relative_to(data_sentinel).as_posix(): _sha256(path)
        for path in data_sentinel.rglob("*")
        if path.is_file()
    }
    return run


def sentinels_unchanged(run: DrillCliRun) -> bool:
    directory = run.root
    return (
        _sha256(directory / ".env") == run.dotenv_sha256
        and _sha256(directory / "database-sentinel.sqlite") == run.database_sentinel_sha256
        and {
            path.relative_to(directory / "data-sentinel").as_posix(): _sha256(path)
            for path in (directory / "data-sentinel").rglob("*")
            if path.is_file()
        }
        == run.data_sentinel
    )
