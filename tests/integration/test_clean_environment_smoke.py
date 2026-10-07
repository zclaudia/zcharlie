"""Clean-environment MVP smoke (work package E1 / MVP-404).

Starts from an empty temporary directory and exercises the documented
deployment flow end to end, fully offline:

1. fresh SQLite database via ``ia migrate up`` (DATABASE_URL-driven);
2. API process starts and answers /healthz;
3. watchlist item is added through the API and a batch refresh is queued;
4. a one-shot worker completes a run against a controlled stub ifind-proxy
   serving sanitized golden fixtures (the stub rejects LLM calls with a
   terminal 400, so the run finishes ``partial`` with the deterministic result preserved);
5. result, baseline diff, artifacts and the web page are all readable;
6. after restarting the API the run state is still queryable.

No real iFinD and no real LLM is contacted.
"""

import json  # noqa: E402
import os  # noqa: E402
import shutil  # noqa: E402
import socket  # noqa: E402
import subprocess  # noqa: E402
import sys  # noqa: E402
import time  # noqa: E402
from pathlib import Path  # noqa: E402

import httpx  # noqa: E402
import pytest  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[2]
FIXTURE_RAW = (
    REPO_ROOT / "tests" / "fixtures" / "research_kernel" / "600519" / "provider_raw.json"
)


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


class StubIfindProxy:
    """Serves the sanitized 600519 fixture bodies for any research request."""

    def __init__(self) -> None:
        raw = json.loads(FIXTURE_RAW.read_text(encoding="utf-8"))
        self.bodies = {name: entry["body"] for name, entry in raw["requests"].items()}
        self.port = _free_port()

    def _resolve(self, path: str, payload: dict[str, object]) -> str:
        if path == "/stock/info":
            return "info"
        if path == "/stock/history":
            return "history"
        if path == "/financial/statements":
            return "income_latest" if payload.get("report_date") == "20251231" else "income_base"
        if path == "/financial/index":
            if payload.get("category") == "capital_structure":
                return "capital_latest"
            return f"profitability_{str(payload.get('report_date', ''))[:4]}"
        raise ValueError(f"unexpected stub path {path}")

    def start(self) -> None:
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("content-length", "0"))
                payload = json.loads(self.rfile.read(length) or b"{}")
                if self.path.startswith("/v1/"):
                    error = json.dumps(
                        {"error": {"message": "stub LLM rejects requests", "type": "invalid_request_error"}}
                    ).encode("utf-8")
                    self.send_response(400)
                    self.send_header("content-type", "application/json")
                    self.send_header("content-length", str(len(error)))
                    self.end_headers()
                    self.wfile.write(error)
                    return
                name = proxy._resolve(self.path, payload)
                body = json.dumps(proxy.bodies[name]).encode("utf-8")
                self.send_response(200)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                return

        self.server = ThreadingHTTPServer(("127.0.0.1", self.port), Handler)
        self.thread = None
        import threading

        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def stop(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _ia_binary() -> str:
    candidate = Path(sys.executable).parent / "ia"
    if candidate.is_file():
        return str(candidate)
    found = shutil.which("ia")
    if found is None:
        raise RuntimeError("ia console script not found; install the package first")
    return found


def _smoke_env(tmp_path: Path, db_path: Path, stub: StubIfindProxy, api_port: int) -> dict[str, str]:
    env = {**os.environ}
    env.update(
        {
            "DATABASE_URL": f"sqlite+aiosqlite:///{db_path}",
            "DATA_DIR": str(tmp_path / "data"),
            "API_KEY": "smoke-secret",
            "LLM_TIER3_API_KEY": "stub",
            # The stub rejects every LLM request with a terminal 400, so the
            # deterministic scoring must survive with a partial outcome.
            # (An unreachable LLM would be a transient error and be retried.)
            "LLM_TIER3_BASE_URL": f"http://127.0.0.1:{stub.port}/v1",
            "IFIND_PROXY_URL": f"http://127.0.0.1:{stub.port}",
            "IFIND_PROXY_API_KEY": "stub-proxy-key",
        }
    )
    return env


def _wait_for_health(client: httpx.Client, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            response = client.get("/healthz")
            if response.status_code == 200:
                return
        except httpx.HTTPError as exc:  # connection not ready yet
            last_error = exc
        time.sleep(0.3)
    raise RuntimeError(f"API did not become healthy: {last_error}")


@pytest.mark.slow
def test_clean_environment_deploy_research_and_restart(tmp_path: Path) -> None:
    db_path = tmp_path / "smoke" / "app.db"
    db_path.parent.mkdir(parents=True)
    stub = StubIfindProxy()
    stub.start()
    api_port = _free_port()
    env = _smoke_env(tmp_path, db_path, stub, api_port)
    base_url = f"http://127.0.0.1:{api_port}"

    api_process = None
    try:
        # Step 1-2: documented migration into an empty database.
        migrated = subprocess.run(
            [_ia_binary(), "migrate", "up"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert migrated.returncode == 0, migrated.stderr[-2000:]

        # Step 3: API starts and answers /healthz.
        api_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "investment_agent.api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            _wait_for_health(client)

            # Step 4: add a watchlist item and queue a batch refresh.
            auth = {"X-API-Key": "smoke-secret"}
            added = client.post(
                "/watchlist",
                headers=auth,
                json={
                    "ticker": "600519.SH",
                    "industry": "白酒",
                    "name": "贵州茅台",
                    "report_year": 2025,
                    # Exercises the iFinD deep-research path against the stub proxy.
                    "provider": "ifind",
                },
            )
            assert added.status_code == 201, added.text

            queued = client.post(
                "/watchlist/refresh",
                headers={**auth, "Idempotency-Key": "smoke-refresh-1"},
                json={},
            )
            assert queued.status_code == 202, queued.text
            assert queued.json()["created_runs"] == 1
            run_id = queued.json()["items"][0]["run_id"]

            # Step 5: one-shot worker completes the run against the stub proxy.
            worker = subprocess.run(
                [_ia_binary(), "worker", "start", "--once"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                timeout=180,
            )
            assert worker.returncode == 0, (worker.stdout or "")[-2000:] + (worker.stderr or "")[-2000:]

            detail = client.get(f"/research/runs/{run_id}", headers=auth).json()
            assert detail["status"] == "partial", detail["status"]
            assert detail["result"] is not None, "deterministic result must survive a partial run"
            # The chain persists the bare score item (no {"items": [...]} wrapper).
            assert detail["result"]["score"]["investment_score"] is not None
            assert detail["result"]["explanation"] is None
            assert detail["change"]["baseline"] is True
            artifact_names = {item["name"] for item in detail["result"]["artifacts"]}
            assert {"pool", "market", "financial"} <= artifact_names, artifact_names

            # Step 6: web session reads the stock page with the result.
            login = client.post(
                "/app/login",
                data={"api_key": "smoke-secret", "next": "/app"},
                follow_redirects=True,
            )
            assert login.status_code == 200
            page = client.get("/app/stocks/600519.SH")
            assert page.status_code == 200
            assert "研究辅助，非投资建议" in page.text

        # Step 7: restart the API; the run state must still be queryable.
        api_process.terminate()
        api_process.wait(timeout=15)
        api_process = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "uvicorn",
                "investment_agent.api.app:create_app",
                "--factory",
                "--host",
                "127.0.0.1",
                "--port",
                str(api_port),
            ],
            cwd=REPO_ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
        )
        with httpx.Client(base_url=base_url, timeout=10.0) as client:
            _wait_for_health(client)
            detail_after = client.get(f"/research/runs/{run_id}", headers={"X-API-Key": "smoke-secret"}).json()
            assert detail_after["status"] == "partial"
            assert detail_after["result"]["score"]["investment_score"] is not None
    finally:
        if api_process is not None and api_process.poll() is None:
            api_process.terminate()
            try:
                api_process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                api_process.kill()
        stub.stop()
