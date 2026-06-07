"""File notifier — writes RenderedContent to data/reports/YYYY-MM-DD/."""

from datetime import date
from pathlib import Path

from investment_agent.domain.protocols import NotificationResult, RenderedContent


class FileNotifier:
    name = "file"

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)

    async def send(
        self,
        content: RenderedContent,
        channel_metadata: dict[str, object],
    ) -> NotificationResult:
        date_raw = channel_metadata.get("date")
        date_str = str(date_raw) if date_raw else date.today().isoformat()
        report_kind = str(channel_metadata.get("kind", "report"))
        run_id = channel_metadata.get("run_id", "unknown")

        day_dir = self._base_dir / date_str
        day_dir.mkdir(parents=True, exist_ok=True)

        ext = "md" if content.format == "markdown" else content.format
        out_path = day_dir / f"{report_kind}_{run_id}.{ext}"
        out_path.write_text(content.body)

        return NotificationResult(
            notifier_name=self.name,
            success=True,
            target=str(out_path),
        )
