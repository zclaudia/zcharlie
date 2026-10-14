"""Notifications for finished ``ifind_verify`` runs (P1-A13).

``action_required`` when the run found conflicts on key fields (iFinD vs the
value the factor view uses from free sources) or stopped because the iFinD
quota ran out (A8). At most one file per run (deterministic target path).
"""

from __future__ import annotations

from typing import Any

from investment_agent.domain.pipeline_status import TERMINAL_RUN_STATUSES
from investment_agent.domain.protocols import RenderedContent
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.verification.job import IFIND_VERIFY

_MAX_LISTED = 30


def verification_notification_level(run: PipelineRun) -> str | None:
    if run.kind != IFIND_VERIFY or run.status not in TERMINAL_RUN_STATUSES:
        return None
    metadata = run.metadata_json or {}
    if run.status == "failed" or metadata.get("stop_reason") == "quota":
        return "action_required"
    if metadata.get("key_field_conflicts"):
        return "action_required"
    return None


def _listed(value: object) -> list[str]:
    return [str(item) for item in value] if isinstance(value, list) else []


def _fmt(value: Any) -> str:
    if isinstance(value, float):
        return f"{value:.6g}"
    return str(value)


def render_verification_notification(run: PipelineRun) -> str:
    metadata = run.metadata_json or {}
    finished = run.finished_at or run.started_at
    lines = [
        f"# iFinD 核对 · {finished.date().isoformat()}",
        "",
        "- 级别：`action_required`",
        f"- 运行：#{run.id}（{run.kind}，状态 `{run.status}`）",
        f"- 处理：{metadata.get('done', 0)} 完成，{metadata.get('deferred', 0)} 延后，"
        f"{metadata.get('failed', 0)} 失败；iFinD 调用 {metadata.get('proxy_calls', 0)} 次",
    ]
    if run.error_summary:
        lines.append(f"- 摘要：{run.error_summary}")
    if metadata.get("stop_reason") == "quota":
        lines += [
            "- **iFinD 额度耗尽**：本次核对已停止，剩余请求全部延后到 "
            f"{metadata.get('deferred_until')} 之后再试；已完成的核对结果保留。",
            f"- 上游信息：{metadata.get('stop_error')}",
        ]
    conflicts = metadata.get("conflicts")
    if isinstance(conflicts, dict) and conflicts:
        lines += [
            "",
            f"## 关键字段冲突（{len(_listed(metadata.get('key_field_conflicts')))} 只）",
            "",
            "iFinD 与观察端所用的免费源取值超出容差。观察端对冲突字段仍用免费源取值并标记 `conflict`；"
            "请人工确认口径（金融股、重述、单位）。",
            "",
        ]
        for ticker, item in list(conflicts.items())[:_MAX_LISTED]:
            if not isinstance(item, dict):
                continue
            fields = item.get("fields") or {}
            parts = []
            for name, detail in fields.items():
                if not isinstance(detail, dict):
                    continue
                key = "（关键）" if name in (item.get("key_fields") or []) else ""
                parts.append(
                    f"{name}{key} iFinD {_fmt(detail.get('ifind'))} / "
                    f"{detail.get('free_provider')} {_fmt(detail.get('free'))}"
                    f"（差 {_fmt(detail.get('relative_difference'))}，容差 {_fmt(detail.get('tolerance'))}）"
                )
            lines.append(
                f"- {ticker} · {item.get('report_period')} · {item.get('trigger')}：" + "；".join(parts)
            )
    lines += ["", "查看：`ia verify status`；重新核对：`ia verify request --ticker … --trigger conflict`。"]
    return "\n".join(lines) + "\n"


async def notify_verification_run(run_id: int, *, notifier: FileNotifier) -> str | None:
    async with session_scope() as session:
        run = await PipelineRunRepository(session).get(run_id)
        if run is None:
            return None
        level = verification_notification_level(run)
        if level is None:
            return None
        body = render_verification_notification(run)
        finished = run.finished_at or run.started_at
        channel_metadata: dict[str, object] = {
            "kind": f"verification_{run.kind}",
            "run_id": run.id,
            "date": finished.date().isoformat(),
            "level": level,
        }
    content = RenderedContent(format="markdown", body=body, metadata={"level": level})
    target = notifier.target_path(content.format, channel_metadata)
    if target.is_file():
        return str(target)
    result = await notifier.send(content, channel_metadata=channel_metadata)
    if not result.success:
        raise RuntimeError(result.error or "verification notification failed")
    return result.target
