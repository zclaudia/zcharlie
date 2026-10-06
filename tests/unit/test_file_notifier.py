"""Tests for FileNotifier."""

from datetime import date
from pathlib import Path

import pytest

from investment_agent.domain.protocols import RenderedContent
from investment_agent.notifiers.file_notifier import FileNotifier


@pytest.mark.asyncio
async def test_writes_markdown_file_with_expected_name_and_content(
    tmp_path: Path,
) -> None:
    notifier = FileNotifier(tmp_path)
    content = RenderedContent(
        format="markdown", body="# Hello\n", metadata={"run_id": 7}
    )
    result = await notifier.send(
        content,
        channel_metadata={"kind": "research_change", "run_id": 7, "date": "2026-06-07"},
    )

    expected = tmp_path / "2026-06-07" / "research_change_7.md"
    assert result.success is True
    assert result.target == str(expected)
    assert expected.read_text() == "# Hello\n"


@pytest.mark.asyncio
async def test_creates_day_directory_when_missing(tmp_path: Path) -> None:
    notifier = FileNotifier(tmp_path / "deep" / "reports")
    content = RenderedContent(format="markdown", body="body", metadata={})
    today = date.today().isoformat()
    result = await notifier.send(
        content,
        channel_metadata={"kind": "research_change", "run_id": 1},
    )
    day_dir = tmp_path / "deep" / "reports" / today
    assert day_dir.is_dir()
    assert result.target == str(day_dir / "research_change_1.md")


@pytest.mark.asyncio
async def test_returns_success_and_target_path(tmp_path: Path) -> None:
    notifier = FileNotifier(tmp_path)
    content = RenderedContent(format="markdown", body="x", metadata={})
    result = await notifier.send(
        content,
        channel_metadata={"kind": "change_notification", "run_id": 42, "date": "2026-01-01"},
    )
    assert result.notifier_name == "file"
    assert result.success is True
    assert result.error is None
    assert result.target.endswith("/2026-01-01/change_notification_42.md")
