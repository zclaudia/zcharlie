"""Shenwan directory resolution: the DATA_DIR copy takes precedence (WP5).

Mirrors ``test_calendar_resolution.py``: the runtime copy a refresh job writes
(``DATA_DIR/shenwan/shenwan_2021.yaml``) shadows the committed baseline, a
broken copy fails closed, and the memoized default directory picks a rewritten
copy up without a process restart.
"""

from __future__ import annotations

import dataclasses
import os
from pathlib import Path

import pytest

from investment_agent.providers.shenwan_directory import (
    DEFAULT_SHENWAN_DIRECTORY_PATH,
    default_shenwan_directory,
    dump_shenwan_directory,
    invalidate_default_shenwan_directory,
    load_shenwan_directory,
    resolved_shenwan_directory_path,
    runtime_shenwan_directory_path,
)


def test_runtime_path_keeps_the_baseline_file_name(tmp_path: Path) -> None:
    target = runtime_shenwan_directory_path(data_dir=tmp_path)

    assert target == tmp_path / "shenwan" / "shenwan_2021.yaml"
    assert not target.exists()  # computed, not created


def test_resolution_prefers_the_data_dir_copy(tmp_path: Path) -> None:
    assert resolved_shenwan_directory_path(data_dir=tmp_path) == DEFAULT_SHENWAN_DIRECTORY_PATH

    target = runtime_shenwan_directory_path(data_dir=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text(DEFAULT_SHENWAN_DIRECTORY_PATH.read_text(encoding="utf-8"), "utf-8")

    assert resolved_shenwan_directory_path(data_dir=tmp_path) == target
    # An explicit path is honoured as-is (the CLI's diff / --write target).
    assert load_shenwan_directory(target).version == load_shenwan_directory(
        DEFAULT_SHENWAN_DIRECTORY_PATH
    ).version


def test_a_broken_runtime_copy_is_not_silently_ignored(tmp_path: Path) -> None:
    """Fail closed: a corrupt copy must surface, never fall back to the baseline."""

    target = runtime_shenwan_directory_path(data_dir=tmp_path)
    target.parent.mkdir(parents=True)
    target.write_text("version: broken\n", encoding="utf-8")

    assert resolved_shenwan_directory_path(data_dir=tmp_path) == target
    with pytest.raises(ValueError):
        load_shenwan_directory(resolved_shenwan_directory_path(data_dir=tmp_path))


def test_default_directory_follows_the_environment(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    invalidate_default_shenwan_directory()
    assert default_shenwan_directory().version == load_shenwan_directory(
        DEFAULT_SHENWAN_DIRECTORY_PATH
    ).version
    invalidate_default_shenwan_directory()


def test_default_directory_serves_a_refresh_without_restart(monkeypatch, tmp_path: Path) -> None:
    """The refresh job's write is visible from the next call in the same process."""

    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    invalidate_default_shenwan_directory()
    directory = default_shenwan_directory()
    first = directory.codes(1)[0]

    target = runtime_shenwan_directory_path(data_dir=tmp_path)
    target.parent.mkdir(parents=True)
    refreshed = dataclasses.replace(
        directory,
        version="2099-12-31",
        industries={
            **directory.industries,
            first: dataclasses.replace(directory.industries[first], name="测试新名称"),
        },
    )
    target.write_text(dump_shenwan_directory(refreshed), encoding="utf-8")

    served = default_shenwan_directory()
    assert served.version == "2099-12-31"
    assert served.name_of(first) == "测试新名称"

    # A rewrite of the same file is picked up too (the memo keys on the mtime).
    again = dataclasses.replace(refreshed, version="2099-12-31-2")
    target.write_text(dump_shenwan_directory(again), encoding="utf-8")
    os.utime(target, (2_000_000_000, 2_000_000_000))  # a stamp that differs, deterministically
    assert default_shenwan_directory().version == "2099-12-31-2"
    invalidate_default_shenwan_directory()
