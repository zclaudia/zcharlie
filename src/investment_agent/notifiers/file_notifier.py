"""File notifier — writes RenderedContent to data/reports/YYYY-MM-DD/."""

import os
from datetime import date
from pathlib import Path

from investment_agent.domain.protocols import NotificationResult, RenderedContent


class FileNotifier:
    name = "file"

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)

    def target_path(
        self, content_format: str, channel_metadata: dict[str, object]
    ) -> Path:
        """Deterministic output path for one (date, kind, run_id) notification."""

        date_raw = channel_metadata.get("date")
        date_str = str(date_raw) if date_raw else date.today().isoformat()
        report_kind = str(channel_metadata.get("kind", "report"))
        run_id = channel_metadata.get("run_id", "unknown")
        ext = "md" if content_format == "markdown" else content_format
        return self._base_dir / date_str / f"{report_kind}_{run_id}.{ext}"

    async def send(
        self,
        content: RenderedContent,
        channel_metadata: dict[str, object],
    ) -> NotificationResult:
        out_path = self.target_path(content.format, channel_metadata)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        # Write atomically so a crash never leaves a truncated notification that
        # a later idempotency check would mistake for a delivered one.
        tmp_path = out_path.with_name(f".{out_path.name}.{os.getpid()}.tmp")
        tmp_path.write_text(content.body)
        os.replace(tmp_path, out_path)

        return NotificationResult(
            notifier_name=self.name,
            success=True,
            target=str(out_path),
        )
