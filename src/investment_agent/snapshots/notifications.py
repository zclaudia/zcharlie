"""Notifications for finished snapshot runs (via the notifier abstraction).

* ``info`` - a universe sync changed index membership;
* ``action_required`` - a snapshot run failed, a market / financial run
  finished below the completeness threshold, or it stopped because the iFinD
  quota ran out (``stop_reason="quota"``).

Each run notifies at most once: the target path is deterministic per run and
an existing file is never rewritten.
"""

from __future__ import annotations

from typing import Any, Literal

from investment_agent.domain.protocols import Notifier, RenderedContent
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.snapshots.collect import QUOTA_STOP_REASON
from investment_agent.snapshots.jobs import (
    FINANCIAL_SNAPSHOT,
    MARKET_SNAPSHOT,
    SNAPSHOT_KINDS,
    UNIVERSE_SYNC,
)
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository

Level = Literal["info", "action_required"]

_KIND_LABELS = {
    "universe_sync": "成分同步",
    "industry_sync": "行业同步",
    "market_snapshot": "日线快照",
    "financial_snapshot": "财报快照",
}
_MAX_LISTED = 30


def _completeness(run: PipelineRun) -> dict[str, Any] | None:
    value = (run.metadata_json or {}).get("completeness")
    return value if isinstance(value, dict) else None


def notification_level(run: PipelineRun, *, threshold: float) -> Level | None:
    """Decide whether (and how loudly) a terminal snapshot run is reported."""

    if run.kind not in SNAPSHOT_KINDS or run.status not in {"success", "partial", "failed"}:
        return None
    if run.status == "failed":
        return "action_required"
    if run.kind in {MARKET_SNAPSHOT, FINANCIAL_SNAPSHOT}:
        if (run.metadata_json or {}).get("stop_reason") == QUOTA_STOP_REASON:
            return "action_required"
        completeness = _completeness(run)
        ratio = completeness.get("ratio") if completeness else None
        if isinstance(ratio, int | float) and ratio < threshold:
            return "action_required"
        return None
    if run.kind == UNIVERSE_SYNC and (run.metadata_json or {}).get("changed"):
        return "info"
    return None


def _tickers(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _run_date(run: PipelineRun) -> str:
    request = run.request_json or {}
    for key in ("trade_date", "as_of"):
        value = request.get(key)
        if isinstance(value, str):
            return value
    finished = run.finished_at or run.started_at
    return finished.date().isoformat()


def render_snapshot_notification(run: PipelineRun, level: Level, *, threshold: float) -> str:
    label = _KIND_LABELS.get(run.kind, run.kind)
    request = run.request_json or {}
    universe = run.universe_code or (
        "观察池" if request.get("scope") == "watchlist" else "全市场"
    )
    metadata = run.metadata_json or {}
    lines = [
        f"# {label} · {universe} · {_run_date(run)}",
        "",
        f"- 级别：`{level}`",
        f"- 运行：#{run.id}（{run.kind}，状态 `{run.status}`）",
    ]
    if "provider" in request:
        lines.append(f"- 数据源：{request['provider']}")
    if run.error_summary:
        lines.append(f"- 错误：{run.error_summary}")
    quota_stop = metadata.get("stop_reason") == QUOTA_STOP_REASON
    if quota_stop:
        lines.append(
            "- **iFinD 额度耗尽**：本次任务已停止剩余批次，已采数据保留；"
            "额度窗口（约 5 小时）恢复后下次调度自动续采。"
        )
        if metadata.get("stop_error"):
            lines.append(f"- 上游信息：{metadata.get('stop_error')}")
    if run.kind == UNIVERSE_SYNC:
        added = _tickers(metadata.get("added"))
        removed = _tickers(metadata.get("removed"))
        lines.append(f"- 生效日期：{metadata.get('effective_date')}")
        lines.append(f"- 调入（{len(added)}）：{', '.join(added) or '无'}")
        lines.append(f"- 调出（{len(removed)}）：{', '.join(removed) or '无'}")
    completeness = _completeness(run)
    if completeness is not None:
        lines.append(
            f"- 完整率：{completeness.get('ratio')}（阈值 {threshold}）；"
            f"应采 {completeness.get('expected')}，实采 {completeness.get('collected')}，"
            f"失败 {completeness.get('failed_count')}，待披露 {completeness.get('pending_count')}，"
            f"额度未采 {completeness.get('quota_count', 0)}"
        )
        missing = completeness.get("missing") or {}
        if isinstance(missing, dict) and missing:
            lines += ["", "## 缺失清单", ""]
            for ticker, detail in list(missing.items())[:_MAX_LISTED]:
                if isinstance(detail, dict):
                    lines.append(f"- {ticker}：{detail.get('kind')} · {detail.get('reason')}")
            if len(missing) > _MAX_LISTED:
                lines.append(f"- ……另有 {len(missing) - _MAX_LISTED} 只，见运行元数据")
    if quota_stop:
        lines += [
            "",
            "处理：无需立即重试；如需提前补采，等额度恢复后用 `ia snapshot` 重新入队。",
        ]
    elif level == "action_required":
        lines += ["", "处理：检查 iFinD proxy / baostock 可用性后用 `ia snapshot` 重新采集。"]
    return "\n".join(lines) + "\n"


async def notify_snapshot_run(
    run_id: int,
    *,
    notifier: FileNotifier,
    threshold: float,
    extra_notifiers: tuple[Notifier, ...] = (),
) -> str | None:
    """Write the notification for one finished snapshot run, at most once."""

    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(run_id)
        if run is None:
            return None
        level = notification_level(run, threshold=threshold)
        if level is None:
            return None
        body = render_snapshot_notification(run, level, threshold=threshold)
        channel_metadata: dict[str, object] = {
            "kind": f"snapshot_{run.kind}",
            "run_id": run.id,
            "date": _run_date(run),
            "level": level,
        }
    content = RenderedContent(format="markdown", body=body, metadata={"level": level})
    target = notifier.target_path(content.format, channel_metadata)
    if target.is_file():
        return str(target)
    result = await notifier.send(content, channel_metadata=channel_metadata)
    if not result.success:
        raise RuntimeError(result.error or "snapshot notification failed")
    for extra in extra_notifiers:
        await extra.send(content, channel_metadata=channel_metadata)
    return result.target
