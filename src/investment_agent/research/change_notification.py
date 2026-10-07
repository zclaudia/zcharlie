"""Render meaningful research changes as a concise review notification."""

from investment_agent.domain.protocols import RenderedContent

_SOURCE_LABELS = {"ifind": "iFinD", "snapshot": "免费源快照"}


def render_change_notification(
    *,
    ticker: str,
    company_name: str,
    current_run_id: int,
    previous_run_id: int | None,
    diff: dict[str, object],
) -> RenderedContent:
    raw_changes = diff.get("changes")
    changes = raw_changes if isinstance(raw_changes, list) else []
    meaningful = [
        item
        for item in changes
        if isinstance(item, dict) and item.get("meaningful") is True
    ]
    version_change = next(
        (
            item
            for item in changes
            if isinstance(item, dict) and item.get("kind") == "scoring_version_changed"
        ),
        None,
    )
    source_change = next(
        (
            item
            for item in changes
            if isinstance(item, dict) and item.get("kind") == "source_changed"
        ),
        None,
    )
    source_suppressed = [
        item
        for item in changes
        if isinstance(item, dict) and item.get("suppressed_reason") == "source_changed"
    ]
    lines = [
        f"# 研究变化提醒：{ticker} {company_name}",
        "",
        f"当前运行：{current_run_id}",
        f"上一运行：{previous_run_id if previous_run_id is not None else '-'}",
        f"重要变化：{len(meaningful)} 项",
    ]
    if version_change is not None:
        lines.append(
            f"评分版本变更：{version_change.get('before')!s} → "
            f"{version_change.get('after')!s}（评分与策略差异源于算法调整，未计入重要变化）"
        )
    if source_change is not None:
        lines.append(
            f"数据源变更：{_SOURCE_LABELS.get(str(source_change.get('before')), source_change.get('before'))}"
            f" → {_SOURCE_LABELS.get(str(source_change.get('after')), source_change.get('after'))}"
            "（本次评分、策略与指标的跳变可能源于数据源切换（字段口径不同），"
            "相关差异已列出但未计入重要变化）"
        )
    lines.extend(["", "## 变化明细", ""])
    for item in meaningful:
        field = item.get("field", "unknown")
        before = item.get("before")
        after = item.get("after")
        category = item.get("category", "other")
        lines.append(f"- [{category}] `{field}`：{before!s} → {after!s}")
    if not meaningful:
        lines.append("- 无")
    if source_suppressed:
        lines.extend(["", "## 数据源切换带来的差异（未计入重要变化）", ""])
        for item in source_suppressed:
            field = item.get("field", "unknown")
            category = item.get("category", "other")
            lines.append(f"- [{category}] `{field}`：{item.get('before')!s} → {item.get('after')!s}")
    lines.extend(
        [
            "",
            "---",
            "",
            "此通知用于研究复核，不构成投资建议或交易信号。",
            "",
        ]
    )
    return RenderedContent(
        format="markdown",
        body="\n".join(lines),
        metadata={"run_id": current_run_id, "ticker": ticker},
    )
