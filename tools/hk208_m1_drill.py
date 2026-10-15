"""Run the isolated HK2-08 M1 backup, migration, restore and read-path drill.

Example::

    python tools/hk208_m1_drill.py --work-dir /tmp/hk208-drill \
        --manifest /path/to/manifest.json --source-root /path/to/raw \
        --source-repo /path/to/zcharlie

The work directory must not exist. All databases, copied artifacts, backups,
restores and reports are created beneath it. The supplied originals are read
only inputs. The script never loads dotenv or uses configured production paths.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import socket
import sqlite3
import subprocess
import sys
import traceback
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

SENTINEL_SOURCE = "hk208_drill_sentinel"
SENTINEL_BYTES = b"HK208 M1 legacy original document sentinel\n"
_NETWORK_ATTEMPTS = 0


class DrillError(RuntimeError):
    pass


def _safe_child(root: Path, name: str) -> Path:
    result = (root / name).resolve()
    if root.resolve() not in result.parents:
        raise DrillError(f"drill path escaped work directory: {name}")
    return result


def _run(command: list[str], *, cwd: Path, env: dict[str, str]) -> str:
    completed = subprocess.run(
        command,
        cwd=cwd,
        env=env,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        check=False,
    )
    if completed.returncode:
        raise DrillError(
            f"command failed ({completed.returncode}): {' '.join(command)}\n{completed.stdout}"
        )
    return completed.stdout.strip()


def _isolated_env(db_path: Path, data_dir: Path) -> dict[str, str]:
    env = os.environ.copy()
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
            "DATA_DIR": str(data_dir),
            "API_KEY": "hk208-isolated-drill-key",
            "LLM_TIER3_API_KEY": "offline-drill-unused",
            "PYTHONPATH": os.pathsep.join(
                item
                for item in [
                    env.get("HK208_GUARD_DIR", ""),
                    str(Path(__file__).resolve().parents[1] / "src"),
                    env.get("PYTHONPATH", ""),
                ]
                if item
            ),
        }
    )
    if env.get("HK208_GUARD_DIR"):
        env["HK208_NETWORK_GUARD_LOG"] = str(Path(env["HK208_GUARD_DIR"]) / "blocked.jsonl")
    # These variables are deliberately fixed to paths inside this new drill.
    return env


def _offline_socket_guard() -> None:
    def refuse(*_args: Any, **_kwargs: Any) -> None:
        global _NETWORK_ATTEMPTS
        _NETWORK_ATTEMPTS += 1
        raise AssertionError("HK208 M1 drill attempted network access")

    socket.socket.connect = refuse  # type: ignore[method-assign]
    socket.socket.connect_ex = refuse  # type: ignore[method-assign]
    socket.create_connection = refuse  # type: ignore[assignment]
    socket.getaddrinfo = refuse  # type: ignore[assignment]


def _install_subprocess_network_guard(work_dir: Path) -> None:
    guard_dir = work_dir / "offline-guard"
    guard_dir.mkdir(mode=0o700)
    guard_source = "\n".join(
        [
            "import os, socket",
            "def _blocked(*args, **kwargs):",
            "    path = os.environ.get('HK208_NETWORK_GUARD_LOG')",
            "    if path:",
            "        with open(path, 'a', encoding='utf-8') as stream: stream.write('blocked\\n')",
            "    raise RuntimeError('HK208 M1 subprocess network access refused')",
            "socket.socket.connect = _blocked",
            "socket.socket.connect_ex = _blocked",
            "socket.create_connection = _blocked",
            "socket.getaddrinfo = _blocked",
            "",
        ]
    )
    (guard_dir / "sitecustomize.py").write_text(guard_source, encoding="utf-8")
    os.environ["HK208_GUARD_DIR"] = str(guard_dir)
    os.environ["HK208_NETWORK_GUARD_LOG"] = str(guard_dir / "blocked.jsonl")


async def _seed_legacy(database_url: str, data_dir: Path, captured_at: datetime) -> dict[str, Any]:
    from investment_agent.materials.contracts import (
        DatasetDraft,
        DatasetKind,
        SubjectKind,
        content_hash,
    )
    from investment_agent.materials.store import MaterialStore
    from investment_agent.storage.database import dispose_engine, init_engine, session_scope
    from investment_agent.storage.repositories.listing import ListingRepository
    from investment_agent.storage.repositories.watchlist import WatchlistRepository

    init_engine(database_url)
    try:
        async with session_scope() as session:
            listing = await ListingRepository(session).ensure_a_share_listing(
                "600519.SH", company_name="HK208 legacy sentinel 茅台"
            )
            item = await WatchlistRepository(session).add(
                listing.id,
                note="HK208 M1 legacy watchlist sentinel",
                provider="ifind",
                industry="白酒",
                report_year=2025,
            )
            draft = DatasetDraft(
                kind=DatasetKind.FILING_INDEX,
                subject_kind=SubjectKind.COMPANY,
                subject_id=listing.company_id,
                source=SENTINEL_SOURCE,
                schema_version=1,
                adapter_version="hk208-m1-v1",
                captured_at=captured_at,
                payload={"facts": [], "sentinel": "preserve-exactly"},
                raw_bytes=SENTINEL_BYTES,
                raw_media_type="text/plain",
                raw_source_url="https://legacy.invalid/hk208-sentinel.txt",
            )
            saved = await MaterialStore(session, data_dir=data_dir).save(draft)
            dataset_hash = content_hash(draft)
            await session.commit()
            return {
                "listing_id": listing.id,
                "watchlist_id": item.id,
                "dataset_id": saved.id,
                "company_id": listing.company_id,
                "payload": draft.payload,
                "content_hash": dataset_hash,
                "raw_ref": saved.raw_ref,
            }
    finally:
        await dispose_engine()


async def _import_and_prepare(
    database_url: str,
    *,
    manifest: Path,
    source_root: Path,
    data_dir: Path,
    imported_at: datetime,
    as_of: datetime,
) -> dict[str, Any]:
    from investment_agent.domain.security import DEFAULT_PILOT_LIST_PATH, load_pilot_identities
    from investment_agent.materials.bootstrap import initialize_pilot
    from investment_agent.materials.publication_prepare import prepare_material_publication
    from investment_agent.materials.reviewed_import import import_reviewed_annual
    from investment_agent.storage.database import dispose_engine, init_engine, session_scope
    from investment_agent.storage.models.stock_directory import StockDirectoryEntry

    identities = load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
    if len(identities) != 8:
        raise DrillError(f"expected eight controlled identities, found {len(identities)}")
    init_engine(database_url)
    try:
        async with session_scope() as session:
            setup = await initialize_pilot(session, identities)
            # The API starts a provider sync only when this table is empty. Seed a
            # local directory fixture so startup remains fully offline.
            session.add(
                StockDirectoryEntry(
                    ticker="600519.SH",
                    name="贵州茅台",
                    pinyin_initials="gzmt",
                    status="listed",
                    synced_on=imported_at.date(),
                    provider="hk208-drill",
                    captured_at=imported_at,
                )
            )
            await session.flush()
            first = await import_reviewed_annual(
                session,
                manifest_path=manifest,
                source_root=source_root,
                data_dir=data_dir,
                identities=identities,
                company_ids=setup.company_ids,
                imported_at=imported_at,
            )
            second = await import_reviewed_annual(
                session,
                manifest_path=manifest,
                source_root=source_root,
                data_dir=data_dir,
                identities=identities,
                company_ids=setup.company_ids,
                imported_at=imported_at,
            )
            if len(first.saved) != 8 or len(second.saved) != 8:
                raise DrillError("reviewed import did not return eight datasets on both passes")
            if any(item.created for item in second.saved):
                raise DrillError("repeat reviewed import was not idempotent")
            listing_ids = {
                identity.company_key: setup.listings[(identity.market_code, identity.ticker)].id
                for identity in identities
            }
            saved_refs: dict[str, list[dict[str, Any]]] = {}
            partial_gaps: dict[str, dict[str, Any]] = {}
            for identity in identities:
                listing_id = listing_ids[identity.company_key]
                result = await prepare_material_publication(
                    session, listing_id=listing_id, as_of=as_of
                )
                usable = [metric for metric in result.metrics if metric.calculation_ref]
                if not usable:
                    raise DrillError(f"no calculation refs prepared for {identity.company_key}")
                if any(
                    metric.status != "prepared" or "ambiguous_source" in metric.gaps
                    for metric in usable
                ):
                    raise DrillError(
                        f"saved calculations are not qualified for {identity.company_key}"
                    )
                saved_refs[identity.company_key] = [
                    {
                        "key": metric.key,
                        "calculation_ref": metric.calculation_ref.model_dump(mode="json"),
                        "fact_refs": [ref.model_dump(mode="json") for ref in metric.fact_refs],
                    }
                    for metric in usable
                ]
                partial_gaps[identity.company_key] = {
                    "prepared_metrics": len(usable),
                    "blocked_metrics": len(result.metrics) - len(usable),
                    "blocked_gaps": list(result.gaps),
                }
            return {
                "listing_ids": listing_ids,
                "prepared": saved_refs,
                "datasets_created": sum(item.created for item in first.saved),
                "datasets_reused_on_repeat": sum(not item.created for item in second.saved),
                "companies_imported": len(first.companies),
                "partial_metric_gaps": partial_gaps,
            }
    finally:
        await dispose_engine()


def _capture_db_rows(db_path: Path) -> dict[str, Any]:
    conn = sqlite3.connect(f"file:{db_path.as_posix()}?mode=ro", uri=True)
    try:
        conn.row_factory = sqlite3.Row
        rows = {}
        for table, fields in [
            ("watchlist_item", "id, listing_id, note, provider, enabled"),
            (
                "research_dataset",
                "id, kind, subject_kind, subject_id, source, period_end, revision, content_hash, payload",
            ),
        ]:
            if table == "watchlist_item":
                row = conn.execute(
                    f"SELECT {fields} FROM {table} WHERE note = ?",
                    ("HK208 M1 legacy watchlist sentinel",),
                ).fetchone()
            else:
                row = conn.execute(
                    f"SELECT {fields}, raw_ref FROM {table} WHERE source = ?", (SENTINEL_SOURCE,)
                ).fetchone()
            if row is None:
                raise DrillError(f"legacy sentinel missing in {table}")
            rows[table] = dict(row)
        return rows
    finally:
        conn.close()


def _verify_immutable(db_path: Path, before: dict[str, Any]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        try:
            conn.execute(
                "UPDATE research_dataset SET payload = '{}' WHERE source = ?", (SENTINEL_SOURCE,)
            )
            conn.commit()
        except sqlite3.DatabaseError as exc:
            conn.rollback()
            if "immutable" not in str(exc).lower():
                raise DrillError(
                    f"legacy mutation rejected for an unexpected reason: {exc}"
                ) from exc
        else:
            raise DrillError("legacy research dataset mutation unexpectedly succeeded")
        after = _capture_db_rows(db_path)
        if before != after:
            raise DrillError("legacy sentinel rows changed during migration")
    finally:
        conn.close()


def _perform_restored_checks(
    db_path: Path,
    data_dir: Path,
    report_file: Path,
    listing_ids: dict[str, int],
    prepared: dict[str, list[dict[str, Any]]],
    source_repo: Path,
    as_of: datetime,
    legacy_rows: dict[str, Any],
) -> dict[str, Any]:
    from fastapi.testclient import TestClient

    from investment_agent.api.app import create_app
    from investment_agent.evidence.store import EvidenceService
    from investment_agent.investor.common import ObjectRef
    from investment_agent.storage.database import dispose_engine, init_engine, session_scope

    if _capture_db_rows(db_path) != legacy_rows:
        raise DrillError("legacy watchlist or research-material sentinel changed after restore")

    env = _isolated_env(db_path, data_dir)
    os.environ.update(
        {
            "DATABASE_URL": env["DATABASE_URL"],
            "DATA_DIR": env["DATA_DIR"],
            "API_KEY": env["API_KEY"],
            "LLM_TIER3_API_KEY": env["LLM_TIER3_API_KEY"],
        }
    )
    # The app's settings are intentionally prevented from loading any developer .env.
    from investment_agent.config import Settings

    Settings.model_config["env_file"] = None
    _offline_socket_guard()
    client = TestClient(create_app())
    statuses: dict[str, int] = {}
    try:
        with client:
            for key, listing_id in listing_ids.items():
                response = client.get(
                    f"/research/listings/{listing_id}/financial-context",
                    headers={"X-API-Key": env["API_KEY"]},
                )
                statuses[key] = response.status_code
                if response.status_code != 200:
                    raise DrillError(
                        f"financial-context returned {response.status_code}: {response.text}"
                    )
        # TestClient shutdown disposed its app engine. Start a separate engine and
        # session, simulating a fresh process after restore.
        init_engine(env["DATABASE_URL"])
        try:

            async def check_refs() -> int:
                async with session_scope() as session:
                    service = EvidenceService(session)
                    count = 0
                    for key, refs in prepared.items():
                        for item in refs:
                            calculation_ref = ObjectRef.model_validate(item["calculation_ref"])
                            expected_fact_refs = [
                                ObjectRef.model_validate(ref) for ref in item["fact_refs"]
                            ]
                            read = await service.get_calculation(
                                "local_owner", calculation_ref, as_of
                            )
                            if read.data is None or read.data.results[item["key"]].value is None:
                                raise DrillError(
                                    f"saved calculation unreadable: {key}/{item['key']}"
                                )
                            if list(read.data.fact_refs) != expected_fact_refs:
                                raise DrillError(f"exact fact refs changed: {key}/{item['key']}")
                            count += 1
                    return count

            calculation_count = asyncio.run(check_refs())
        finally:
            asyncio.run(dispose_engine())
    finally:
        client.close()
    # Independently run the release auditor in a subprocess, which also verifies
    # the copied original-byte references against the restored data directory.
    release_report = _safe_child(report_file.parent, "release-check.json")
    output = _run(
        [
            sys.executable,
            str(source_repo / "tools/hk208_release_check.py"),
            "--database",
            str(db_path),
            "--data-dir",
            str(data_dir),
            "--report",
            str(release_report),
            "--require-m1",
        ],
        cwd=source_repo,
        env=env,
    )
    parsed = json.loads(release_report.read_text(encoding="utf-8"))
    if not parsed.get("ok"):
        raise DrillError(f"read-only release check failed: {parsed.get('issues')}")
    return {
        "http_statuses": statuses,
        "calculation_count": calculation_count,
        "legacy_sentinel_preserved": True,
        "release_check": parsed,
        "release_check_output": output,
    }


def _fresh_read_check(
    db_path: Path,
    data_dir: Path,
    report_file: Path,
    listing_ids: dict[str, int],
    prepared: dict[str, list[dict[str, Any]]],
    source_repo: Path,
    as_of: datetime,
    legacy_rows: dict[str, Any],
) -> dict[str, Any]:
    """Run the restore checks in a new interpreter, then parse its small report."""
    request_path = _safe_child(report_file.parent, "fresh-process-check-input.json")
    result_path = _safe_child(report_file.parent, "fresh-process-check.json")
    request_path.write_text(
        json.dumps(
            {
                "database": str(db_path),
                "data_dir": str(data_dir),
                "report": str(report_file),
                "listing_ids": listing_ids,
                "prepared": prepared,
                "source_repo": str(source_repo),
                "as_of": as_of.isoformat(),
                "result": str(result_path),
                "legacy_rows": legacy_rows,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    env = _isolated_env(db_path, data_dir)
    _run(
        [
            sys.executable,
            str(Path(__file__).resolve()),
            "--check-restored",
            "--check-input",
            str(request_path),
        ],
        cwd=source_repo,
        env=env,
    )
    return json.loads(result_path.read_text(encoding="utf-8"))


def run(args: argparse.Namespace) -> dict[str, Any]:
    work_dir = args.work_dir.expanduser().absolute().resolve(strict=False)
    manifest = args.manifest.expanduser().resolve(strict=True)
    source_root = args.source_root.expanduser().resolve(strict=True)
    source_repo = args.source_repo.expanduser().resolve(strict=True)
    if work_dir.exists():
        raise DrillError(f"work directory must not already exist: {work_dir}")
    if not manifest.is_file() or not source_root.is_dir():
        raise DrillError("manifest must be a file and source-root must be a directory")
    for path in (manifest, source_root):
        if work_dir == path or work_dir in path.parents or path in work_dir.parents:
            raise DrillError("work directory must be separate from all read-only inputs")
    if work_dir == source_repo or work_dir in source_repo.parents:
        raise DrillError("work directory cannot contain or replace the source repository")
    work_dir.mkdir(mode=0o700, parents=True, exist_ok=False)
    work_dir.chmod(0o700)
    report_path = work_dir / "report.json"
    report: dict[str, Any] = {
        "schema": "hk208-m1-drill-report.v1",
        "started_at": datetime.now(UTC).isoformat(),
        "status": "running",
        "scope": {
            "code": "worktree implementation only",
            "database_migration": "isolated drill database only",
            "reviewed_import": "isolated drill database only",
            "source_mode": "not yet checked",
            "browser_page_acceptance": "not checked by this CLI; separate browser acceptance is required",
            "production_release": False,
        },
        "paths": {
            "work_dir": str(work_dir),
            "manifest": str(manifest),
            "source_root": str(source_root),
            "source_repo": str(source_repo),
        },
    }
    try:
        _offline_socket_guard()
        _install_subprocess_network_guard(work_dir)
        from investment_agent.materials.handlers import ONLINE_REFRESH_ENABLED

        if ONLINE_REFRESH_ENABLED is not False:
            raise DrillError("ONLINE_REFRESH_ENABLED must remain False during the M1 drill")
        report["scope"]["source_mode"] = (
            "offline-only; handlers.ONLINE_REFRESH_ENABLED=False (asserted)"
        )
        db_path = work_dir / "data/app.db"
        data_dir = work_dir / "data"
        data_dir.mkdir(mode=0o700)
        database_url = f"sqlite+aiosqlite:///{db_path}"
        env = _isolated_env(db_path, data_dir)
        # Migration is an in-process Alembic call with the explicit drill URL.
        from alembic import command
        from alembic.config import Config

        cfg = Config(str(source_repo / "alembic.ini"))
        cfg.set_main_option("script_location", str(source_repo / "alembic"))
        cfg.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        old_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = database_url
        try:
            command.upgrade(cfg, "048")
        finally:
            if old_database_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old_database_url
        imported_at = datetime.now(UTC)
        as_of = imported_at
        sentinel = asyncio.run(_seed_legacy(database_url, data_dir, imported_at))
        sentinel["raw_sha256"] = hashlib.sha256(SENTINEL_BYTES).hexdigest()
        before = _capture_db_rows(db_path)
        report["legacy_sentinel"] = sentinel
        report["migration"] = {"from": "048", "to": "049", "backup_before_upgrade": True}

        backup_dir = work_dir / "backup-before-049"
        report["pre_upgrade_backup"] = _run(
            [
                sys.executable,
                str(source_repo / "tools/backup_research.py"),
                "--data-dir",
                str(data_dir),
                "--db",
                str(db_path),
                "--output",
                str(backup_dir),
            ],
            cwd=source_repo,
            env=env,
        )
        # verify command included to prove the backup manifest before migration.
        _run(
            [
                sys.executable,
                str(source_repo / "tools/verify_backup.py"),
                "--backup-dir",
                str(backup_dir),
            ],
            cwd=source_repo,
            env=env,
        )
        old_database_url = os.environ.get("DATABASE_URL")
        os.environ["DATABASE_URL"] = database_url
        try:
            command.upgrade(cfg, "head")
        finally:
            if old_database_url is None:
                os.environ.pop("DATABASE_URL", None)
            else:
                os.environ["DATABASE_URL"] = old_database_url
        report["legacy_sentinel_rows"] = before
        _verify_immutable(db_path, before)
        report["legacy_sentinel_preserved_across_049"] = True

        import_summary = asyncio.run(
            _import_and_prepare(
                database_url,
                manifest=manifest,
                source_root=source_root,
                data_dir=data_dir,
                imported_at=imported_at,
                as_of=as_of,
            )
        )
        report["timestamps"] = {"imported_at": imported_at.isoformat(), "as_of": as_of.isoformat()}
        report["reviewed_import_and_calculations"] = import_summary
        backup_dir = work_dir / "backup-restorable"
        _run(
            [
                sys.executable,
                str(source_repo / "tools/backup_research.py"),
                "--data-dir",
                str(data_dir),
                "--db",
                str(db_path),
                "--output",
                str(backup_dir),
            ],
            cwd=source_repo,
            env=env,
        )
        _run(
            [
                sys.executable,
                str(source_repo / "tools/verify_backup.py"),
                "--backup-dir",
                str(backup_dir),
            ],
            cwd=source_repo,
            env=env,
        )
        restored_dir = work_dir / "restored"
        _run(
            [
                sys.executable,
                str(source_repo / "tools/restore_research.py"),
                "--backup-dir",
                str(backup_dir),
                "--output",
                str(restored_dir),
            ],
            cwd=source_repo,
            env=env,
        )
        restored_db = restored_dir / "app.db"
        read_report = _fresh_read_check(
            restored_db,
            restored_dir,
            report_path,
            import_summary["listing_ids"],
            import_summary["prepared"],
            source_repo,
            as_of,
            before,
        )
        report["restored_read_validation"] = read_report
        report["status"] = "passed"
    except Exception as exc:
        report["status"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
        report["traceback"] = traceback.format_exc()
    finally:
        network_log = work_dir / "offline-guard/blocked.jsonl"
        report["network_guard"] = {
            "operations_blocked": [
                "socket.connect",
                "socket.connect_ex",
                "socket.create_connection",
                "socket.getaddrinfo",
            ],
            "parent_attempts_observed": _NETWORK_ATTEMPTS,
            "subprocess_attempts_observed": (
                len(network_log.read_text(encoding="utf-8").splitlines())
                if network_log.is_file()
                else 0
            ),
        }
        report["finished_at"] = datetime.now(UTC).isoformat()
        report_path.write_text(
            json.dumps(report, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
        )
        report_path.chmod(0o600)
    if report["status"] != "passed":
        print(
            json.dumps(
                {
                    "status": report["status"],
                    "report": str(report_path),
                    "error": report.get("error"),
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return report
    print(json.dumps({"status": "passed", "report": str(report_path)}, ensure_ascii=False))
    return report


def main() -> int:
    if sys.argv[1:2] == ["--check-restored"]:
        parser = argparse.ArgumentParser()
        parser.add_argument("--check-restored", action="store_true")
        parser.add_argument("--check-input", type=Path, required=True)
        args = parser.parse_args()
        request = json.loads(args.check_input.read_text(encoding="utf-8"))
        try:
            result = _perform_restored_checks(
                Path(request["database"]),
                Path(request["data_dir"]),
                Path(request["report"]),
                request["listing_ids"],
                request["prepared"],
                Path(request["source_repo"]),
                datetime.fromisoformat(request["as_of"]),
                request["legacy_rows"],
            )
        except Exception as exc:
            print(f"fresh restored read check failed: {type(exc).__name__}: {exc}", file=sys.stderr)
            return 1
        result_path = Path(request["result"])
        result_path.write_text(
            json.dumps(result, ensure_ascii=False, default=str) + "\n", encoding="utf-8"
        )
        return 0
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--source-root", type=Path, required=True)
    parser.add_argument("--source-repo", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    result = run(args)
    return 0 if result["status"] == "passed" else 1


if __name__ == "__main__":
    raise SystemExit(main())
