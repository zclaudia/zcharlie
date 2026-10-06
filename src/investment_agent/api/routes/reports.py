"""/reports endpoints — list + read archived markdown reports."""

from datetime import date
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from investment_agent.api.auth import verify_api_key
from investment_agent.api.schemas.report import ReportContent, ReportSummary
from investment_agent.config import Settings

router = APIRouter(
    prefix="/reports",
    tags=["reports"],
    dependencies=[Depends(verify_api_key)],
)


def _reports_dir() -> Path:
    return Settings().data_dir / "reports"  # type: ignore[call-arg]


def _parse(p: Path) -> ReportSummary | None:
    """Parse path like data/reports/2025-06-03/discovery_42.md → ReportSummary."""
    try:
        d = date.fromisoformat(p.parent.name)
        stem = p.stem  # 'discovery_42' or 'attribution_AAPL_15'
        kind = "discovery" if stem.startswith("discovery_") else "attribution"
        parts = stem.split("_")
        try:
            run_id: int | None = int(parts[-1])
        except ValueError:
            run_id = None
        return ReportSummary(
            kind=kind,
            date=d,
            run_id=run_id,
            markdown_path=str(p),
            size_bytes=p.stat().st_size,
        )
    except (ValueError, OSError):
        return None


def _list_reports(pattern: str, limit: int) -> list[ReportSummary]:
    """Newest-first reports, scanning one day directory at a time.

    Reports live at ``reports/<YYYY-MM-DD>/<kind>_*.md``; walking day
    directories newest-first and stopping at ``limit`` avoids a full recursive
    scan of the archive on every request.
    """

    base = _reports_dir()
    if not base.is_dir():
        return []
    day_dirs: list[Path] = []
    for entry in base.iterdir():
        try:
            date.fromisoformat(entry.name)
        except ValueError:
            continue
        if entry.is_dir():
            day_dirs.append(entry)
    out: list[ReportSummary] = []
    for day_dir in sorted(day_dirs, reverse=True):
        for p in sorted(day_dir.glob(pattern), reverse=True):
            s = _parse(p)
            if s is not None:
                out.append(s)
                if len(out) >= limit:
                    return out
    return out


@router.get("/discovery", response_model=list[ReportSummary])
async def list_discovery_reports(
    limit: int = Query(30, ge=1, le=200),
) -> list[ReportSummary]:
    return _list_reports("discovery_*.md", limit)


@router.get("/discovery/{day}", response_model=ReportContent)
async def get_discovery_report(day: date) -> ReportContent:
    base = _reports_dir() / day.isoformat()
    if not base.exists():
        raise HTTPException(status_code=404, detail=f"No reports for {day}")
    files = sorted(base.glob("discovery_*.md"), reverse=True)
    if not files:
        raise HTTPException(
            status_code=404,
            detail=f"No discovery report for {day}",
        )
    p = files[0]
    summary = _parse(p)
    assert summary is not None
    return ReportContent(summary=summary, body=p.read_text())


@router.get("/attribution", response_model=list[ReportSummary])
async def list_attribution_reports(
    limit: int = Query(30, ge=1, le=200),
) -> list[ReportSummary]:
    return _list_reports("attribution_*.md", limit)
