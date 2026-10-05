"""Render meaningful research changes as a concise review notification."""

from investment_agent.domain.protocols import RenderedContent


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
    lines = [
        f"# 研究变化提醒：{ticker} {company_name}",
        "",
        f"当前运行：{current_run_id}",
        f"上一运行：{previous_run_id if previous_run_id is not None else '-'}",
        f"重要变化：{len(meaningful)} 项",
        "",
        "## 变化明细",
        "",
    ]
    for item in meaningful:
        field = item.get("field", "unknown")
        before = item.get("before")
        after = item.get("after")
        category = item.get("category", "other")
        lines.append(f"- [{category}] `{field}`：{before!s} → {after!s}")
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
