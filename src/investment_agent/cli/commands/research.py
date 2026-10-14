"""Research validation commands."""

import os
from datetime import date
from pathlib import Path

import typer
from rich import print as rprint

from investment_agent.cli.runtime import bootstrap_settings

app = typer.Typer(help="Evidence-first research workflows")
_DEFAULT_OUTPUT_ROOT = Path("data/research-runs")


@app.command("ifind")
def run_ifind_research(
    ticker: str = typer.Argument(..., help="A-share ticker, e.g. 600406.SH"),
    industry: str = typer.Option(..., help="Canonical industry used by classification"),
    report_year: int = typer.Option(..., min=1990, help="Latest complete annual report year"),
    as_of: str = typer.Option(date.today().isoformat(), help="Research date (YYYY-MM-DD)"),
    proxy_url: str | None = typer.Option(
        None,
        envvar="IFIND_PROXY_URL",
        help="iFinD proxy base URL (default: the ifind_proxy_url setting)",
        show_default=False,
    ),
    proxy_api_key: str | None = typer.Option(
        None,
        envvar="IFIND_PROXY_API_KEY",
        help="iFinD proxy API key (default: the ifind_proxy_api_key setting)",
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
    """Manual deep research: iFinD -> canonical data -> deterministic score -> agent report.

    Every proxy call is counted in the iFinD budget ledger (P1-A13). Daily
    watchlist research uses ``ia research snapshot`` (free sources) instead.
    """

    from investment_agent.research.ifind_chain import (
        ResearchChainResult,
        ResearchPartialError,
        run_ifind_research_chain,
    )
    from investment_agent.settings.loader import load_settings
    from investment_agent.storage.database import run_with_engine

    if idempotency_key is not None and (
        not idempotency_key.strip() or len(idempotency_key) > 128
    ):
        raise typer.BadParameter(
            "must contain 1 to 128 characters",
            param_hint="--idempotency-key",
        )
    bootstrap = bootstrap_settings()

    async def main() -> ResearchChainResult:
        settings = await load_settings(base=bootstrap)
        if explanation_model:
            settings = settings.model_copy(update={"llm_tier3_model": explanation_model})
        key = proxy_api_key or os.getenv("PROXY_API_KEY", "") or settings.ifind_proxy_api_key
        if not key:
            raise typer.BadParameter(
                "configure ifind_proxy_api_key (settings page / `ia settings set`), "
                "set IFIND_PROXY_API_KEY or pass --proxy-api-key",
                param_hint="--proxy-api-key",
            )
        return await run_ifind_research_chain(
            settings=settings,
            proxy_url=proxy_url or settings.ifind_proxy_url,
            proxy_api_key=key,
            ticker=ticker,
            industry=industry,
            report_year=report_year,
            as_of=date.fromisoformat(as_of),
            output_root=output_root,
            idempotency_key=idempotency_key,
        )

    try:
        result = run_with_engine(bootstrap.database_url, main)
    except ResearchPartialError as exc:
        rprint(f"[yellow]ResearchRun {exc.run_id} partial[/yellow]: {exc.reason}")
        raise typer.Exit(code=2) from exc
    rprint(f"[green]ResearchRun {result.run_id} complete[/green]")
    rprint(f"Score: {result.score['business_score']}")
    rprint(f"Workflow: {result.score['suggestion']}")
    rprint(f"Artifacts: {result.artifact_dir.resolve()}")


@app.command("snapshot")
def run_snapshot_research(
    ticker: str = typer.Argument(..., help="A-share ticker, e.g. 600519.SH"),
    as_of: str = typer.Option(date.today().isoformat(), help="Research date (YYYY-MM-DD)"),
    industry: str | None = typer.Option(
        None,
        help="Optional user industry text (overrides the Shenwan / CSRC industry group)",
        show_default=False,
    ),
    report_year: int | None = typer.Option(
        None,
        min=1990,
        help="Expected annual report year (informational; the latest public one is used)",
        show_default=False,
    ),
    idempotency_key: str | None = typer.Option(
        None,
        help="Optional retry-safe key; a completed run is replayed",
        show_default=False,
    ),
    output_root: Path = typer.Option(  # noqa: B008
        _DEFAULT_OUTPUT_ROOT,
        help="Persistent research artifact root",
    ),
) -> None:
    """Free-source research: snapshot store -> canonical data -> score -> agent report.

    No iFinD call (P1-B5). Missing free-source data queues an iFinD
    verification request (trigger ``missing``) and ends the run partial.
    """

    from investment_agent.research.ifind_chain import (
        ResearchChainResult,
        ResearchPartialError,
        run_ifind_research_chain,
    )
    from investment_agent.settings.loader import load_settings
    from investment_agent.storage.database import run_with_engine

    bootstrap = bootstrap_settings()

    async def main() -> ResearchChainResult:
        return await run_ifind_research_chain(
            settings=await load_settings(base=bootstrap),
            proxy_url="",
            proxy_api_key="",
            ticker=ticker.upper(),
            industry=industry,
            report_year=report_year,
            as_of=date.fromisoformat(as_of),
            output_root=output_root,
            idempotency_key=idempotency_key,
            source_mode="snapshot",
        )

    try:
        result = run_with_engine(bootstrap.database_url, main)
    except ResearchPartialError as exc:
        rprint(f"[yellow]ResearchRun {exc.run_id} partial[/yellow]: {exc.reason}")
        raise typer.Exit(code=2) from exc
    rprint(f"[green]ResearchRun {result.run_id} complete[/green]")
    rprint(f"Score: {result.score['business_score']}")
    rprint(f"Workflow: {result.score['suggestion']}")
    rprint(f"Artifacts: {result.artifact_dir.resolve()}")
