# Investment Agent

Personal multi-strategy stock analysis system: daily Discovery (NASDAQ-100 multi-strategy screening) + on-demand Attribution (why did X move?). Phase A — analysis only, no trading.

## Features

- **3 strategies** out of the box (`value`, `growth`, `risk_aggressive`) — YAML-defined screening rules + Jinja2 prompts, versioned per strategy.
- **Discovery pipeline** — two-tier LLM scoring (Tier 2 score-all → Tier 3 deep-analyze top N) over a universe, with per-strategy thresholds.
- **Attribution pipeline** — explains a single ticker's recent move via β-adjusted excess return + structured LLM reasoning over fundamentals, news, and tracked auxiliary assets.
- **Auxiliary tracking** — automatic discovery + manual curation of related tickers/indices/commodities used to enrich Attribution context.
- **Markdown reports** — every Discovery and Attribution run archives a human-readable `.md` to `data/reports/YYYY-MM-DD/`.
- **Dual entry points** — full-featured Typer CLI (`ia ...`) and FastAPI HTTP service (Swagger at `/docs`), backed by APScheduler for unattended daily Discovery.
- **One-command Docker deploy** — `docker compose up -d` brings up API + scheduler; SQLite + alembic-managed schema.

## Quick start (Mac dev)

```bash
# 1. Clone + install
git clone <repo> && cd zcharlie
python3.13 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# 2. Configure
cp .env.example .env
# Edit .env: set API_KEY (any random string) + real LLM_TIER2_API_KEY + LLM_TIER3_API_KEY

# 3. Initialize DB + data
ia migrate up
ia data seed                            # seed markets + universe
ia data backfill --universe NASDAQ_100  # pull 30d prices + fundamentals
ia data backfill --tickers SPY,QQQ      # benchmark for Attribution
ia strategy sync                        # load 3 strategies from strategies/*.yaml

# 4. Run a Discovery
ia pipeline discover --universe NASDAQ_100
cat data/reports/$(date +%Y-%m-%d)/discovery_*.md

# 5. Run an Attribution
ia attribute run AAPL
cat data/reports/$(date +%Y-%m-%d)/attribution_AAPL_*.md
```

## Daily workflow

The APScheduler container triggers Discovery on the configured cron (default `30 16 * * 1-5` America/New_York — 30 min after US close). Each run produces a Markdown report archived under `data/reports/YYYY-MM-DD/` for operator review. Attribution is on-demand: invoke via `ia attribute run <TICKER>` or `POST /attribute/{ticker}` whenever a name needs explaining.

## CLI reference

All CLI verbs live under the `ia` entry point, grouped by domain:

| Group | Commands | Purpose |
|---|---|---|
| `ia data` | `seed`, `backfill` | yfinance ingestion |
| `ia strategy` | `list`, `show`, `sync` | Strategy YAML management |
| `ia pipeline` | `discover`, `list`, `show` | Discovery pipeline |
| `ia attribute` | `run`, `show` | Attribution pipeline |
| `ia research` | `ifind` | iFinD → canonical data → deterministic score → cited Agent report |
| `ia auxiliary` | `list`, `add`, `remove`, `refresh` | Auxiliary tracking |
| `ia admin` | `cost` | LLM monthly cost |
| `ia api` | `serve` | FastAPI server |
| `ia scheduler` | `start` | APScheduler |
| `ia migrate` | `up`, `down`, `current`, `history` | alembic wrapper |

Use `ia <group> --help` to see options.

### iFinD vertical-slice validation

The `research ifind` command validates the evidence-first A-share path against a
running `ifind-proxy`. It persists raw and canonical inputs, invokes the
repository-owned deterministic research kernel, records the run and LLM call in the
application database, and writes an Agent explanation whose claims cite canonical
score fields.

```bash
export IFIND_PROXY_API_KEY=replace_with_proxy_key
ia research ifind 600406.SH \
  --industry 电力设备 \
  --report-year 2025 \
  --explanation-model deepseek-flash
```

Artifacts are written under `data/research-runs/<run_id>/`. This validation path
does not place trades and does not turn LLM output into numeric scores.

The accepted product and engineering boundaries are documented in
[`docs/product/engineering-baseline.md`](docs/product/engineering-baseline.md), with
the MVP-to-final delivery sequence in
[`docs/product/delivery-roadmap.md`](docs/product/delivery-roadmap.md).

## HTTP API

The API exposes the same operations as the CLI, plus read-only access for future Web/Mobile clients. All endpoints require header `X-API-Key: <your-api-key>` except `/healthz`.

Launch: `ia api serve` (default port 8000). Then visit:
- http://localhost:8000/docs — Swagger UI (auto-generated OpenAPI)
- http://localhost:8000/redoc — ReDoc UI

| Endpoint Group | Methods | Purpose |
|---|---|---|
| `/healthz` | GET | Liveness probe (no auth) |
| `/pipelines` | POST `/discovery/run`, GET `/`, GET `/{id}`, GET `/{id}/scores` | Discovery |
| `/attribute/{ticker}` | POST, GET `/latest`, GET `/runs/{id}` | Attribution |
| `/reports` | GET `/discovery`, GET `/discovery/{date}`, GET `/attribution` | Archived reports |
| `/listings/{ticker}` | GET, GET `/scores`, GET/POST `/relationships` | Listing inspection |
| `/strategies` | GET `/`, GET `/{code}` | Strategy catalog |
| `/universes` | GET `/` | Universe catalog |
| `/watchlist` | GET, POST, DELETE `/{listing_id}` | Durable watchlist with A-share research metadata |
| `/research/single-stock` | POST | Queue an A-share research run and return HTTP 202 + `run_id` |
| `/research/runs` | GET | Paginated research-run list with optional status filter |
| `/research/runs/{id}` | GET | Lifecycle detail plus optional versioned result |
| `/research/runs/{id}/artifacts` | GET | List and download audited run artifacts |
| `/admin/llm-calls` | GET, GET `/cost` | LLM audit |

Add an A-share directly with its exchange suffix. If a prior research snapshot
exists, `name`, `industry`, and `report_year` are inferred from the latest snapshot.
Otherwise, `name` and `industry` are required when the listing is first created.

```bash
curl -X POST http://localhost:8000/watchlist \
  -H "X-API-Key: $API_KEY" \
  -H "Content-Type: application/json" \
  -d '{"ticker":"600406.SH","name":"国电南瑞","industry":"电力设备","provider":"ifind","report_year":2025}'
```

## Configuration

### `.env` — secrets and runtime

| Variable | Purpose |
|---|---|
| `API_KEY` | Shared secret for HTTP `X-API-Key` header |
| `DATABASE_URL` | SQLAlchemy async URL, default `sqlite+aiosqlite:///./data/app.db` |
| `LLM_TIER2_BASE_URL` / `LLM_TIER2_API_KEY` / `LLM_TIER2_MODEL` | Mid-tier model for scoring (e.g. DeepSeek) |
| `LLM_TIER3_BASE_URL` / `LLM_TIER3_API_KEY` / `LLM_TIER3_MODEL` | Top-tier model for deep analysis (e.g. GPT-5) |
| `YFINANCE_MAX_CONCURRENT` | Cap on concurrent yfinance fetches |
| `LLM_MAX_CONCURRENT` | Cap on concurrent LLM calls |
| `DATA_DIR` | Root for `app.db` and `data/reports/` |
| `LOG_LEVEL` | `DEBUG` / `INFO` / `WARNING` |
| `IFIND_PROXY_URL` / `IFIND_PROXY_API_KEY` | Standalone iFinD data gateway |

Run the durable research worker separately with `ia worker start`. For local smoke
tests, `ia worker start --once` recovers stale work and processes at most one job.
If deterministic scoring succeeds but the explanation model fails, the run is
`partial`: `/research/runs/{id}` still returns the score and deterministic artifacts
with `explanation: null` for explicit review.

### `config/app.yaml` — universes, strategies, pipeline, scheduler

```yaml
universes:
  - code: NASDAQ_100
    refresh_membership_freq: weekly
enabled_strategies: [value, growth, risk_aggressive]
pipeline:
  tier2_score_threshold: 40
  tier3_top_n: 10
  deep_analysis_cache_days: 90
scheduler:
  discovery_cron: "30 16 * * 1-5"
  discovery_timezone: America/New_York
auxiliary_tracking:
  strength_threshold: 0.5
  max_per_company: 5
  total_cap_multiplier: 2
```

### `strategies/*.yaml` — per-strategy config

One file per strategy: ScreeningRule + prompt path + version. See existing `value.yaml`, `growth.yaml`, `risk_aggressive.yaml`. Run `ia strategy sync` after editing to upsert into the DB.

### `prompts/` — Jinja2 templates

- `prompts/strategies/<code>/scoring.j2` — Tier 2 scoring prompt
- `prompts/strategies/<code>/deep.j2` — Tier 3 deep analysis prompt
- `prompts/attribution/attribution.j2` — Attribution reasoning prompt

## Docker deployment

```bash
docker compose build
docker compose up -d
# API at http://localhost:8000 ; healthcheck via /healthz
# Scheduler runs in its own container; logs:
docker compose logs -f scheduler
```

Data persists in `./data` (mounted volume) — SQLite DB plus `data/reports/` archive.

## Testing & linting

```bash
pytest -q                          # 171+ tests
ruff check .                       # lint
mypy src/investment_agent          # strict type-check
```

## Architecture

Layered: ingestion (yfinance) → SQLAlchemy/alembic schema → Pydantic-AI agents (Tier 2 scoring, Tier 3 deep, Attribution) → pipeline orchestrators → Typer CLI + FastAPI + APScheduler. Async-first with bounded concurrency. See `docs/superpowers/specs/2026-06-07-investment-agent-design.md` for the design doc and `docs/superpowers/plans/` for the per-phase implementation plans (A: foundation, B: Discovery, C: Attribution, D: API/Scheduler/Docker).

## Status

V1 (2026-06-08): analysis-only Phase A. No trading execution. NASDAQ-100 single universe; future V1.5+ adds HSI, Telegram notifier, Web UI.
