"""ia api serve."""

import typer
import uvicorn

app = typer.Typer(help="API server commands")


@app.command("serve")
def serve(
    # 工程基线：默认只绑定本机；远程暴露（如 Docker）必须显式传 --host 0.0.0.0。
    host: str = "127.0.0.1",
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
