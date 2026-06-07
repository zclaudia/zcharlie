"""ia api serve."""

import typer
import uvicorn

app = typer.Typer(help="API server commands")


@app.command("serve")
def serve(
    host: str = "0.0.0.0",  # noqa: S104 — bind all interfaces (Docker)
    port: int = 8000,
    reload: bool = False,
    log_level: str = "info",
) -> None:
    """Start the FastAPI server (uvicorn)."""
    uvicorn.run(
        "investment_agent.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level=log_level,
    )
