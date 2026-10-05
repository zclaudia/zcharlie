"""Research validation commands."""

import asyncio
import os
from datetime import date
from pathlib import Path

import typer
from rich import print as rprint

app = typer.Typer(help="Evidence-first research workflows")
_DEFAULT_OUTPUT_ROOT = Path("data/research-runs")


@app.command("ifind")
def run_ifind_research(
    ticker: str = typer.Argument(..., help="A-share ticker, e.g. 600406.SH"),
    industry: str = typer.Option(..., help="Canonical industry used by classification"),
    report_year: int = typer.Option(..., min=1990, help="Latest complete annual report year"),
    as_of: str = typer.Option(date.today().isoformat(), help="Research date (YYYY-MM-DD)"),
    proxy_url: str = typer.Option(
        "http://127.0.0.1:7100",
        envvar="IFIND_PROXY_URL",
        help="iFinD proxy base URL",
    ),
    proxy_api_key: str | None = typer.Option(
        None,
        envvar="IFIND_PROXY_API_KEY",
        help="iFinD proxy API key (prefer environment variable)",
        show_default=False,
    ),
    explanation_model: str | None = typer.Option(
        None,
        envvar="IFIND_EXPLANATION_MODEL",
        help="Optional tier-3 model override for the explanation Agent",
        show_default=False,
    ),
    idempotency_key: str | None = typer.Option(
        None,
        help="Optional retry-safe key; a completed run is replayed without new calls",
        show_default=False,
    ),
    output_root: Path = typer.Option(  # noqa: B008
        _DEFAULT_OUTPUT_ROOT,
        help="Persistent research artifact root",
    ),
) -> None:
    """Validate iFinD -> canonical data -> deterministic score -> agent report."""

    from investment_agent.config import Settings
    from investment_agent.research.ifind_chain import run_ifind_research_chain
    from investment_agent.storage.database import init_engine

    key = proxy_api_key or os.getenv("PROXY_API_KEY", "")
    if not key:
        raise typer.BadParameter(
            "set IFIND_PROXY_API_KEY or pass --proxy-api-key",
            param_hint="--proxy-api-key",
        )
    if idempotency_key is not None and (
        not idempotency_key.strip() or len(idempotency_key) > 128
    ):
        raise typer.BadParameter(
            "must contain 1 to 128 characters",
            param_hint="--idempotency-key",
        )
    settings = Settings()  # type: ignore[call-arg]
    if explanation_model:
        settings = settings.model_copy(update={"llm_tier3_model": explanation_model})
    init_engine(settings.database_url)
    result = asyncio.run(
        run_ifind_research_chain(
            settings=settings,
            proxy_url=proxy_url,
            proxy_api_key=key,
            ticker=ticker,
            industry=industry,
            report_year=report_year,
            as_of=date.fromisoformat(as_of),
            output_root=output_root,
            idempotency_key=idempotency_key,
        )
    )
    rprint(f"[green]ResearchRun {result.run_id} complete[/green]")
    rprint(f"Score: {result.score['business_score']}")
    rprint(f"Workflow: {result.score['suggestion']}")
    rprint(f"Artifacts: {result.artifact_dir.resolve()}")
