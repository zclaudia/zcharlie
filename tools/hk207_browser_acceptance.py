#!/usr/bin/env python3
"""Capture a real-browser HK2-07 financial page matrix from a running app.

Run after the reviewed-material importer has populated a scratch database:

  HK207_API_KEY=... PYTHONPATH=src .venv/bin/python tools/hk207_browser_acceptance.py \\
      --base-url http://127.0.0.1:8000 --require-imported \\
      --output-dir data/hk207-browser-acceptance

The runner uses the locally installed Chromium and the ``websockets`` package
for Chrome DevTools Protocol. It never inserts fixture data, calls a refresh
endpoint, or adds production debug routes. Add ``--case label=/app/stocks/CODE``
for additional normal-route state examples (for example, an empty company in a
separate scratch database). The JSON report records viewport overflow, source
state, periods, metrics, statuses, and all 16 default screenshots.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import json
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
from urllib.parse import quote, urljoin, urlsplit

import websockets

DEFAULT_CODES = (
    "00002.HK",  # CLP Holdings
    "00005.HK",  # HSBC
    "00700.HK",  # Tencent
    "AAPL",  # Apple
    "COST",  # Costco
    "JPM",  # JPMorgan Chase
    "XOM",  # Exxon Mobil
    "TSM",  # TSMC ADS
)
VIEWPORTS = ((1440, 1000, "desktop"), (390, 844, "mobile"))


def _browser_path(given: str | None) -> str:
    if given:
        path = Path(given).expanduser()
        if not path.is_file() or not os.access(path, os.X_OK):
            raise SystemExit(f"Chromium executable is not runnable: {path}")
        return str(path)
    candidates = (
        *Path.home().glob("Library/Caches/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-mac-arm64/chrome-headless-shell"),
        *Path.home().glob("Library/Caches/ms-playwright/chromium-*/chrome-mac-*/Chromium.app/Contents/MacOS/Chromium"),
        *Path.home().glob(".cache/ms-playwright/chromium_headless_shell-*/chrome-headless-shell-linux64/chrome-headless-shell"),
        *Path.home().glob(".cache/ms-playwright/chromium-*/chrome-linux*/chrome"),
    )
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise SystemExit("Chromium was not found; pass --browser /path/to/chromium")


class DevTools:
    def __init__(self, websocket_url: str) -> None:
        self.websocket_url = websocket_url
        self.socket: Any = None
        self.next_id = 1
        self.events: list[dict[str, Any]] = []

    async def __aenter__(self) -> DevTools:
        self.socket = await websockets.connect(
            self.websocket_url,
            origin="http://localhost",
            max_size=64 * 1024 * 1024,
            open_timeout=10,
            proxy=None,
        )
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.socket.close()

    async def call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        call_id = self.next_id
        self.next_id += 1
        await self.socket.send(json.dumps({"id": call_id, "method": method, "params": params or {}}))
        while True:
            message = json.loads(await self.socket.recv())
            if message.get("id") == call_id:
                if "error" in message:
                    raise RuntimeError(f"DevTools {method}: {message['error']}")
                return message.get("result", {})
            self.events.append(message)

    async def evaluate(self, expression: str) -> Any:
        result = await self.call(
            "Runtime.evaluate",
            {"expression": expression, "returnByValue": True, "awaitPromise": True},
        )
        remote = result.get("result", {})
        if remote.get("subtype") == "error":
            raise RuntimeError(remote.get("description", "browser evaluation failed"))
        return remote.get("value")

    async def wait_for(self, expression: str, timeout_s: float = 15) -> bool:
        deadline = time.monotonic() + timeout_s
        while time.monotonic() < deadline:
            if await self.evaluate(expression):
                return True
            await asyncio.sleep(0.15)
        return False


def _target_url(port: int) -> tuple[str, str]:
    with urllib.request.urlopen(f"http://127.0.0.1:{port}/json/list", timeout=4) as response:
        targets = json.load(response)
    page = next((target for target in targets if target.get("type") == "page"), None)
    if page is None:
        raise RuntimeError("Chromium did not expose a page target")
    return page["webSocketDebuggerUrl"], page["id"]


def _read_cases(args: argparse.Namespace) -> list[tuple[str, str]]:
    cases = [] if args.only_cases else [
        (code, f"/app/stocks/{quote(code, safe='.')}") for code in DEFAULT_CODES
    ]
    for raw in args.case:
        if "=" not in raw:
            raise SystemExit(f"invalid --case {raw!r}; use label=/app/stocks/CODE")
        label, path = raw.split("=", 1)
        if not label or not path.startswith("/app/stocks/"):
            raise SystemExit(f"invalid --case {raw!r}; use label=/app/stocks/CODE")
        cases.append((label, path))
    if not cases:
        raise SystemExit("--only-cases requires at least one --case")
    return cases


async def run(args: argparse.Namespace) -> dict[str, Any]:
    api_key = os.environ.get("HK207_API_KEY")
    if not api_key:
        raise SystemExit("Set HK207_API_KEY to the scratch app's API key")
    base_url = args.base_url.rstrip("/")
    parsed = urlsplit(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise SystemExit("--base-url must be an http(s) origin")
    browser = _browser_path(args.browser)
    output_dir = args.output_dir
    user_data = tempfile.TemporaryDirectory(prefix="hk207-chromium-")
    process = await asyncio.create_subprocess_exec(
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-first-run",
        "--no-default-browser-check",
        "--remote-allow-origins=*",
        "--remote-debugging-port=0",
        f"--user-data-dir={user_data.name}",
        "about:blank",
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        port_file = Path(user_data.name) / "DevToolsActivePort"
        deadline = time.monotonic() + 15
        while not port_file.exists() and time.monotonic() < deadline:
            if process.returncode is not None:
                raise RuntimeError(f"Chromium exited with code {process.returncode}")
            await asyncio.sleep(0.1)
        if not port_file.exists():
            raise RuntimeError("Chromium did not start its DevTools endpoint")
        port = int(port_file.read_text().splitlines()[0])
        websocket_url, _ = _target_url(port)
        cases = _read_cases(args)
        entries: list[dict[str, Any]] = []
        external_requests: set[str] = set()
        async with DevTools(websocket_url) as cdp:
            await cdp.call("Page.enable")
            await cdp.call("Network.enable")
            await cdp.call("Runtime.enable")
            # Authenticate through the real form and session cookie.
            first_url = urljoin(base_url + "/", "app/login?next=" + quote(cases[0][1], safe="/"))
            await cdp.call("Page.navigate", {"url": first_url})
            if not await cdp.wait_for("Boolean(document.querySelector('#api_key'))"):
                raise RuntimeError("login form did not load")
            await cdp.evaluate(
                "(() => { const input = document.querySelector('#api_key'); "
                f"if (!input) return false; input.value = {json.dumps(api_key)}; "
                "input.closest('form').requestSubmit(); return true; })()"
            )
            if not await cdp.wait_for("location.pathname !== '/app/login'"):
                raise RuntimeError("normal login form did not establish a session")
            await cdp.call("Page.navigate", {"url": urljoin(base_url + "/", "app/stocks/" + quote(DEFAULT_CODES[0], safe="."))})
            if not await cdp.wait_for("Boolean(document.querySelector('#stock-financials'))"):
                login_state = await cdp.evaluate("({url: location.href, title: document.title})")
                raise RuntimeError(f"login did not reach the financial page: {login_state}")
            login_state = await cdp.evaluate("({url: location.href, hasFinancial: !!document.querySelector('#stock-financials')})")
            if not login_state.get("hasFinancial"):
                raise RuntimeError(f"login did not reach the financial page: {login_state}")

            for label, path in cases:
                entry: dict[str, Any] = {"case": label, "path": path, "viewports": {}}
                for width, height, viewport_name in VIEWPORTS:
                    await cdp.call(
                        "Emulation.setDeviceMetricsOverride",
                        {
                            "width": width,
                            "height": height,
                            "deviceScaleFactor": 1,
                            "mobile": width < 600,
                        },
                    )
                    await cdp.call("Page.navigate", {"url": urljoin(base_url + "/", path.lstrip("/"))})
                    page_ready = await cdp.wait_for("Boolean(document.querySelector('#stock-financials'))", timeout_s=20)
                    if not page_ready:
                        raise RuntimeError(f"{label}: financial page did not load at {path}")
                    await asyncio.sleep(0.2)
                    details = await cdp.evaluate("""(() => {
                      const text = (selector) => document.querySelector(selector)?.textContent?.trim() ?? null;
                      const root = document.documentElement;
                      const tableWraps = [...document.querySelectorAll('#stock-financials .table-wrap')].map((el) => ({
                        clientWidth: el.clientWidth, scrollWidth: el.scrollWidth,
                        contained: el.scrollWidth <= el.clientWidth + 1,
                      }));
                      return {
                        title: document.querySelector('.stock-name')?.textContent?.trim() ?? null,
                        code: document.querySelector('.stock-code')?.textContent?.trim() ?? null,
                        market: text('#financial-market'), securityType: text('#financial-security-type'),
                        quoteCurrency: text('#financial-quote-currency'), reportCurrency: text('#financial-report-currency'),
                        periods: [...document.querySelectorAll('#financial-fiscal-years .financial-year-end')].map((el) => el.textContent.trim()),
                        facts: [...document.querySelectorAll('#stock-financials .financial-fact:not(.financial-fact-absent)')].map((el) => ({
                          metric: el.dataset.metric, periodEnd: el.dataset.periodEnd,
                          amount: el.querySelector('.financial-amount')?.textContent?.trim() ?? null,
                          currency: el.querySelector('.financial-unit')?.textContent?.trim() ?? null,
                          verification: el.querySelector('.verify')?.textContent?.trim() ?? null,
                          conflict: el.querySelector('.badge.warn')?.textContent?.trim() ?? null,
                        })),
                        factCount: document.querySelectorAll('#stock-financials .financial-fact:not(.financial-fact-absent)').length,
                        statuses: [...document.querySelectorAll('.financial-status')].map((el) => el.dataset.statusCode),
                        latestness: document.querySelector('#financial-freshness [data-latestness]')?.dataset.latestness ?? null,
                        dates: Object.fromEntries(['last-verified-on','latestness-checked-on','last-imported-at','last-requested-at','last-attempt-at','last-success-at'].map((key) => [key, text(`#financial-${key}`)])),
                        sources: [...document.querySelectorAll('.financial-source-control')].map((el) => ({enabled: el.dataset.sourceEnabled, text: el.textContent.trim()})),
                        metrics: [...document.querySelectorAll('.financial-metric')].map((el) => ({key:el.dataset.metric,status:el.dataset.metricStatus,evidence:el.querySelector('.financial-metric-evidence')?.dataset.evidenceStatus ?? null})),
                        refreshButton: Boolean(document.querySelector('#financial-refresh-button')),
                        refreshHint: text('#financial-refresh-hint'),
                        viewport: {width: window.innerWidth, height: window.innerHeight, documentWidth: root.scrollWidth, documentHeight: root.scrollHeight, noHorizontalOverflow: root.scrollWidth <= window.innerWidth + 1},
                        tableWraps
                      };
                    })()""")
                    if args.require_imported and (
                        not details["periods"] or "0 项事实" in (await cdp.evaluate("document.querySelector('#financial-material-coverage')?.textContent ?? ''"))
                    ):
                        raise RuntimeError(f"{label}: expected imported reviewed material; no periods/facts rendered")
                    if not details["viewport"]["noHorizontalOverflow"]:
                        raise RuntimeError(f"{label} {viewport_name}: page overflows horizontally ({details['viewport']})")
                    shot = output_dir / f"{label}.{viewport_name}.png"
                    layout = await cdp.call("Page.getLayoutMetrics")
                    content_size = layout.get("cssContentSize") or layout.get("contentSize") or {}
                    content_width = max(width, int(content_size.get("width", width)))
                    content_height = max(height, int(content_size.get("height", height)))
                    image = await cdp.call(
                        "Page.captureScreenshot",
                        {
                            "format": "png",
                            "captureBeyondViewport": True,
                            "fromSurface": True,
                            "clip": {"x": 0, "y": 0, "width": content_width, "height": content_height, "scale": 1},
                        },
                    )
                    shot.write_bytes(base64.b64decode(image["data"]))
                    details["screenshot"] = str(shot)
                    entry["viewports"][viewport_name] = details
                entries.append(entry)
            await cdp.call("Browser.getVersion")
            request_urls = {
                event.get("params", {}).get("requestId"): event.get("params", {}).get("request", {}).get("url")
                for event in cdp.events
                if event.get("method") == "Network.requestWillBeSent"
            }
            javascript_exceptions: list[dict[str, Any]] = []
            console_errors: list[dict[str, Any]] = []
            network_failures: list[dict[str, Any]] = []
            http_errors: list[dict[str, Any]] = []
            for event in cdp.events:
                method = event.get("method")
                params = event.get("params", {})
                if method == "Runtime.exceptionThrown":
                    exception = params.get("exceptionDetails", {})
                    javascript_exceptions.append({
                        "text": exception.get("text"),
                        "description": exception.get("exception", {}).get("description"),
                        "url": exception.get("url"),
                        "lineNumber": exception.get("lineNumber"),
                    })
                elif method == "Runtime.consoleAPICalled" and params.get("type") == "error":
                    console_errors.append({
                        "text": " ".join(str(item.get("value", item.get("description", ""))) for item in params.get("args", [])),
                        "url": params.get("stackTrace", {}).get("callFrames", [{}])[0].get("url"),
                    })
                elif method == "Network.loadingFailed":
                    network_failures.append({
                        "requestId": params.get("requestId"),
                        "url": request_urls.get(params.get("requestId")),
                        "errorText": params.get("errorText"),
                        "blockedReason": params.get("blockedReason"),
                        "canceled": params.get("canceled", False),
                    })
                elif method == "Network.responseReceived":
                    response = params.get("response", {})
                    if response.get("status", 200) >= 400:
                        http_errors.append({
                            "url": response.get("url"),
                            "status": response.get("status"),
                            "statusText": response.get("statusText"),
                            "resourceType": params.get("type"),
                        })
                if event.get("method") == "Network.requestWillBeSent":
                    request_url = event.get("params", {}).get("request", {}).get("url", "")
                    request_origin = urlsplit(request_url)
                    if request_origin.netloc and request_origin.netloc != parsed.netloc:
                        external_requests.add(request_url)
        report = {
            "base_url": base_url,
            "cases": entries,
            "external_requests": sorted(external_requests),
            "javascript_exceptions": javascript_exceptions,
            "console_errors": console_errors,
            "network_failures": network_failures,
            "http_errors": http_errors,
            "screenshots": sum(len(row["viewports"]) for row in entries),
            "require_imported": args.require_imported,
        }
        report_path = output_dir / "report.json"
        report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if external_requests:
            raise RuntimeError(f"unexpected external browser requests: {sorted(external_requests)}")
        return report
    finally:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=5)
        except TimeoutError:
            process.kill()
            await process.wait()
        user_data.cleanup()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="origin of the actual scratch web app")
    parser.add_argument("--output-dir", default="data/hk207-browser-acceptance")
    parser.add_argument("--browser", help="Chromium executable; auto-detected from Playwright cache by default")
    parser.add_argument("--require-imported", action="store_true", help="fail if any case lacks periods/facts")
    parser.add_argument("--only-cases", action="store_true", help="capture only explicit --case pages; useful for a separate state database")
    parser.add_argument("--case", action="append", default=[], metavar="LABEL=/app/stocks/CODE")
    args = parser.parse_args()
    args.output_dir = Path(args.output_dir).expanduser().resolve()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        report = asyncio.run(run(args))
    except Exception as exc:
        print(f"HK2-07 browser acceptance failed: {exc}", file=sys.stderr)
        return 1
    print(json.dumps({"report": str(Path(args.output_dir).expanduser().resolve() / "report.json"), "screenshots": report["screenshots"], "cases": len(report["cases"])}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
