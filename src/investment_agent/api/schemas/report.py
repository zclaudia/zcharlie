"""/reports endpoint schemas."""

from datetime import date

from pydantic import BaseModel


class ReportSummary(BaseModel):
    kind: str  # "discovery" or "attribution"
    date: date
    run_id: int | None
    markdown_path: str
    size_bytes: int


class ReportContent(BaseModel):
    summary: ReportSummary
    body: str
