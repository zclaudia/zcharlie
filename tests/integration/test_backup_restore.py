"""Backup and restore drill (work package E3 / MVP-405).

Exercises the runbook in docs/product/backup-and-recovery.md inside
temporary directories: build a migrated database with a watchlist item,
a research run (snapshot + change set + artifact file), back it up with
tools/backup_research.py, verify the manifest, detect tampering, then
restore into a fresh location and confirm the migration version, the
watchlist entry, the run, its snapshot and diff, and the artifact are all
readable and aligned by run_id.
"""

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def _ia_binary() -> str:
    candidate = Path(sys.executable).parent / "ia"
    assert candidate.is_file(), "ia console script must be installed"
    return str(candidate)


def _run(cmd: list[str], env: dict[str, str]) -> None:
    result = subprocess.run(
        cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=120
    )
    assert result.returncode == 0, (result.stderr or result.stdout)[-2000:]


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@pytest.fixture()
def drill_env(tmp_path: Path) -> dict[str, str]:
    env = {**dict(__import__("os").environ)}
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{data_dir / 'app.db'}",
            "DATA_DIR": str(data_dir),
            "API_KEY": "drill-secret",
            "LLM_TIER3_API_KEY": "x",
        }
    )
    _run([_ia_binary(), "migrate", "up"], env)
    return env


def _seed_one_research_run(env: dict[str, str], artifact_dir: Path) -> int:
    """Insert watchlist + run + snapshot + diff + artifact via the repos."""

    script = f"""
import asyncio
from datetime import UTC, date, datetime
from pathlib import Path

async def main():
    from investment_agent.storage.database import init_engine, session_scope
    from investment_agent.storage.models.base import Base
    from investment_agent.storage.models import (  # noqa: F401
        analysis, market_data, ops, reference, relationship, user_domain,
    )
    from investment_agent.storage.repositories.listing import ListingRepository
    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
    from investment_agent.storage.repositories.research_snapshot import (
        ResearchSnapshotRepository,
    )
    from investment_agent.storage.repositories.watchlist import WatchlistRepository
    from investment_agent.storage.models.reference import Market

    init_engine({env["DATABASE_URL"]!r})
    async with session_scope() as session:
        session.add(Market(code="SSE", name="Shanghai", timezone="Asia/Shanghai", currency="CNY"))
        await session.flush()
        listing = await ListingRepository(session).upsert_listing(
            market_code="SSE", ticker="600519", company_name="贵州茅台"
        )
        await WatchlistRepository(session).add(
            listing.id, None, industry="白酒", provider="ifind", report_year=2025
        )
        run = await PipelineRunRepository(session).create(
            kind="ifind_research", request_json={{"ticker": "600519.SH", "industry": "白酒"}}
        )
        artifact_dir = Path({str(artifact_dir)!r})
        artifact_dir.mkdir(parents=True)
        scores_path = artifact_dir / "scores.json"
        scores_path.write_text("{{\\"investment_score\\": 70.0}}", encoding="utf-8")
        await ResearchSnapshotRepository(session).create(
            pipeline_run_id=run.id,
            symbol="600519",
            market="SH",
            company_name="贵州茅台",
            industry="白酒",
            as_of=date(2026, 10, 5),
            report_year=2025,
            canonical_schema_version="ifind-canonical.v1",
            scoring_version="v1.2",
            score_schema_version="v2.0",
            score_json={{"investment_score": 70.0, "needs_agent_review": False}},
            explanation_json=None,
            artifact_manifest={{"scores": str(scores_path)}},
        )
        await PipelineRunRepository(session).mark_success(run.id)
        print(run.id)

asyncio.run(main())
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return int(result.stdout.strip().splitlines()[-1])


def test_backup_verify_tamper_and_restore_drill(tmp_path: Path, drill_env: dict[str, str]) -> None:
    data_dir = Path(drill_env["DATA_DIR"])
    artifact_dir = data_dir / "research-runs" / "1"
    run_id = _seed_one_research_run(drill_env, artifact_dir)
    assert run_id == 1

    backup_dir = tmp_path / "backup"
    _run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "backup_research.py"),
            "--data-dir",
            str(data_dir),
            "--output",
            str(backup_dir),
        ],
        env=drill_env,
    )
    manifest = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    backed_up = {entry["path"] for entry in manifest["files"]}
    assert "app.db" in backed_up
    assert "research-runs/1/scores.json" in backed_up

    verify = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "verify_backup.py"),
            "--backup-dir",
            str(backup_dir),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert verify.returncode == 0, verify.stderr
    assert "verified" in verify.stdout

    # Tampering must be detected: flip one byte inside the database copy.
    db_copy = backup_dir / "app.db"
    original_bytes = db_copy.read_bytes()
    db_copy.write_bytes(original_bytes.replace(b"600519", b"600520", 1))
    tampered = subprocess.run(
        [
            sys.executable,
            str(REPO_ROOT / "tools" / "verify_backup.py"),
            "--backup-dir",
            str(backup_dir),
        ],
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert tampered.returncode == 1
    assert "sha256 mismatch" in tampered.stderr
    db_copy.write_bytes(original_bytes)

    # Restore into a fresh location and confirm everything is readable.
    restore_dir = tmp_path / "restored" / "data"
    restore_dir.mkdir(parents=True)
    shutil.copy2(backup_dir / "app.db", restore_dir / "app.db")
    shutil.copytree(backup_dir / "research-runs", restore_dir / "research-runs")

    restore_env = {
        **drill_env,
        "DATABASE_URL": f"sqlite+aiosqlite:///{restore_dir / 'app.db'}",
        "DATA_DIR": str(restore_dir),
    }
    current = subprocess.run(
        [_ia_binary(), "migrate", "current"],
        cwd=REPO_ROOT,
        env=restore_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert current.returncode == 0
    assert "022 (head)" in current.stdout

    check = subprocess.run(
        [
            sys.executable,
            "-c",
            f"""
import asyncio
from pathlib import Path

async def main():
    from investment_agent.storage.database import init_engine, session_scope
    from investment_agent.storage.models.analysis import (
        PipelineRun, ResearchChangeSet, ResearchSnapshot,
    )
    from investment_agent.storage.models.user_domain import WatchlistItem
    from sqlalchemy import select

    init_engine('sqlite+aiosqlite:///' + {str(restore_dir / 'app.db')!r})
    async with session_scope() as session:
        watchlist = (await session.execute(select(WatchlistItem))).scalars().all()
        assert len(watchlist) == 1 and watchlist[0].enabled is True
        run = (await session.execute(select(PipelineRun))).scalars().one()
        assert run.status == "success"
        snapshot = (await session.execute(select(ResearchSnapshot))).scalars().one()
        assert snapshot.pipeline_run_id == run.id
        assert snapshot.score_json["investment_score"] == 70.0
        change = (await session.execute(select(ResearchChangeSet))).scalars().one()
        assert change.current_snapshot_id == snapshot.id and change.baseline is True
    print("restored-ok")

asyncio.run(main())
""",
        ],
        cwd=REPO_ROOT,
        env=restore_env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert check.returncode == 0, check.stderr[-2000:]
    assert "restored-ok" in check.stdout

    restored_artifact = restore_dir / "research-runs" / "1" / "scores.json"
    assert restored_artifact.is_file()
    assert _sha256(restored_artifact) == _sha256(artifact_dir / "scores.json")
