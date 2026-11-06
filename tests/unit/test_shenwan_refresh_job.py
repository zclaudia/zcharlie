"""``shenwan_directory_refresh``: fetch -> validate -> write the DATA_DIR copy (WP5).

The fetch is always the offline fixture backend (``tests/unit/_shenwan_fake``);
the coverage check runs against the baseline industry-groups mapping with an
empty CSI 300 table, so the directory-level gaps (a new level-1 code) are what
the notification must surface.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.util
from datetime import date
from pathlib import Path
from typing import Any

import pytest

from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.providers.akshare_shenwan import RawTable, ShenwanTransientError
from investment_agent.providers.shenwan_directory import (
    DEFAULT_SHENWAN_DIRECTORY_PATH,
    default_shenwan_directory,
    invalidate_default_shenwan_directory,
    load_shenwan_directory,
    runtime_shenwan_directory_path,
)
from investment_agent.providers.shenwan_refresh import (
    fetch_shenwan_directory,
    validate_rendered_directory,
)
from investment_agent.snapshots.jobs import ShenwanRefreshPayload, idempotency_key
from investment_agent.snapshots.shenwan_refresh import collect_shenwan_refresh
from tests.unit._shenwan_fake import FakeShenwanBackend, fake_shenwan_client

TODAY = date(2026, 11, 1)
TOOL = Path(__file__).resolve().parents[2] / "tools" / "refresh_shenwan_directory.py"


class NewCodeBackend(FakeShenwanBackend):
    """The official table plus one level-1 code the mapping has no default for."""

    def class_code_table(self) -> RawTable:
        table = super().class_code_table()
        row = dict(table.rows[0])
        row.update(
            {
                "行业代码": "990000",
                "一级行业名称": "测试新行业",
                "二级行业名称": None,
                "三级行业名称": None,
            }
        )
        return RawTable(table.columns, (*table.rows, row))


def _notification(tmp_path: Path, run_id: int) -> str:
    return (
        tmp_path / "notifications" / TODAY.isoformat() / f"shenwan_refresh_{run_id}.md"
    ).read_text(encoding="utf-8")


async def _refresh(tmp_path: Path, *, backend: FakeShenwanBackend | None = None, run_id: int = 17):
    return await collect_shenwan_refresh(
        ShenwanRefreshPayload(as_of=TODAY),
        client=fake_shenwan_client(backend),
        data_dir=tmp_path,
        notifier=FileNotifier(tmp_path / "notifications"),
        run_id=run_id,
        today=TODAY,
    )


def test_fetched_directory_survives_the_round_trip() -> None:
    """The CD3 validator: what a refresh writes must parse back through the loader."""

    with pytest.raises(ValueError, match="industries"):
        validate_rendered_directory("version: x\nsystem: sw2021\nindustries: {}\n")


async def test_refresh_writes_only_the_data_dir_copy(tmp_path, db_session, monkeypatch) -> None:
    repo_before = DEFAULT_SHENWAN_DIRECTORY_PATH.read_bytes()
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    invalidate_default_shenwan_directory()

    outcome = await _refresh(tmp_path)

    assert outcome.status == "success"
    assert DEFAULT_SHENWAN_DIRECTORY_PATH.read_bytes() == repo_before, "baseline must stay untouched"
    target = runtime_shenwan_directory_path(data_dir=tmp_path)
    assert target.is_file() and outcome.metadata["path"] == str(target)
    written = load_shenwan_directory(target)
    assert written.version == outcome.metadata["version"] == "2026-10-06"
    assert outcome.metadata["level_counts"] == {
        f"level{n}": len(written.codes(n)) for n in (1, 2, 3)
    }
    assert outcome.metadata["changed"] is False  # same code set as the baseline
    assert outcome.metadata["coverage_ok"] is True

    # Every reader (this process included) serves the copy from the next call.
    assert default_shenwan_directory().version == written.version
    body = _notification(tmp_path, run_id=17)
    assert "`info`" in body and written.version in body and str(target) in body
    invalidate_default_shenwan_directory()


async def test_refresh_reports_unmapped_codes_as_action_required(
    tmp_path, db_session, monkeypatch
) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    invalidate_default_shenwan_directory()

    outcome = await _refresh(tmp_path, backend=NewCodeBackend(), run_id=18)

    # The copy takes effect anyway: the gap is the mapping's problem, not the
    # directory's (it is exactly the level-1/2 follow-up the plan calls out).
    assert default_shenwan_directory().get("990000") is not None
    assert outcome.metadata["added"] == ["990000"]
    assert outcome.metadata["coverage_ok"] is False
    gaps = outcome.metadata["coverage"]
    assert gaps["missing_level1_defaults"] == [
        {"code": "990000", "name": "测试新行业", "tickers": []}
    ]
    assert "覆盖" in outcome.summary

    body = _notification(tmp_path, run_id=18)
    assert "`action_required`" in body
    assert "990000" in body and "测试新行业" in body
    assert "/app/industry-groups" in body
    invalidate_default_shenwan_directory()


async def test_refresh_failure_propagates_and_writes_nothing(tmp_path, db_session) -> None:
    class BrokenClient:
        async def fetch_code_directory(self, *, version: str | None = None) -> Any:
            raise ShenwanTransientError("HTTP 504 from swsresearch.com", provider="akshare")

    with pytest.raises(ShenwanTransientError):
        await collect_shenwan_refresh(
            ShenwanRefreshPayload(as_of=TODAY),
            client=BrokenClient(),
            data_dir=tmp_path,
            notifier=FileNotifier(tmp_path / "notifications"),
            run_id=19,
            today=TODAY,
        )
    assert not runtime_shenwan_directory_path(data_dir=tmp_path).exists()
    assert not (tmp_path / "notifications").exists()


def test_payload_key_is_one_run_per_day() -> None:
    assert (
        idempotency_key(ShenwanRefreshPayload(as_of=TODAY))
        == "shenwan_directory_refresh:2026-11-01"
    )


# ---- the CLI tool is a thin wrapper over the src fetch -----------------------


def _load_tool() -> Any:
    spec = importlib.util.spec_from_file_location("refresh_shenwan_directory_tool", TOOL)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_tool_fetches_through_the_src_function(monkeypatch, tmp_path: Path) -> None:
    module = _load_tool()
    monkeypatch.setattr(module, "AkshareShenwanClient", lambda *a, **k: fake_shenwan_client())
    real_fetch = module.fetch_shenwan_directory
    seen: dict[str, Any] = {}

    async def spy(client: Any, *, version: str | None = None) -> Any:
        seen["client"] = client
        return await real_fetch(client, version=version)

    monkeypatch.setattr(module, "fetch_shenwan_directory", spy)

    target = tmp_path / "shenwan_2021.yaml"
    args = argparse.Namespace(
        write=True, fixtures=False, cross_check=False, limit=None, cache=None, path=str(target)
    )
    assert asyncio.run(module.main(args)) == 0

    assert "client" in seen, "the tool must fetch through providers.shenwan_refresh"
    assert load_shenwan_directory(target).version == "2026-10-06"
    repo_before = DEFAULT_SHENWAN_DIRECTORY_PATH.read_bytes()
    assert DEFAULT_SHENWAN_DIRECTORY_PATH.read_bytes() == repo_before


def test_cli_tool_without_write_only_reports_the_diff(monkeypatch, tmp_path: Path, capsys) -> None:
    module = _load_tool()
    monkeypatch.setattr(module, "AkshareShenwanClient", lambda *a, **k: fake_shenwan_client())
    target = tmp_path / "shenwan_2021.yaml"
    target.write_text(
        DEFAULT_SHENWAN_DIRECTORY_PATH.read_text(encoding="utf-8"), encoding="utf-8"
    )
    args = argparse.Namespace(
        write=False, fixtures=False, cross_check=False, limit=None, cache=None, path=str(target)
    )
    assert asyncio.run(module.main(args)) == 0
    assert "diff vs" in capsys.readouterr().out
    assert target.read_text(encoding="utf-8") == DEFAULT_SHENWAN_DIRECTORY_PATH.read_text(
        encoding="utf-8"
    )


async def test_fetch_validates_the_assembled_directory() -> None:
    """The src fetch (shared by tool and job) round-trips what it returns."""

    directory = await fetch_shenwan_directory(fake_shenwan_client())
    committed = default_shenwan_directory()
    assert directory.version == "2026-10-06"
    assert list(directory.industries) == list(committed.industries)


def test_worker_registry_serves_the_kind(tmp_path: Path) -> None:
    """The snapshot handler registry (the worker's default handlers) has the kind."""

    from tests.unit._snapshot_support import FakeProxy, handlers_for, settings_for

    settings = settings_for(tmp_path)
    kinds = {handler.kind for handler in handlers_for(settings, FakeProxy())}
    assert "shenwan_directory_refresh" in kinds
