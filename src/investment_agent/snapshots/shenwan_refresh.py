"""``shenwan_directory_refresh`` - regenerate the Shenwan 2021 code directory.

config-docs-plan §3.2 / WP5: the directory is reference data, regenerated
rather than hand-edited, so the health page's 行业目录 card enqueues this job
instead of an operator running the host CLI. One run:

* fetches Shenwan's official code table (``SwClassCode_2021.xls``, one
  request) and validates the rendered YAML through the runtime loader (CD3,
  :func:`investment_agent.providers.shenwan_refresh.fetch_shenwan_directory`);
* writes **only the runtime copy** ``DATA_DIR/shenwan/shenwan_2021.yaml``,
  atomically - never the repository baseline (that stays a reviewed commit
  via ``tools/refresh_shenwan_directory.py --write``);
* checks the *new* directory against the effective industry-groups document
  and the current CSI 300 membership
  (:func:`investment_agent.factors.industry_groups.check_industry_groups_coverage`,
  WP3): unmapped codes are exactly what a level-1/2 change means for the
  mapping, so a gapped run notifies ``action_required`` with the codes
  listed, a clean (or unchecked) run ``info`` with the new version and code
  counts;
* records the outcome on the run. No settings revision is bumped (this is
  data, not a config document); every reader picks the copy up through
  :func:`investment_agent.providers.shenwan_directory.default_shenwan_directory`
  (mtime-keyed memo), so no process needs a restart.

Failures (network after retries, a table the loader rejects) propagate to
the worker and settle through the standard snapshot ``action_required``
notification.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.domain.protocols import RenderedContent
from investment_agent.factors.industry_groups import (
    CoverageMember,
    IndustryGroupsCoverage,
    check_industry_groups_coverage,
    default_industry_groups,
    usable_sw_code,
)
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.providers.shenwan_directory import (
    SW2021_SYSTEM,
    DirectoryDiff,
    ShenwanDirectory,
    dump_shenwan_directory,
    load_shenwan_directory,
    resolved_shenwan_directory_path,
    runtime_shenwan_directory_path,
)
from investment_agent.providers.shenwan_refresh import DirectoryClient, fetch_shenwan_directory
from investment_agent.snapshots.jobs import ShenwanRefreshPayload
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.industry import IndustryRepository
from investment_agent.storage.repositories.universe import UniverseRepository

log = structlog.get_logger(__name__)

_MAX_LISTED = 30


async def csi300_coverage_members(
    session: AsyncSession, as_of: date, directory: ShenwanDirectory
) -> list[CoverageMember]:
    """Current CSI 300 members with their CSRC / usable Shenwan code.

    The same reads a snapshot view does
    (:func:`investment_agent.factors.view_db.load_snapshot_view`): the
    classification intervals valid on ``as_of``, the Shenwan code vetted by
    :func:`usable_sw_code` (pre-2021 intervals and unknown codes fall back to
    the CSRC side, exactly what a computation would do). Shared by the
    coverage-check endpoint (WP3) and this refresh job (WP5).
    """

    industries = IndustryRepository(session)
    members: list[CoverageMember] = []
    for member in await UniverseRepository(session).members_as_of("CSI300", as_of):
        csrc = await industries.industry_as_of(member.listing_id, as_of)
        shenwan = await industries.industry_as_of(member.listing_id, as_of, system=SW2021_SYSTEM)
        members.append(
            CoverageMember(
                ticker=member.ticker,
                csrc_code=csrc.code if csrc is not None else None,
                csrc_name=csrc.name if csrc is not None else None,
                sw_code=(
                    usable_sw_code(
                        shenwan.code,
                        shenwan.valid_from,
                        shenwan.valid_to,
                        directory,
                        shenwan.name,
                    )
                    if shenwan is not None
                    else None
                ),
            )
        )
    return members


async def _coverage(directory: ShenwanDirectory, today: date) -> IndustryGroupsCoverage | None:
    """Coverage of the new directory; ``None`` when the DB side is unavailable.

    The refresh itself must not fail because the coverage inputs could not be
    read (fresh database, unmigrated schema): the directory is already
    validated, so the run succeeds and the metadata notes the missing check.
    """

    try:
        async with session_scope() as session:
            members = await csi300_coverage_members(session, today, directory)
            return check_industry_groups_coverage(
                default_industry_groups(), directory=directory, members=members
            )
    except Exception:
        log.exception("shenwan_refresh.coverage_unavailable")
        return None


def _write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _diff_text(diff: DirectoryDiff) -> str:
    if not diff.changed:
        return "无变化"
    parts = [
        f"新增 {', '.join(diff.added) or '无'}",
        f"移除 {', '.join(diff.removed) or '无'}",
        f"更名 {', '.join(f'{code}({old}→{new})' for code, old, new in diff.renamed) or '无'}",
    ]
    return "；".join(parts)


def _gaps(coverage: IndustryGroupsCoverage) -> list[str]:
    """The unmapped codes as actionable lines (what /app/industry-groups fixes)."""

    lines: list[str] = []
    for gap in coverage.missing_level1_defaults:
        lines.append(f"- 申万一级缺默认分组：{gap.code} {gap.name or ''}".rstrip())
    for gap in coverage.invalid_overrides:
        listed = f"（成员 {', '.join(gap.tickers[:_MAX_LISTED])}）" if gap.tickers else ""
        lines.append(f"- 覆写引用了目录外代码：{gap.code}{listed}".rstrip())
    for gap in coverage.unmapped_csrc:
        listed = f"（成员 {', '.join(gap.tickers[:_MAX_LISTED])}）" if gap.tickers else ""
        lines.append(f"- CSRC 码未显式映射：{gap.code} {gap.name or ''}{listed}".rstrip())
    return lines


def _refresh_body(
    *,
    directory: ShenwanDirectory,
    target: Path,
    diff: DirectoryDiff,
    coverage: IndustryGroupsCoverage | None,
    today: date,
    level: str,
) -> str:
    counts = "、".join(
        f"{label} {len(directory.codes(number))}" for label, number in (("一级", 1), ("二级", 2), ("三级", 3))
    )
    captured = directory.captured_on.isoformat() if directory.captured_on else "未知"
    lines = [
        f"# 行业目录刷新 · {today.isoformat()}",
        "",
        f"- 级别：`{level}`",
        f"- 新版本：{directory.version}（采集于 {captured}，来源 {directory.source or 'swsresearch.com'}）",
        f"- 代码数：{counts}",
        f"- 与此前目录的差异：{_diff_text(diff)}",
        f"- 写入：{target}（DATA_DIR 副本；仓库基线未改动）",
        "- 生效：各进程下次读取即用新目录，无需重启。",
    ]
    if coverage is None:
        lines += ["", "覆盖率检查不可用（数据库暂不可读）；目录本身已校验并生效。"]
    elif coverage.ok:
        lines += ["", f"行业分组覆盖检查：{coverage.summary}"]
    else:
        lines += ["", f"行业分组覆盖检查：{coverage.summary}", ""]
        lines += _gaps(coverage)
        lines += [
            "",
            "处理：在「行业分组」页（/app/industry-groups）为上述代码补充映射并保存；"
            "本目录已生效，未映射代码的行业分组暂回退 CSRC / other。",
        ]
    return "\n".join(lines) + "\n"


async def _notify(
    notifier: FileNotifier,
    *,
    run_id: int,
    today: date,
    level: str,
    body: str,
) -> str | None:
    content = RenderedContent(format="markdown", body=body, metadata={"level": level})
    channel_metadata: dict[str, object] = {
        "kind": "shenwan_refresh",
        "run_id": run_id,
        "date": today.isoformat(),
        "level": level,
    }
    target = notifier.target_path(content.format, channel_metadata)
    if target.is_file():
        return str(target)
    result = await notifier.send(content, channel_metadata=channel_metadata)
    if not result.success:
        raise RuntimeError(result.error or "shenwan refresh notification failed")
    return result.target


@dataclass(frozen=True)
class RefreshOutcome:
    status: Literal["success", "partial"]
    metadata: dict[str, object]
    summary: str = ""


async def collect_shenwan_refresh(
    payload: ShenwanRefreshPayload,
    *,
    client: DirectoryClient | None,
    data_dir: Path,
    notifier: FileNotifier,
    run_id: int,
    today: date,
) -> RefreshOutcome:
    """One refresh run: fetch, validate, write the DATA_DIR copy, report coverage."""

    directory = await fetch_shenwan_directory(client)
    target = runtime_shenwan_directory_path(data_dir=data_dir)
    # The diff base is the directory this very write will replace (same
    # data_dir), so the metadata describes the change the run makes.
    previous = load_shenwan_directory(resolved_shenwan_directory_path(data_dir=data_dir))
    diff = previous.diff(directory)
    coverage = await _coverage(directory, today)
    _write_atomic(target, dump_shenwan_directory(directory))
    level = "info" if coverage is None or coverage.ok else "action_required"
    await _notify(
        notifier,
        run_id=run_id,
        today=today,
        level=level,
        body=_refresh_body(
            directory=directory,
            target=target,
            diff=diff,
            coverage=coverage,
            today=today,
            level=level,
        ),
    )
    counts = " / ".join(f"L{n} {len(directory.codes(n))}" for n in (1, 2, 3))
    summary = (
        f"目录已更新到 {directory.version}（{counts}）"
        + (
            f"；行业分组覆盖缺 {len(coverage.missing_level1_defaults) + len(coverage.invalid_overrides) + len(coverage.unmapped_csrc)} 项"
            if coverage is not None and not coverage.ok
            else ""
        )
    )
    log.info(
        "shenwan_refresh.written",
        run_id=run_id,
        path=str(target),
        version=directory.version,
        changed=diff.changed,
        coverage_ok=coverage.ok if coverage is not None else None,
    )
    return RefreshOutcome(
        "success",
        {
            "status": "refreshed",
            "summary": summary,
            "version": directory.version,
            "captured_on": directory.captured_on.isoformat() if directory.captured_on else None,
            "level_counts": {f"level{n}": len(directory.codes(n)) for n in (1, 2, 3)},
            "path": str(target),
            "changed": diff.changed,
            "added": list(diff.added),
            "removed": list(diff.removed),
            "renamed": [[code, old, new] for code, old, new in diff.renamed],
            "coverage_ok": coverage.ok if coverage is not None else None,
            "coverage": coverage.describe() if coverage is not None else None,
        },
        summary,
    )
