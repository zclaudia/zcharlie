# Investment Agent · Plan A: Foundation

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 搭建项目骨架（Phase 0），构建可工作的市场数据采集层（Phase 1），构建可工作的 LLM 调用与审计层（Phase 2）。完成后系统可以拉取 NASDAQ-100 的真实市场数据进库，并能用 OpenAI-compatible 任意 provider 完成结构化 LLM 调用。

**Architecture:** Python 3.12 + asyncio 单进程。SQLAlchemy 2.x async + alembic + SQLite。yfinance 作为 V1 数据源，藏在 `DataSource` Protocol 后。openai SDK + Pydantic-AI 框架，封装在内部 `LLMClient` Protocol 后。所有 LLM 调用自动记录到 `llm_call` 表用于成本审计。

**Tech Stack:** Python 3.12, FastAPI（Plan D 才用，本计划仅引入依赖）, Typer, SQLAlchemy 2.x async, aiosqlite, alembic, pydantic 2.x, pydantic-settings, pydantic-ai, openai, yfinance, pandas, httpx, structlog, tenacity, pytest + pytest-asyncio + respx + freezegun, ruff, mypy, Docker

**Plan Scope:** Phase 0 (4-6h) + Phase 1 (11-17h) + Phase 2 (8-12h) = 23-35h 总计

**Plans 后续编号说明：**
- Plan B（Phase 3+4+6）：策略 + Discovery + Renderer/Notifier
- Plan C（Phase 5）：Attribution + Tools + 归因 Agent
- Plan D（Phase 7+8+9）：API + Scheduler + Docker + 测试加固

**Spec：** `docs/superpowers/specs/2026-06-07-investment-agent-design.md`

---

## 文件结构总览（Plan A 结束时的状态）

```
investment-agent (= 仓库根)
├── .env.example
├── .gitignore
├── .dockerignore
├── Dockerfile
├── docker-compose.yml
├── pyproject.toml
├── alembic.ini
├── alembic/
│   ├── env.py
│   ├── script.py.mako
│   └── versions/
│       ├── 001_reference_entities.py
│       ├── 002_market_data.py
│       ├── 003_relationships_and_user_domain.py
│       └── 004_analysis_and_ops.py
├── config/
│   ├── app.yaml
│   └── universes/
│       └── nasdaq_100.yaml
├── src/
│   └── investment_agent/
│       ├── __init__.py
│       ├── config.py                        # pydantic-settings
│       ├── logging.py                       # structlog 配置
│       ├── domain/
│       │   ├── __init__.py
│       │   └── protocols.py                 # DataSource + LLMClient Protocols
│       ├── storage/
│       │   ├── __init__.py
│       │   ├── database.py                  # async engine / session factory
│       │   ├── models/
│       │   │   ├── __init__.py
│       │   │   ├── base.py                  # DeclarativeBase + timestamp mixin
│       │   │   ├── reference.py             # Market, Sector, Company, Listing, Universe, UniverseMembership
│       │   │   ├── market_data.py           # DailyPrice, FinancialReport, FundamentalSnapshot, NewsItem, AnalystRatingChange
│       │   │   ├── relationship.py          # CompanyRelationship
│       │   │   ├── user_domain.py           # WatchlistItem, HoldingItem, AuxiliaryListing
│       │   │   ├── analysis.py              # Strategy, PipelineRun, StockScore, DiscoveryReport, AttributionReport
│       │   │   └── ops.py                   # LLMCall, DataFetchJob, DeepAnalysisCache
│       │   ├── repositories/
│       │   │   ├── __init__.py
│       │   │   ├── base.py                  # 泛型 Repository 基类
│       │   │   ├── listing.py
│       │   │   ├── market_data.py
│       │   │   └── llm_call.py
│       │   └── seeders/
│       │       ├── __init__.py
│       │       └── nasdaq_100.py            # NASDAQ-100 成份股初始化
│       ├── sources/
│       │   ├── __init__.py
│       │   ├── records.py                   # Pydantic DTOs for data records
│       │   ├── retry.py                     # tenacity + Semaphore 工具
│       │   └── yfinance_source.py
│       ├── llm/
│       │   ├── __init__.py
│       │   ├── pricing.py                   # 模型成本表
│       │   ├── tracking.py                  # LLMCall 审计写入
│       │   └── openai_compatible.py
│       └── cli/
│           ├── __init__.py
│           ├── main.py                      # Typer app
│           └── commands/
│               ├── __init__.py
│               ├── data.py                  # data backfill / refresh / seed
│               └── admin.py                 # db migrate / cost
├── tests/
│   ├── __init__.py
│   ├── conftest.py                          # 共享 fixtures (testdb, fake_llm 等)
│   ├── unit/
│   │   ├── __init__.py
│   │   ├── test_config.py
│   │   ├── test_yfinance_source.py
│   │   ├── test_repositories.py
│   │   ├── test_llm_tracking.py
│   │   └── test_pricing.py
│   ├── integration/
│   │   ├── __init__.py
│   │   ├── test_database.py
│   │   └── test_data_pipeline_smoke.py
│   └── fixtures/
│       ├── yfinance_history.json
│       ├── yfinance_info.json
│       ├── yfinance_financials.json
│       └── yfinance_news.json
└── data/
    └── .gitkeep
```

---

# Phase 0: 项目脚手架

## Task 1: 初始化 Python 项目元数据

**Files:**
- Create: `pyproject.toml`
- Create: `.gitignore`
- Create: `.dockerignore`
- Create: `src/investment_agent/__init__.py`
- Create: `tests/__init__.py`
- Create: `tests/unit/__init__.py`
- Create: `tests/integration/__init__.py`
- Create: `data/.gitkeep`

- [ ] **Step 1: 创建 `.gitignore`**

```gitignore
# Python
__pycache__/
*.py[cod]
*$py.class
*.egg-info/
*.egg
.pytest_cache/
.mypy_cache/
.ruff_cache/
.coverage
htmlcov/

# Virtualenv
.venv/
venv/

# IDE
.vscode/
.idea/
*.swp

# Env
.env
.env.local
.env.*.local

# Data
data/*
!data/.gitkeep

# OS
.DS_Store
Thumbs.db
```

- [ ] **Step 2: 创建 `.dockerignore`**

```
.git
.venv
__pycache__
.pytest_cache
.mypy_cache
.ruff_cache
data
*.egg-info
.env
.env.*
docs
```

- [ ] **Step 3: 创建 `pyproject.toml`**

```toml
[build-system]
requires = ["setuptools>=68", "wheel"]
build-backend = "setuptools.build_meta"

[project]
name = "investment-agent"
version = "0.1.0"
description = "Personal investment analysis agent"
requires-python = ">=3.12"
dependencies = [
    # Web / API
    "fastapi>=0.110",
    "uvicorn[standard]>=0.27",
    "httpx>=0.27",

    # CLI
    "typer>=0.12",
    "rich>=13.7",

    # Config / Validation
    "pydantic>=2.7",
    "pydantic-settings>=2.2",
    "pyyaml>=6.0",

    # Storage
    "sqlalchemy[asyncio]>=2.0.30",
    "aiosqlite>=0.20",
    "alembic>=1.13",

    # Scheduler (used in Plan D; included now to avoid env churn)
    "apscheduler>=3.10",

    # Data / Finance
    "yfinance>=0.2.40",
    "pandas>=2.2",
    "numpy>=1.26",

    # LLM
    "openai>=1.30",
    "pydantic-ai>=0.0.20",

    # Logging
    "structlog>=24.1",

    # Retry / Rate
    "tenacity>=8.2",
]

[project.optional-dependencies]
dev = [
    "pytest>=8.0",
    "pytest-asyncio>=0.23",
    "pytest-cov>=4.1",
    "respx>=0.21",
    "freezegun>=1.4",
    "ruff>=0.4",
    "mypy>=1.10",
    "types-PyYAML",
]

[project.scripts]
ia = "investment_agent.cli.main:app"

[tool.setuptools.packages.find]
where = ["src"]

[tool.pytest.ini_options]
asyncio_mode = "auto"
testpaths = ["tests"]
addopts = "-ra --strict-markers"
markers = [
    "slow: marks tests as slow (deselect with '-m \"not slow\"')",
    "network: marks tests requiring real network (deselect by default)",
]

[tool.ruff]
target-version = "py312"
line-length = 100
src = ["src", "tests"]

[tool.ruff.lint]
select = ["E", "F", "I", "N", "UP", "B", "SIM", "ASYNC"]
ignore = ["E501"]  # line length already enforced by formatter

[tool.mypy]
python_version = "3.12"
strict = true
mypy_path = ["src"]
namespace_packages = true
explicit_package_bases = true

[[tool.mypy.overrides]]
module = ["yfinance.*", "pandas.*", "numpy.*"]
ignore_missing_imports = true
```

- [ ] **Step 4: 创建包初始化文件**

```bash
mkdir -p src/investment_agent tests/unit tests/integration tests/fixtures data
touch src/investment_agent/__init__.py
touch tests/__init__.py
touch tests/unit/__init__.py
touch tests/integration/__init__.py
touch tests/fixtures/.gitkeep
touch data/.gitkeep
```

`src/investment_agent/__init__.py` 内容：

```python
"""Investment Agent - Personal investment analysis system."""

__version__ = "0.1.0"
```

- [ ] **Step 5: 创建本地 venv 并安装**

```bash
python3.12 -m venv .venv
source .venv/bin/activate
pip install -U pip
pip install -e ".[dev]"
```

Expected: 全部依赖安装成功，pip 输出 `Successfully installed investment-agent-0.1.0`。

- [ ] **Step 6: 验证 pytest 启动**

Run: `pytest --version`
Expected: `pytest 8.x.x`

Run: `pytest tests/` (空目录)
Expected: `no tests ran in 0.0xs`

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml .gitignore .dockerignore src/ tests/ data/
git commit -m "feat: init project scaffold and dependencies"
```

---

## Task 2: 配置层（pydantic-settings + .env + YAML）

**Files:**
- Create: `.env.example`
- Create: `config/app.yaml`
- Create: `src/investment_agent/config.py`
- Create: `tests/unit/test_config.py`

- [ ] **Step 1: 创建 `.env.example`**

```bash
mkdir -p config
```

`.env.example`:

```env
# Application
APP_ENV=dev
LOG_LEVEL=INFO
DATA_DIR=./data

# Security
API_KEY=change-me-dev-key

# Database
DATABASE_URL=sqlite+aiosqlite:///./data/app.db

# LLM Tier 2 (mid-tier model for scoring)
LLM_TIER2_BASE_URL=https://api.deepseek.com/v1
LLM_TIER2_API_KEY=sk-PLACEHOLDER
LLM_TIER2_MODEL=deepseek-chat

# LLM Tier 3 (top-tier model for deep analysis)
LLM_TIER3_BASE_URL=https://api.openai.com/v1
LLM_TIER3_API_KEY=sk-PLACEHOLDER
LLM_TIER3_MODEL=gpt-5

# Concurrency / rate limits
YFINANCE_MAX_CONCURRENT=10
LLM_MAX_CONCURRENT=8
```

- [ ] **Step 2: 创建 `config/app.yaml`**

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

- [ ] **Step 3: 写失败的测试 — `tests/unit/test_config.py`**

```python
from pathlib import Path

from investment_agent.config import Settings, load_app_yaml


def test_settings_loads_env_with_defaults(monkeypatch, tmp_path):
    monkeypatch.setenv("API_KEY", "test-key")
    monkeypatch.setenv("DATABASE_URL", "sqlite+aiosqlite:///:memory:")
    monkeypatch.setenv("LLM_TIER2_API_KEY", "sk-1")
    monkeypatch.setenv("LLM_TIER3_API_KEY", "sk-2")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))

    s = Settings()
    assert s.app_env == "dev"
    assert s.api_key == "test-key"
    assert s.llm_tier2.model == "deepseek-chat"
    assert s.yfinance_max_concurrent == 10


def test_app_yaml_loads(tmp_path):
    p = tmp_path / "app.yaml"
    p.write_text(
        "universes:\n"
        "  - code: NASDAQ_100\n"
        "    refresh_membership_freq: weekly\n"
        "enabled_strategies: [value]\n"
        "pipeline:\n"
        "  tier2_score_threshold: 40\n"
        "  tier3_top_n: 10\n"
        "  deep_analysis_cache_days: 90\n"
        "scheduler:\n"
        "  discovery_cron: '30 16 * * 1-5'\n"
        "  discovery_timezone: America/New_York\n"
        "auxiliary_tracking:\n"
        "  strength_threshold: 0.5\n"
        "  max_per_company: 5\n"
        "  total_cap_multiplier: 2\n"
    )
    cfg = load_app_yaml(p)
    assert cfg.pipeline.tier2_score_threshold == 40
    assert cfg.scheduler.discovery_timezone == "America/New_York"
    assert cfg.auxiliary_tracking.strength_threshold == 0.5
```

- [ ] **Step 4: 运行测试确认失败**

Run: `pytest tests/unit/test_config.py -v`
Expected: `ImportError: cannot import name 'Settings' from 'investment_agent.config'`

- [ ] **Step 5: 实现 `src/investment_agent/config.py`**

```python
"""Application configuration: env-based Settings + YAML config."""

from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class LLMTierConfig(BaseModel):
    base_url: str
    api_key: str
    model: str


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_nested_delimiter="__", extra="ignore"
    )

    app_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    data_dir: Path = Path("./data")

    api_key: str

    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    # Tier 2 (mid)
    llm_tier2_base_url: str = "https://api.deepseek.com/v1"
    llm_tier2_api_key: str
    llm_tier2_model: str = "deepseek-chat"

    # Tier 3 (top)
    llm_tier3_base_url: str = "https://api.openai.com/v1"
    llm_tier3_api_key: str
    llm_tier3_model: str = "gpt-5"

    yfinance_max_concurrent: int = 10
    llm_max_concurrent: int = 8

    @property
    def llm_tier2(self) -> LLMTierConfig:
        return LLMTierConfig(
            base_url=self.llm_tier2_base_url,
            api_key=self.llm_tier2_api_key,
            model=self.llm_tier2_model,
        )

    @property
    def llm_tier3(self) -> LLMTierConfig:
        return LLMTierConfig(
            base_url=self.llm_tier3_base_url,
            api_key=self.llm_tier3_api_key,
            model=self.llm_tier3_model,
        )


class UniverseEntry(BaseModel):
    code: str
    refresh_membership_freq: Literal["daily", "weekly", "monthly"] = "weekly"


class PipelineConfig(BaseModel):
    tier2_score_threshold: int = 40
    tier3_top_n: int = 10
    deep_analysis_cache_days: int = 90


class SchedulerConfig(BaseModel):
    discovery_cron: str
    discovery_timezone: str


class AuxiliaryTrackingConfig(BaseModel):
    strength_threshold: float = 0.5
    max_per_company: int = 5
    total_cap_multiplier: int = 2


class AppYamlConfig(BaseModel):
    universes: list[UniverseEntry]
    enabled_strategies: list[str] = Field(default_factory=list)
    pipeline: PipelineConfig
    scheduler: SchedulerConfig
    auxiliary_tracking: AuxiliaryTrackingConfig


def load_app_yaml(path: Path) -> AppYamlConfig:
    with path.open() as f:
        raw = yaml.safe_load(f)
    return AppYamlConfig.model_validate(raw)
```

- [ ] **Step 6: 运行测试确认通过**

Run: `pytest tests/unit/test_config.py -v`
Expected: 2 passed

- [ ] **Step 7: Commit**

```bash
git add .env.example config/ src/investment_agent/config.py tests/unit/test_config.py
git commit -m "feat: configuration layer (env + yaml) with validation"
```

---

## Task 3: 结构化日志

**Files:**
- Create: `src/investment_agent/logging.py`

- [ ] **Step 1: 实现 `src/investment_agent/logging.py`**

```python
"""structlog configuration."""

import logging
import sys

import structlog


def configure_logging(level: str = "INFO") -> None:
    log_level = getattr(logging, level.upper(), logging.INFO)
    logging.basicConfig(format="%(message)s", stream=sys.stdout, level=log_level)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.make_filtering_bound_logger(log_level),
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )


def get_logger(name: str | None = None) -> structlog.BoundLogger:
    return structlog.get_logger(name)
```

- [ ] **Step 2: Smoke test**

Run:
```bash
python -c "
from investment_agent.logging import configure_logging, get_logger
configure_logging('DEBUG')
get_logger('test').info('hello', key='value')
"
```
Expected: JSON line with `level: info`, `event: hello`, `key: value`, `timestamp`.

- [ ] **Step 3: Commit**

```bash
git add src/investment_agent/logging.py
git commit -m "feat: structlog-based JSON logging"
```

---

## Task 4: 数据库连接层 + ORM Base

**Files:**
- Create: `src/investment_agent/storage/__init__.py`
- Create: `src/investment_agent/storage/database.py`
- Create: `src/investment_agent/storage/models/__init__.py`
- Create: `src/investment_agent/storage/models/base.py`
- Create: `tests/integration/test_database.py`

- [ ] **Step 1: 创建包目录**

```bash
mkdir -p src/investment_agent/storage/models src/investment_agent/storage/repositories
touch src/investment_agent/storage/__init__.py
touch src/investment_agent/storage/models/__init__.py
touch src/investment_agent/storage/repositories/__init__.py
```

- [ ] **Step 2: 实现 `models/base.py`**

```python
"""SQLAlchemy declarative base + timestamp mixin."""

from datetime import datetime

from sqlalchemy import DateTime, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Project-wide declarative base."""


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
```

- [ ] **Step 3: 实现 `storage/database.py`**

```python
"""Async engine + session factory."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def init_engine(database_url: str, *, echo: bool = False) -> AsyncEngine:
    global _engine, _session_factory
    _engine = create_async_engine(database_url, echo=echo, future=True)
    _session_factory = async_sessionmaker(
        _engine, class_=AsyncSession, expire_on_commit=False
    )
    return _engine


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Engine not initialized. Call init_engine() first.")
    return _engine


@asynccontextmanager
async def session_scope() -> AsyncIterator[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Session factory not initialized. Call init_engine() first.")
    async with _session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
```

- [ ] **Step 4: 写测试 `tests/integration/test_database.py`**

```python
import pytest

from investment_agent.storage.database import init_engine, session_scope


@pytest.mark.asyncio
async def test_session_scope_commits():
    from sqlalchemy import text

    init_engine("sqlite+aiosqlite:///:memory:")
    async with session_scope() as s:
        result = await s.execute(text("SELECT 1 AS one"))
        assert result.scalar() == 1


@pytest.mark.asyncio
async def test_session_scope_rolls_back_on_error():
    from sqlalchemy import text

    init_engine("sqlite+aiosqlite:///:memory:")
    with pytest.raises(ValueError):
        async with session_scope() as s:
            await s.execute(text("SELECT 1"))
            raise ValueError("boom")
```

- [ ] **Step 5: 运行测试**

Run: `pytest tests/integration/test_database.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add src/investment_agent/storage/ tests/integration/test_database.py
git commit -m "feat: async SQLAlchemy engine + session scope"
```

---

## Task 5: Alembic 设置

**Files:**
- Create: `alembic.ini`
- Create: `alembic/env.py`
- Create: `alembic/script.py.mako`
- Create: `alembic/versions/.gitkeep`

- [ ] **Step 1: 初始化 alembic**

```bash
alembic init -t async alembic
```

这会生成 `alembic/`, `alembic.ini`。**手动修改这两个文件**避免使用模板默认值（下一步）。

- [ ] **Step 2: 修改 `alembic.ini` — 关键字段**

打开 `alembic.ini`，把下面这些字段改为：

```ini
script_location = alembic
prepend_sys_path = src
sqlalchemy.url = sqlite+aiosqlite:///./data/app.db

# 末尾保留 alembic 默认的 logger 配置
```

- [ ] **Step 3: 替换 `alembic/env.py`**

```python
"""Alembic environment configured for async + project models."""

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from investment_agent.storage.models.base import Base

# 触发所有 model 模块导入（让 Base.metadata 收集到所有表）
from investment_agent.storage.models import (  # noqa: F401
    reference,
    market_data,
    relationship,
    user_domain,
    analysis,
    ops,
)

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(connection=connection, target_metadata=target_metadata)
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)
    await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
```

- [ ] **Step 4: 验证 alembic 可以加载（不创建迁移）**

注意：此时 model 文件还没创建，所以会报 ImportError。我们故意先放上 imports，下一组 Task 创建模型时直接对齐。

Run: `alembic current` (会失败)
Expected: `ImportError` for models — 这是预期的，Task 6+ 会修复。

- [ ] **Step 5: Commit（即使不完整）**

```bash
git add alembic.ini alembic/
git commit -m "feat: configure alembic for async migrations"
```

---

## Task 6: ORM Models — Reference Entities

**Files:**
- Create: `src/investment_agent/storage/models/reference.py`

- [ ] **Step 1: 实现 reference models**

```python
"""Reference entities: Market, Sector, Company, Listing, Universe, UniverseMembership."""

from datetime import date

from sqlalchemy import Boolean, Date, ForeignKey, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column, relationship

from investment_agent.storage.models.base import Base, TimestampMixin


class Market(Base, TimestampMixin):
    __tablename__ = "market"

    code: Mapped[str] = mapped_column(String(16), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    currency: Mapped[str] = mapped_column(String(8), nullable=False)


class Sector(Base, TimestampMixin):
    __tablename__ = "sector"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)


class Company(Base, TimestampMixin):
    __tablename__ = "company"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    canonical_name: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    sector_code: Mapped[str | None] = mapped_column(
        ForeignKey("sector.code"), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    website: Mapped[str | None] = mapped_column(String(256), nullable=True)

    listings: Mapped[list["Listing"]] = relationship(back_populates="company")


class Listing(Base, TimestampMixin):
    __tablename__ = "listing"
    __table_args__ = (
        UniqueConstraint("market_code", "ticker", name="uq_listing_market_ticker"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    market_code: Mapped[str] = mapped_column(ForeignKey("market.code"), nullable=False)
    ticker: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)

    company: Mapped[Company] = relationship(back_populates="listings")


class Universe(Base, TimestampMixin):
    __tablename__ = "universe"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)


class UniverseMembership(Base, TimestampMixin):
    __tablename__ = "universe_membership"

    universe_code: Mapped[str] = mapped_column(
        ForeignKey("universe.code"), primary_key=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), primary_key=True
    )
    valid_from: Mapped[date] = mapped_column(Date, primary_key=True)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
```

- [ ] **Step 2: 占位创建其他 model 文件（让 alembic env.py 的 import 不爆炸）**

每个新建空 .py 文件：

```bash
touch src/investment_agent/storage/models/market_data.py
touch src/investment_agent/storage/models/relationship.py
touch src/investment_agent/storage/models/user_domain.py
touch src/investment_agent/storage/models/analysis.py
touch src/investment_agent/storage/models/ops.py
```

每个文件先写一行注释：

```python
"""Placeholder. Populated in subsequent task."""
```

- [ ] **Step 3: 生成首次迁移 `001_reference_entities.py`**

```bash
alembic revision --autogenerate -m "reference entities"
```

将自动生成的文件**重命名**为 `alembic/versions/001_reference_entities.py`（rename 实际文件名以便顺序清晰）。在 Python migration 文件顶部，**手动确认 `revision = "001"`** 而不是随机字符串：

```python
# alembic/versions/001_reference_entities.py
revision = "001"
down_revision = None
```

- [ ] **Step 4: 应用迁移**

```bash
mkdir -p data
alembic upgrade head
```

Expected: `Running upgrade  -> 001, reference entities`，data/app.db 文件被创建。

- [ ] **Step 5: 验证表存在**

```bash
sqlite3 data/app.db ".schema market" | head
sqlite3 data/app.db ".schema listing" | head
```

Expected: 看到 CREATE TABLE 语句包含 ticker, market_code, company_id 等字段。

- [ ] **Step 6: Commit**

```bash
git add src/investment_agent/storage/models/ alembic/versions/001_reference_entities.py
git commit -m "feat: reference entity models + migration 001"
```

---

## Task 7: ORM Models — Market Data

**Files:**
- Modify: `src/investment_agent/storage/models/market_data.py`
- Create: `alembic/versions/002_market_data.py`

- [ ] **Step 1: 实现 market_data models**

```python
"""Daily price, financial report, fundamental snapshot, news, analyst ratings."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import JSON, Date, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin


class DailyPrice(Base, TimestampMixin):
    __tablename__ = "daily_price"

    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)
    open: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    high: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    low: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    volume: Mapped[int] = mapped_column(nullable=False)
    adjusted_close: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)


class FinancialReport(Base, TimestampMixin):
    __tablename__ = "financial_report"

    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), primary_key=True
    )
    fiscal_period: Mapped[str] = mapped_column(String(16), primary_key=True)
    period_end: Mapped[date] = mapped_column(Date, nullable=False)

    revenue: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    net_income: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    fcf: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    total_assets: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)
    total_equity: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)

    raw_data: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class FundamentalSnapshot(Base, TimestampMixin):
    __tablename__ = "fundamental_snapshot"

    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), primary_key=True
    )
    date: Mapped[date] = mapped_column(Date, primary_key=True)

    pe: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    pb: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    peg: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    ps: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    roe: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    roa: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    fcf_yield: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    debt_to_equity: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
    market_cap: Mapped[Decimal | None] = mapped_column(Numeric(20, 2), nullable=True)


class NewsItem(Base, TimestampMixin):
    __tablename__ = "news_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    published_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    source: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    url: Mapped[str] = mapped_column(String(1024), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    sentiment: Mapped[Decimal | None] = mapped_column(Numeric(6, 4), nullable=True)


class AnalystRatingChange(Base, TimestampMixin):
    __tablename__ = "analyst_rating_change"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), nullable=False, index=True
    )
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    firm: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    from_rating: Mapped[str | None] = mapped_column(String(32), nullable=True)
    to_rating: Mapped[str | None] = mapped_column(String(32), nullable=True)
    price_target: Mapped[Decimal | None] = mapped_column(Numeric(18, 6), nullable=True)
```

- [ ] **Step 2: 生成 + rename 迁移**

```bash
alembic revision --autogenerate -m "market data"
```

重命名生成的文件为 `alembic/versions/002_market_data.py`，且文件顶部：

```python
revision = "002"
down_revision = "001"
```

- [ ] **Step 3: 应用并验证**

```bash
alembic upgrade head
sqlite3 data/app.db ".tables"
```

Expected: 看到 `daily_price`, `financial_report`, `fundamental_snapshot`, `news_item`, `analyst_rating_change` 出现在表列表里。

- [ ] **Step 4: Commit**

```bash
git add src/investment_agent/storage/models/market_data.py alembic/versions/002_market_data.py
git commit -m "feat: market data models + migration 002"
```

---

## Task 8: ORM Models — Relationships + User Domain + Auxiliary

**Files:**
- Modify: `src/investment_agent/storage/models/relationship.py`
- Modify: `src/investment_agent/storage/models/user_domain.py`
- Create: `alembic/versions/003_relationships_and_user_domain.py`

- [ ] **Step 1: 实现 relationship.py**

```python
"""CompanyRelationship: supplier_of / competes_with / subsidiary_of / partner_of."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin


class CompanyRelationship(Base, TimestampMixin):
    __tablename__ = "company_relationship"
    __table_args__ = (
        UniqueConstraint(
            "from_company_id",
            "to_company_id",
            "relationship_type",
            "valid_from",
            name="uq_company_rel",
        ),
        Index("ix_rel_from", "from_company_id", "relationship_type"),
        Index("ix_rel_to", "to_company_id", "relationship_type"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    from_company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), nullable=False
    )
    to_company_id: Mapped[int] = mapped_column(
        ForeignKey("company.id"), nullable=False
    )
    relationship_type: Mapped[str] = mapped_column(String(32), nullable=False)

    strength: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False, default=0.5)
    revenue_share_pct: Mapped[Decimal | None] = mapped_column(
        Numeric(6, 3), nullable=True
    )
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    source: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    evidence_url: Mapped[str | None] = mapped_column(String(1024), nullable=True)

    valid_from: Mapped[date] = mapped_column(Date, nullable=False)
    valid_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    discovered_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_verified_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
```

- [ ] **Step 2: 实现 user_domain.py**

```python
"""WatchlistItem, HoldingItem, AuxiliaryListing."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin


class WatchlistItem(Base, TimestampMixin):
    __tablename__ = "watchlist_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), nullable=False, unique=True
    )
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class HoldingItem(Base, TimestampMixin):
    """Reserved for Phase B — V1 not populated."""

    __tablename__ = "holding_item"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), nullable=False)
    cost_basis: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    shares: Mapped[Decimal] = mapped_column(Numeric(18, 6), nullable=False)
    opened_at: Mapped[date] = mapped_column(Date, nullable=False)


class AuxiliaryListing(Base, TimestampMixin):
    __tablename__ = "auxiliary_listing"
    __table_args__ = (UniqueConstraint("listing_id", name="uq_aux_listing"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), nullable=False
    )
    added_via_relationship_id: Mapped[int | None] = mapped_column(
        ForeignKey("company_relationship.id"), nullable=True
    )
    added_by: Mapped[str] = mapped_column(String(32), nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
```

- [ ] **Step 3: 生成 + rename 迁移**

```bash
alembic revision --autogenerate -m "relationships and user domain"
```

重命名为 `alembic/versions/003_relationships_and_user_domain.py`：

```python
revision = "003"
down_revision = "002"
```

- [ ] **Step 4: 应用并验证**

```bash
alembic upgrade head
sqlite3 data/app.db ".tables" | grep -E "(relationship|watchlist|auxiliary)"
```

Expected: 三张表都在。

- [ ] **Step 5: Commit**

```bash
git add src/investment_agent/storage/models/relationship.py \
       src/investment_agent/storage/models/user_domain.py \
       alembic/versions/003_relationships_and_user_domain.py
git commit -m "feat: relationship + user domain + auxiliary models, migration 003"
```

---

## Task 9: ORM Models — Analysis + Ops

**Files:**
- Modify: `src/investment_agent/storage/models/analysis.py`
- Modify: `src/investment_agent/storage/models/ops.py`
- Create: `alembic/versions/004_analysis_and_ops.py`

- [ ] **Step 1: 实现 analysis.py**

```python
"""Strategy + PipelineRun + StockScore + DiscoveryReport + AttributionReport."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin


class Strategy(Base, TimestampMixin):
    __tablename__ = "strategy"

    code: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(16), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    config_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PipelineRun(Base, TimestampMixin):
    __tablename__ = "pipeline_run"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    universe_code: Mapped[str | None] = mapped_column(
        ForeignKey("universe.code"), nullable=True
    )
    listing_id: Mapped[int | None] = mapped_column(
        ForeignKey("listing.id"), nullable=True
    )
    triggered_by: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="running")
    error_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class StockScore(Base, TimestampMixin):
    __tablename__ = "stock_score"
    __table_args__ = (
        UniqueConstraint(
            "pipeline_run_id", "listing_id", "strategy_code", name="uq_stock_score"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline_run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_run.id"), nullable=False, index=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), nullable=False, index=True
    )
    strategy_code: Mapped[str] = mapped_column(
        ForeignKey("strategy.code"), nullable=False
    )
    tier: Mapped[int] = mapped_column(nullable=False)
    score: Mapped[int] = mapped_column(nullable=False)
    confidence: Mapped[Decimal] = mapped_column(Numeric(4, 3), nullable=False)
    bull_thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    bear_thesis: Mapped[str | None] = mapped_column(Text, nullable=True)
    key_metrics: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    deep_analysis: Mapped[str | None] = mapped_column(Text, nullable=True)


class DiscoveryReport(Base, TimestampMixin):
    __tablename__ = "discovery_report"

    pipeline_run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_run.id"), primary_key=True
    )
    consensus_picks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    divergent_picks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    overlooked_picks: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    markdown_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)


class AttributionReport(Base, TimestampMixin):
    __tablename__ = "attribution_report"

    pipeline_run_id: Mapped[int] = mapped_column(
        ForeignKey("pipeline_run.id"), primary_key=True
    )
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), nullable=False
    )
    analysis_date: Mapped[date] = mapped_column(Date, nullable=False)
    price_change_pct: Mapped[Decimal] = mapped_column(Numeric(8, 4), nullable=False)
    beta_attribution: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)
    triggering_events: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    narrative: Mapped[str] = mapped_column(Text, nullable=False)
    valuation_delta: Mapped[str | None] = mapped_column(Text, nullable=True)
    markdown_path: Mapped[str | None] = mapped_column(String(1024), nullable=True)
```

- [ ] **Step 2: 实现 ops.py**

```python
"""LLMCall + DataFetchJob + DeepAnalysisCache."""

from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base, TimestampMixin


class LLMCall(Base, TimestampMixin):
    __tablename__ = "llm_call"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    pipeline_run_id: Mapped[int | None] = mapped_column(
        ForeignKey("pipeline_run.id"), nullable=True, index=True
    )
    purpose: Mapped[str] = mapped_column(String(64), nullable=False)
    model: Mapped[str] = mapped_column(String(128), nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    prompt_tokens: Mapped[int] = mapped_column(nullable=False)
    completion_tokens: Mapped[int] = mapped_column(nullable=False)
    cost_usd: Mapped[Decimal] = mapped_column(Numeric(12, 6), nullable=False)
    latency_ms: Mapped[int] = mapped_column(nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)


class DataFetchJob(Base, TimestampMixin):
    __tablename__ = "data_fetch_job"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    target_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    finished_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    records_fetched: Mapped[int] = mapped_column(nullable=False, default=0)
    errors: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict)


class DeepAnalysisCache(Base, TimestampMixin):
    __tablename__ = "deep_analysis_cache"
    __table_args__ = (
        UniqueConstraint(
            "listing_id", "latest_financial_report_period", name="uq_deep_cache_key"
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    listing_id: Mapped[int] = mapped_column(
        ForeignKey("listing.id"), nullable=False, index=True
    )
    latest_financial_report_period: Mapped[str] = mapped_column(
        String(16), nullable=False
    )
    strategy_code: Mapped[str] = mapped_column(
        ForeignKey("strategy.code"), nullable=False
    )
    analysis_json: Mapped[dict] = mapped_column(JSON, nullable=False)
    generated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
```

- [ ] **Step 3: 生成 + rename 迁移**

```bash
alembic revision --autogenerate -m "analysis and ops"
```

重命名为 `alembic/versions/004_analysis_and_ops.py`：

```python
revision = "004"
down_revision = "003"
```

- [ ] **Step 4: 应用并验证全部表**

```bash
alembic upgrade head
sqlite3 data/app.db ".tables"
```

Expected: 完整表清单（按字母序）：

```
analyst_rating_change  daily_price          financial_report
fundamental_snapshot   llm_call             market
auxiliary_listing      attribution_report   company
data_fetch_job         deep_analysis_cache  company_relationship
discovery_report       holding_item         listing
news_item              pipeline_run         sector
stock_score            strategy             universe
universe_membership    watchlist_item       alembic_version
```

- [ ] **Step 5: 测试：完整迁移可以全量回滚再上滚**

```bash
alembic downgrade base
alembic upgrade head
```

Expected: 两次操作都成功，无错误。

- [ ] **Step 6: Commit**

```bash
git add src/investment_agent/storage/models/analysis.py \
       src/investment_agent/storage/models/ops.py \
       alembic/versions/004_analysis_and_ops.py
git commit -m "feat: analysis + ops models, migration 004 (full schema)"
```

---

## Task 10: Repository 基类 + ListingRepository

**Files:**
- Create: `src/investment_agent/storage/repositories/base.py`
- Create: `src/investment_agent/storage/repositories/listing.py`
- Create: `tests/unit/test_repositories.py`

- [ ] **Step 1: 实现 `repositories/base.py`**

```python
"""Generic repository base."""

from typing import Generic, TypeVar

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.storage.models.base import Base

T = TypeVar("T", bound=Base)


class Repository(Generic[T]):
    model: type[T]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, ident: int | str) -> T | None:
        return await self.session.get(self.model, ident)
```

- [ ] **Step 2: 实现 `repositories/listing.py`**

```python
"""Listing repository."""

from sqlalchemy import select

from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.base import Repository


class ListingRepository(Repository[Listing]):
    model = Listing

    async def get_by_ticker(self, market_code: str, ticker: str) -> Listing | None:
        stmt = select(Listing).where(
            Listing.market_code == market_code, Listing.ticker == ticker
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def upsert_listing(
        self,
        *,
        market_code: str,
        ticker: str,
        company_name: str,
        sector_code: str | None = None,
    ) -> Listing:
        # 1. ensure market exists
        market = await self.session.get(Market, market_code)
        if market is None:
            raise ValueError(f"Unknown market: {market_code}")

        # 2. ensure company exists (match by canonical_name)
        stmt = select(Company).where(Company.canonical_name == company_name)
        company = (await self.session.execute(stmt)).scalar_one_or_none()
        if company is None:
            company = Company(canonical_name=company_name, sector_code=sector_code)
            self.session.add(company)
            await self.session.flush()

        # 3. ensure listing exists
        listing = await self.get_by_ticker(market_code, ticker)
        if listing is None:
            listing = Listing(
                company_id=company.id, market_code=market_code, ticker=ticker
            )
            self.session.add(listing)
            await self.session.flush()
        return listing
```

- [ ] **Step 3: 测试 fixtures — `tests/conftest.py`**

```python
"""Shared test fixtures."""

import pytest
import pytest_asyncio

from investment_agent.storage.database import init_engine, session_scope
from investment_agent.storage.models.base import Base
from investment_agent.storage.models import (  # noqa: F401
    reference,
    market_data,
    relationship,
    user_domain,
    analysis,
    ops,
)


@pytest_asyncio.fixture
async def db_session():
    engine = init_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    async with session_scope() as session:
        yield session
    await engine.dispose()


@pytest_asyncio.fixture
async def seeded_market(db_session):
    from investment_agent.storage.models.reference import Market

    market = Market(
        code="NASDAQ", name="NASDAQ Stock Market", timezone="America/New_York",
        currency="USD",
    )
    db_session.add(market)
    await db_session.flush()
    return market
```

- [ ] **Step 4: 写测试 `tests/unit/test_repositories.py`**

```python
import pytest

from investment_agent.storage.repositories.listing import ListingRepository


@pytest.mark.asyncio
async def test_upsert_listing_creates_company_and_listing(db_session, seeded_market):
    repo = ListingRepository(db_session)
    listing = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    assert listing.id is not None
    assert listing.ticker == "AAPL"
    assert listing.company_id is not None


@pytest.mark.asyncio
async def test_upsert_listing_is_idempotent(db_session, seeded_market):
    repo = ListingRepository(db_session)
    l1 = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    l2 = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    assert l1.id == l2.id
    assert l1.company_id == l2.company_id


@pytest.mark.asyncio
async def test_upsert_listing_unknown_market_raises(db_session):
    repo = ListingRepository(db_session)
    with pytest.raises(ValueError, match="Unknown market"):
        await repo.upsert_listing(
            market_code="GHOST", ticker="AAPL", company_name="Apple Inc."
        )
```

- [ ] **Step 5: 运行测试**

Run: `pytest tests/unit/test_repositories.py -v`
Expected: 3 passed

- [ ] **Step 6: Commit**

```bash
git add src/investment_agent/storage/repositories/ tests/conftest.py tests/unit/test_repositories.py
git commit -m "feat: repository pattern base + ListingRepository"
```

---

## Task 11: NASDAQ-100 Seeder + Universe 配置

**Files:**
- Create: `config/universes/nasdaq_100.yaml`
- Create: `src/investment_agent/storage/seeders/__init__.py`
- Create: `src/investment_agent/storage/seeders/nasdaq_100.py`

- [ ] **Step 1: 创建 `config/universes/nasdaq_100.yaml`** (包含完整 NASDAQ-100 成份股)

```yaml
code: NASDAQ_100
name: NASDAQ-100
description: NASDAQ-100 Index constituents (top 100 non-financial companies on NASDAQ)
market: NASDAQ
sector: TECH
constituents:
  - ticker: AAPL
    company_name: Apple Inc.
    sector_code: TECH
  - ticker: MSFT
    company_name: Microsoft Corporation
    sector_code: TECH
  - ticker: AMZN
    company_name: Amazon.com, Inc.
    sector_code: CONSUMER_DISCRETIONARY
  - ticker: NVDA
    company_name: NVIDIA Corporation
    sector_code: TECH
  - ticker: GOOGL
    company_name: Alphabet Inc. (Class A)
    sector_code: COMMUNICATION
  - ticker: GOOG
    company_name: Alphabet Inc. (Class C)
    sector_code: COMMUNICATION
  - ticker: META
    company_name: Meta Platforms, Inc.
    sector_code: COMMUNICATION
  - ticker: TSLA
    company_name: Tesla, Inc.
    sector_code: CONSUMER_DISCRETIONARY
  - ticker: AVGO
    company_name: Broadcom Inc.
    sector_code: TECH
  - ticker: COST
    company_name: Costco Wholesale Corporation
    sector_code: CONSUMER_STAPLES
  # NOTE: 完整 100 只成份股清单建议运行时再从 yfinance 拉取动态成份，
  # 此 YAML 仅作为初始 seed。Task 18 实现的 fetch_universe 会同步真实成份。
```

注：本 seed 文件只放 10 只代表性股票作为冷启动，剩余 90 只通过 Task 18 实现的 `fetch_universe()` 动态拉取后入库。

- [ ] **Step 2: 实现 `seeders/nasdaq_100.py`**

```python
"""Seed NASDAQ universe and bootstrap constituents from YAML."""

from datetime import date
from pathlib import Path

import yaml

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import (
    Market,
    Sector,
    Universe,
    UniverseMembership,
)
from investment_agent.storage.repositories.listing import ListingRepository


_DEFAULT_SECTORS = [
    ("TECH", "Information Technology"),
    ("COMMUNICATION", "Communication Services"),
    ("CONSUMER_DISCRETIONARY", "Consumer Discretionary"),
    ("CONSUMER_STAPLES", "Consumer Staples"),
    ("HEALTHCARE", "Health Care"),
    ("INDUSTRIALS", "Industrials"),
    ("MATERIALS", "Materials"),
    ("ENERGY", "Energy"),
    ("FINANCIALS", "Financials"),
    ("UTILITIES", "Utilities"),
    ("REAL_ESTATE", "Real Estate"),
]


async def seed_nasdaq_100(yaml_path: Path) -> int:
    with yaml_path.open() as f:
        data = yaml.safe_load(f)

    async with session_scope() as session:
        # Markets
        if await session.get(Market, "NASDAQ") is None:
            session.add(
                Market(
                    code="NASDAQ",
                    name="NASDAQ Stock Market",
                    timezone="America/New_York",
                    currency="USD",
                )
            )
        if await session.get(Market, "NYSE") is None:
            session.add(
                Market(
                    code="NYSE",
                    name="New York Stock Exchange",
                    timezone="America/New_York",
                    currency="USD",
                )
            )
        # Sectors
        for code, name in _DEFAULT_SECTORS:
            if await session.get(Sector, code) is None:
                session.add(Sector(code=code, name=name))
        # Universe
        if await session.get(Universe, data["code"]) is None:
            session.add(
                Universe(
                    code=data["code"],
                    name=data["name"],
                    description=data.get("description"),
                )
            )
        await session.flush()

        # Constituents
        repo = ListingRepository(session)
        n = 0
        for c in data["constituents"]:
            listing = await repo.upsert_listing(
                market_code=data["market"],
                ticker=c["ticker"],
                company_name=c["company_name"],
                sector_code=c.get("sector_code"),
            )
            membership = await session.get(
                UniverseMembership,
                (data["code"], listing.id, date(2024, 1, 1)),
            )
            if membership is None:
                session.add(
                    UniverseMembership(
                        universe_code=data["code"],
                        listing_id=listing.id,
                        valid_from=date(2024, 1, 1),
                    )
                )
                n += 1
        return n
```

- [ ] **Step 3: 测试 seeder**

`tests/unit/test_seeder.py`:

```python
from pathlib import Path

import pytest

from investment_agent.storage.database import init_engine
from investment_agent.storage.models.base import Base
from investment_agent.storage.models import (  # noqa: F401
    reference, market_data, relationship, user_domain, analysis, ops,
)
from investment_agent.storage.seeders.nasdaq_100 import seed_nasdaq_100


@pytest.mark.asyncio
async def test_seed_creates_universe_and_listings(tmp_path):
    yaml = tmp_path / "u.yaml"
    yaml.write_text(
        "code: TEST_UNIV\n"
        "name: Test\n"
        "description: test\n"
        "market: NASDAQ\n"
        "sector: TECH\n"
        "constituents:\n"
        "  - ticker: AAPL\n"
        "    company_name: Apple Inc.\n"
        "    sector_code: TECH\n"
        "  - ticker: MSFT\n"
        "    company_name: Microsoft Corporation\n"
        "    sector_code: TECH\n"
    )
    engine = init_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    n = await seed_nasdaq_100(yaml)
    assert n == 2
    # idempotency
    n2 = await seed_nasdaq_100(yaml)
    assert n2 == 0
    await engine.dispose()
```

Run: `pytest tests/unit/test_seeder.py -v`
Expected: 1 passed

- [ ] **Step 4: Commit**

```bash
git add config/universes/ src/investment_agent/storage/seeders/ tests/unit/test_seeder.py
git commit -m "feat: NASDAQ-100 seeder with 10 bootstrap constituents"
```

---

## Task 12: Docker + docker-compose

**Files:**
- Create: `Dockerfile`
- Create: `docker-compose.yml`

- [ ] **Step 1: 创建 `Dockerfile`**

```dockerfile
FROM python:3.12-slim AS builder

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libpq-dev curl && \
    rm -rf /var/lib/apt/lists/*

COPY pyproject.toml ./
COPY src/ src/
RUN pip install --no-cache-dir --upgrade pip && \
    pip install --no-cache-dir -e .

# ---- Runtime ----
FROM python:3.12-slim

WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 sqlite3 && \
    rm -rf /var/lib/apt/lists/*

COPY --from=builder /usr/local/lib/python3.12/site-packages /usr/local/lib/python3.12/site-packages
COPY --from=builder /usr/local/bin /usr/local/bin
COPY src/ src/
COPY config/ config/
COPY alembic/ alembic/
COPY alembic.ini ./

ENV PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app/src

CMD ["python", "-c", "print('investment-agent container ready')"]
```

- [ ] **Step 2: 创建 `docker-compose.yml`**

```yaml
services:
  app:
    build: .
    image: investment-agent:dev
    container_name: investment-agent
    env_file:
      - .env
    volumes:
      - ./data:/app/data
      - ./config:/app/config
      - ./src:/app/src
      - ./alembic:/app/alembic
      - ./alembic.ini:/app/alembic.ini
    working_dir: /app
    command: tail -f /dev/null  # idle; commands invoked via `docker compose exec`
```

- [ ] **Step 3: 测试构建**

```bash
docker compose build
```

Expected: 镜像构建成功。

- [ ] **Step 4: 测试启动 + 执行**

```bash
docker compose up -d
docker compose exec app python -c "import investment_agent; print(investment_agent.__version__)"
```

Expected: `0.1.0`

```bash
docker compose down
```

- [ ] **Step 5: Commit**

```bash
git add Dockerfile docker-compose.yml
git commit -m "feat: dockerfile + docker-compose for dev/prod parity"
```

---

# Phase 1: DataSource + YFinanceSource

## Task 13: 定义 Protocol 与 DTO

**Files:**
- Create: `src/investment_agent/domain/__init__.py`
- Create: `src/investment_agent/domain/protocols.py`
- Create: `src/investment_agent/sources/__init__.py`
- Create: `src/investment_agent/sources/records.py`

- [ ] **Step 1: 创建包**

```bash
mkdir -p src/investment_agent/domain src/investment_agent/sources
touch src/investment_agent/domain/__init__.py
touch src/investment_agent/sources/__init__.py
```

- [ ] **Step 2: 实现 `sources/records.py`**

```python
"""Pydantic DTOs returned by DataSource implementations."""

from datetime import date, datetime
from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class _RecordBase(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)


class ListingRef(_RecordBase):
    market_code: str
    ticker: str
    company_name: str
    sector_code: str | None = None


class DailyPriceRecord(_RecordBase):
    ticker: str
    date: date
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: int
    adjusted_close: Decimal


class FinancialReportRecord(_RecordBase):
    ticker: str
    fiscal_period: str   # '2025-Q3' | '2024-FY'
    period_end: date
    revenue: Decimal | None
    net_income: Decimal | None
    fcf: Decimal | None
    total_assets: Decimal | None
    total_equity: Decimal | None
    raw_data: dict


class FundamentalSnapshotRecord(_RecordBase):
    ticker: str
    date: date
    pe: Decimal | None
    pb: Decimal | None
    peg: Decimal | None
    ps: Decimal | None
    roe: Decimal | None
    roa: Decimal | None
    fcf_yield: Decimal | None
    debt_to_equity: Decimal | None
    market_cap: Decimal | None


class NewsRecord(_RecordBase):
    ticker: str
    published_at: datetime
    source: str
    title: str
    url: str
    summary: str | None = None


class AnalystRatingRecord(_RecordBase):
    ticker: str
    date: date
    firm: str
    action: str
    from_rating: str | None = None
    to_rating: str | None = None
    price_target: Decimal | None = None
```

- [ ] **Step 3: 实现 `domain/protocols.py` (DataSource)**

```python
"""Core protocols for pluggable extension points."""

from datetime import date
from typing import Protocol, runtime_checkable

from investment_agent.sources.records import (
    AnalystRatingRecord,
    DailyPriceRecord,
    FinancialReportRecord,
    FundamentalSnapshotRecord,
    ListingRef,
    NewsRecord,
)


@runtime_checkable
class DataSource(Protocol):
    name: str
    supported_markets: list[str]

    async def fetch_universe(
        self, universe_code: str, as_of: date
    ) -> list[ListingRef]: ...

    async def fetch_daily_prices(
        self, tickers: list[str], start: date, end: date
    ) -> list[DailyPriceRecord]: ...

    async def fetch_financial_report(
        self, ticker: str, periods: list[str]
    ) -> list[FinancialReportRecord]: ...

    async def fetch_fundamental_snapshot(
        self, ticker: str, as_of: date
    ) -> FundamentalSnapshotRecord: ...

    async def fetch_news(
        self, ticker: str, days: int
    ) -> list[NewsRecord]: ...

    async def fetch_analyst_ratings(
        self, ticker: str, days: int
    ) -> list[AnalystRatingRecord]: ...
```

- [ ] **Step 4: 自检 — 导入与类型不报错**

Run:
```bash
python -c "from investment_agent.domain.protocols import DataSource; print(DataSource)"
```
Expected: `<class 'investment_agent.domain.protocols.DataSource'>`

- [ ] **Step 5: Commit**

```bash
git add src/investment_agent/domain/ src/investment_agent/sources/
git commit -m "feat: DataSource Protocol + record DTOs"
```

---

## Task 14: Retry / Rate-limit 工具

**Files:**
- Create: `src/investment_agent/sources/retry.py`
- Create: `tests/unit/test_retry.py`

- [ ] **Step 1: 写测试 `tests/unit/test_retry.py`**

```python
import asyncio
import pytest

from investment_agent.sources.retry import retry_async, RateLimiter


@pytest.mark.asyncio
async def test_retry_async_succeeds_after_failure():
    calls = {"n": 0}

    @retry_async(attempts=3, base_delay=0.01)
    async def flaky():
        calls["n"] += 1
        if calls["n"] < 3:
            raise ConnectionError("boom")
        return "ok"

    assert await flaky() == "ok"
    assert calls["n"] == 3


@pytest.mark.asyncio
async def test_retry_async_gives_up_after_max_attempts():
    @retry_async(attempts=2, base_delay=0.01)
    async def always_fail():
        raise ConnectionError("permanent")

    with pytest.raises(ConnectionError):
        await always_fail()


@pytest.mark.asyncio
async def test_retry_async_does_not_retry_on_value_error():
    calls = {"n": 0}

    @retry_async(attempts=3, base_delay=0.01, retry_on=(ConnectionError,))
    async def bad_input():
        calls["n"] += 1
        raise ValueError("4xx")

    with pytest.raises(ValueError):
        await bad_input()
    assert calls["n"] == 1


@pytest.mark.asyncio
async def test_rate_limiter_caps_concurrency():
    limiter = RateLimiter(max_concurrent=2)
    in_flight = {"current": 0, "max": 0}

    async def worker():
        async with limiter:
            in_flight["current"] += 1
            in_flight["max"] = max(in_flight["max"], in_flight["current"])
            await asyncio.sleep(0.05)
            in_flight["current"] -= 1

    await asyncio.gather(*[worker() for _ in range(5)])
    assert in_flight["max"] == 2
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/unit/test_retry.py -v`
Expected: `ImportError`

- [ ] **Step 3: 实现 `sources/retry.py`**

```python
"""Async retry decorator + simple rate limiter."""

import asyncio
import functools
from collections.abc import Awaitable, Callable
from typing import ParamSpec, TypeVar

import structlog

P = ParamSpec("P")
R = TypeVar("R")

_log = structlog.get_logger(__name__)


def retry_async(
    *,
    attempts: int = 3,
    base_delay: float = 1.0,
    backoff: float = 2.0,
    retry_on: tuple[type[BaseException], ...] = (ConnectionError, TimeoutError, OSError),
) -> Callable[[Callable[P, Awaitable[R]]], Callable[P, Awaitable[R]]]:
    def decorator(fn: Callable[P, Awaitable[R]]) -> Callable[P, Awaitable[R]]:
        @functools.wraps(fn)
        async def wrapped(*args: P.args, **kwargs: P.kwargs) -> R:
            last: BaseException | None = None
            for attempt in range(1, attempts + 1):
                try:
                    return await fn(*args, **kwargs)
                except retry_on as e:
                    last = e
                    if attempt == attempts:
                        break
                    delay = base_delay * (backoff ** (attempt - 1))
                    _log.warning(
                        "retry.scheduling",
                        attempt=attempt,
                        max_attempts=attempts,
                        delay=delay,
                        error=str(e),
                    )
                    await asyncio.sleep(delay)
            assert last is not None
            raise last
        return wrapped
    return decorator


class RateLimiter:
    def __init__(self, max_concurrent: int):
        self._sem = asyncio.Semaphore(max_concurrent)

    async def __aenter__(self) -> None:
        await self._sem.acquire()

    async def __aexit__(self, *exc) -> None:
        self._sem.release()
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/unit/test_retry.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add src/investment_agent/sources/retry.py tests/unit/test_retry.py
git commit -m "feat: async retry decorator + rate limiter"
```

---

## Task 15: YFinanceSource — `fetch_daily_prices`

**Files:**
- Create: `src/investment_agent/sources/yfinance_source.py`
- Create: `tests/fixtures/yfinance_history.json`
- Create: `tests/unit/test_yfinance_source.py`

- [ ] **Step 1: 准备 fixture `tests/fixtures/yfinance_history.json`**

```json
{
  "AAPL": [
    {"date": "2025-06-02", "open": 196.5, "high": 198.2, "low": 195.8, "close": 197.4, "volume": 50123400, "adjusted_close": 197.4},
    {"date": "2025-06-03", "open": 197.5, "high": 199.0, "low": 196.9, "close": 198.7, "volume": 48000000, "adjusted_close": 198.7}
  ]
}
```

- [ ] **Step 2: 写测试 — `tests/unit/test_yfinance_source.py`**

```python
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from investment_agent.sources.yfinance_source import YFinanceSource


_FIXTURE = json.loads(
    (Path(__file__).parent.parent / "fixtures" / "yfinance_history.json").read_text()
)


def _fake_download_factory(payload: dict):
    def _fake_download(tickers, start, end, **kwargs):
        rows = []
        for t in (tickers if isinstance(tickers, list) else [tickers]):
            for r in payload.get(t, []):
                rows.append({**r, "ticker": t})
        df = pd.DataFrame(rows)
        if df.empty:
            return df
        df["date"] = pd.to_datetime(df["date"])
        df = df.set_index("date")
        return df
    return _fake_download


@pytest.mark.asyncio
async def test_fetch_daily_prices_parses_yfinance_dataframe(monkeypatch):
    import yfinance

    monkeypatch.setattr(
        yfinance, "download", _fake_download_factory(_FIXTURE)
    )

    src = YFinanceSource(max_concurrent=2)
    records = await src.fetch_daily_prices(
        tickers=["AAPL"], start=date(2025, 6, 2), end=date(2025, 6, 3)
    )
    assert len(records) == 2
    assert records[0].ticker == "AAPL"
    assert records[0].date == date(2025, 6, 2)
    assert float(records[0].close) == 197.4


@pytest.mark.asyncio
async def test_fetch_daily_prices_handles_empty_response(monkeypatch):
    import yfinance

    monkeypatch.setattr(yfinance, "download", _fake_download_factory({}))
    src = YFinanceSource(max_concurrent=2)
    out = await src.fetch_daily_prices(
        tickers=["GHOST"], start=date(2025, 6, 1), end=date(2025, 6, 2)
    )
    assert out == []
```

- [ ] **Step 3: 运行测试确认失败**

Run: `pytest tests/unit/test_yfinance_source.py -v`
Expected: `ImportError`

- [ ] **Step 4: 实现 `sources/yfinance_source.py`**

```python
"""yfinance-based DataSource implementation."""

import asyncio
from datetime import date, datetime
from decimal import Decimal

import pandas as pd
import yfinance as yf

from investment_agent.sources.records import (
    AnalystRatingRecord,
    DailyPriceRecord,
    FinancialReportRecord,
    FundamentalSnapshotRecord,
    ListingRef,
    NewsRecord,
)
from investment_agent.sources.retry import RateLimiter, retry_async


class YFinanceSource:
    name = "yfinance"
    supported_markets = ["NASDAQ", "NYSE", "HKEX", "TWSE", "SSE", "SZSE"]

    def __init__(self, max_concurrent: int = 10):
        self._limiter = RateLimiter(max_concurrent=max_concurrent)

    @retry_async(attempts=3, base_delay=1.0)
    async def fetch_daily_prices(
        self, tickers: list[str], start: date, end: date
    ) -> list[DailyPriceRecord]:
        async with self._limiter:
            df = await asyncio.to_thread(
                yf.download,
                tickers,
                start=start.isoformat(),
                end=(end.toordinal() + 1 and end).isoformat(),
                progress=False,
                auto_adjust=False,
                threads=False,
            )
        if df is None or df.empty:
            return []
        return _df_to_price_records(df, tickers)

    async def fetch_universe(
        self, universe_code: str, as_of: date
    ) -> list[ListingRef]:
        raise NotImplementedError("Implemented in Task 17")

    async def fetch_financial_report(
        self, ticker: str, periods: list[str]
    ) -> list[FinancialReportRecord]:
        raise NotImplementedError("Implemented in Task 18")

    async def fetch_fundamental_snapshot(
        self, ticker: str, as_of: date
    ) -> FundamentalSnapshotRecord:
        raise NotImplementedError("Implemented in Task 16")

    async def fetch_news(self, ticker: str, days: int) -> list[NewsRecord]:
        raise NotImplementedError("Implemented in Task 19")

    async def fetch_analyst_ratings(
        self, ticker: str, days: int
    ) -> list[AnalystRatingRecord]:
        raise NotImplementedError("Implemented in Task 20")


def _df_to_price_records(
    df: pd.DataFrame, tickers: list[str]
) -> list[DailyPriceRecord]:
    out: list[DailyPriceRecord] = []
    df = df.reset_index()

    if "ticker" in df.columns:
        ticker_col = "ticker"
    else:
        ticker_col = None

    for _, row in df.iterrows():
        ticker = row[ticker_col] if ticker_col else tickers[0]
        d = row["date"] if "date" in row.index else row["Date"]
        if isinstance(d, pd.Timestamp):
            d = d.date()
        out.append(
            DailyPriceRecord(
                ticker=str(ticker),
                date=d,
                open=Decimal(str(row.get("open", row.get("Open")))),
                high=Decimal(str(row.get("high", row.get("High")))),
                low=Decimal(str(row.get("low", row.get("Low")))),
                close=Decimal(str(row.get("close", row.get("Close")))),
                volume=int(row.get("volume", row.get("Volume", 0))),
                adjusted_close=Decimal(
                    str(row.get("adjusted_close", row.get("Adj Close", row.get("close", row.get("Close")))))
                ),
            )
        )
    return out
```

- [ ] **Step 5: 运行测试**

Run: `pytest tests/unit/test_yfinance_source.py -v`
Expected: 2 passed

- [ ] **Step 6: Commit**

```bash
git add src/investment_agent/sources/yfinance_source.py tests/fixtures/yfinance_history.json tests/unit/test_yfinance_source.py
git commit -m "feat: YFinanceSource.fetch_daily_prices with rate limiting"
```

---

## Task 16: YFinanceSource — `fetch_fundamental_snapshot`

**Files:**
- Modify: `src/investment_agent/sources/yfinance_source.py`
- Create: `tests/fixtures/yfinance_info.json`
- Modify: `tests/unit/test_yfinance_source.py`

- [ ] **Step 1: Fixture `tests/fixtures/yfinance_info.json`**

```json
{
  "AAPL": {
    "trailingPE": 32.5,
    "priceToBook": 51.2,
    "trailingPegRatio": 2.45,
    "priceToSalesTrailing12Months": 8.7,
    "returnOnEquity": 1.5612,
    "returnOnAssets": 0.2814,
    "freeCashflow": 99584000000,
    "marketCap": 3000000000000,
    "debtToEquity": 1.85,
    "totalRevenue": 391035000000
  }
}
```

- [ ] **Step 2: 写测试**

追加到 `tests/unit/test_yfinance_source.py`：

```python
import json
from datetime import date


def _fake_ticker_info(payload: dict):
    class FakeTicker:
        def __init__(self, ticker):
            self.info = payload.get(ticker, {})
    return FakeTicker


@pytest.mark.asyncio
async def test_fetch_fundamental_snapshot_parses_info(monkeypatch):
    import yfinance

    info = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "yfinance_info.json").read_text()
    )
    monkeypatch.setattr(yfinance, "Ticker", _fake_ticker_info(info))

    src = YFinanceSource(max_concurrent=2)
    snap = await src.fetch_fundamental_snapshot("AAPL", as_of=date(2025, 6, 3))
    assert snap.ticker == "AAPL"
    assert float(snap.pe) == 32.5
    assert float(snap.pb) == 51.2
    assert float(snap.roe) == pytest.approx(1.5612, rel=1e-4)
    assert float(snap.market_cap) == 3_000_000_000_000


@pytest.mark.asyncio
async def test_fetch_fundamental_snapshot_handles_missing_fields(monkeypatch):
    import yfinance

    monkeypatch.setattr(yfinance, "Ticker", _fake_ticker_info({"GHOST": {}}))
    src = YFinanceSource(max_concurrent=2)
    snap = await src.fetch_fundamental_snapshot("GHOST", as_of=date(2025, 6, 3))
    assert snap.pe is None
    assert snap.market_cap is None
```

- [ ] **Step 3: 运行测试确认失败**

Run: `pytest tests/unit/test_yfinance_source.py::test_fetch_fundamental_snapshot_parses_info -v`
Expected: `NotImplementedError`

- [ ] **Step 4: 实现 fetch_fundamental_snapshot**

替换 `yfinance_source.py` 中的 `fetch_fundamental_snapshot` 方法和顶部加一个工具函数：

```python
def _maybe_decimal(value) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except (ValueError, ArithmeticError):
        return None


# 替换 fetch_fundamental_snapshot:
@retry_async(attempts=3, base_delay=1.0)
async def fetch_fundamental_snapshot(
    self, ticker: str, as_of: date
) -> FundamentalSnapshotRecord:
    async with self._limiter:
        info = await asyncio.to_thread(lambda: yf.Ticker(ticker).info)
    info = info or {}

    market_cap = _maybe_decimal(info.get("marketCap"))
    fcf = _maybe_decimal(info.get("freeCashflow"))
    fcf_yield = None
    if fcf is not None and market_cap not in (None, Decimal(0)):
        fcf_yield = fcf / market_cap

    return FundamentalSnapshotRecord(
        ticker=ticker,
        date=as_of,
        pe=_maybe_decimal(info.get("trailingPE")),
        pb=_maybe_decimal(info.get("priceToBook")),
        peg=_maybe_decimal(info.get("trailingPegRatio")),
        ps=_maybe_decimal(info.get("priceToSalesTrailing12Months")),
        roe=_maybe_decimal(info.get("returnOnEquity")),
        roa=_maybe_decimal(info.get("returnOnAssets")),
        fcf_yield=fcf_yield,
        debt_to_equity=_maybe_decimal(info.get("debtToEquity")),
        market_cap=market_cap,
    )
```

- [ ] **Step 5: 运行测试**

Run: `pytest tests/unit/test_yfinance_source.py -v`
Expected: 4 passed

- [ ] **Step 6: Commit**

```bash
git add src/investment_agent/sources/yfinance_source.py tests/fixtures/yfinance_info.json tests/unit/test_yfinance_source.py
git commit -m "feat: YFinanceSource.fetch_fundamental_snapshot"
```

---

## Task 17: YFinanceSource — `fetch_universe` (NASDAQ-100)

**Files:**
- Modify: `src/investment_agent/sources/yfinance_source.py`
- Modify: `tests/unit/test_yfinance_source.py`

- [ ] **Step 1: 写测试**

追加到 `tests/unit/test_yfinance_source.py`：

```python
@pytest.mark.asyncio
async def test_fetch_universe_returns_nasdaq_100(monkeypatch):
    """Mock yfinance.Tickers / index member fetch."""
    # NDX index components come from a local YAML or pandas read_html;
    # for unit test we monkeypatch _fetch_nasdaq_100_tickers.
    from investment_agent.sources import yfinance_source as ys
    monkeypatch.setattr(
        ys, "_fetch_nasdaq_100_tickers",
        lambda: [("AAPL", "Apple Inc.", "TECH"), ("MSFT", "Microsoft Corporation", "TECH")],
    )
    src = YFinanceSource(max_concurrent=2)
    refs = await src.fetch_universe("NASDAQ_100", as_of=date(2025, 6, 3))
    assert len(refs) == 2
    assert refs[0].market_code == "NASDAQ"
    assert refs[0].ticker == "AAPL"
    assert refs[0].company_name == "Apple Inc."


@pytest.mark.asyncio
async def test_fetch_universe_unknown_code_raises():
    src = YFinanceSource(max_concurrent=2)
    with pytest.raises(ValueError, match="Unsupported universe"):
        await src.fetch_universe("UNKNOWN_INDEX", as_of=date(2025, 6, 3))
```

- [ ] **Step 2: 实现 fetch_universe + helper**

在 `yfinance_source.py` 顶部加：

```python
def _fetch_nasdaq_100_tickers() -> list[tuple[str, str, str]]:
    """Fetch NDX components from Wikipedia table.

    Returns list of (ticker, company_name, sector_code).
    """
    import pandas as pd

    tables = pd.read_html("https://en.wikipedia.org/wiki/Nasdaq-100")
    # Wikipedia 's NDX page typically has constituents in one of the early tables.
    for tbl in tables:
        cols = {c.lower() for c in tbl.columns.astype(str)}
        if "ticker" in cols and ("company" in cols or "security" in cols):
            df = tbl
            ticker_col = next(c for c in df.columns if c.lower() == "ticker")
            name_col = next(c for c in df.columns if c.lower() in ("company", "security"))
            sector_col = next(
                (c for c in df.columns if "sector" in str(c).lower()), None
            )
            out: list[tuple[str, str, str]] = []
            for _, row in df.iterrows():
                ticker = str(row[ticker_col]).strip()
                name = str(row[name_col]).strip()
                sector = str(row[sector_col]).strip() if sector_col else "UNKNOWN"
                sector_code = _normalize_sector(sector)
                out.append((ticker, name, sector_code))
            return out
    raise RuntimeError("Could not locate NASDAQ-100 components table")


def _normalize_sector(raw: str) -> str:
    s = raw.lower()
    if "tech" in s or "information" in s:
        return "TECH"
    if "communication" in s:
        return "COMMUNICATION"
    if "consumer discretionary" in s:
        return "CONSUMER_DISCRETIONARY"
    if "consumer staples" in s:
        return "CONSUMER_STAPLES"
    if "health" in s:
        return "HEALTHCARE"
    if "industrial" in s:
        return "INDUSTRIALS"
    if "material" in s:
        return "MATERIALS"
    if "energy" in s:
        return "ENERGY"
    if "financial" in s:
        return "FINANCIALS"
    if "utilities" in s:
        return "UTILITIES"
    if "real estate" in s:
        return "REAL_ESTATE"
    return "UNKNOWN"
```

替换 `fetch_universe`：

```python
async def fetch_universe(
    self, universe_code: str, as_of: date
) -> list[ListingRef]:
    if universe_code != "NASDAQ_100":
        raise ValueError(f"Unsupported universe code: {universe_code}")
    async with self._limiter:
        tickers = await asyncio.to_thread(_fetch_nasdaq_100_tickers)
    return [
        ListingRef(
            market_code="NASDAQ",
            ticker=t,
            company_name=n,
            sector_code=s,
        )
        for (t, n, s) in tickers
    ]
```

- [ ] **Step 3: 运行测试**

Run: `pytest tests/unit/test_yfinance_source.py -v`
Expected: 6 passed

- [ ] **Step 4: Commit**

```bash
git add src/investment_agent/sources/yfinance_source.py tests/unit/test_yfinance_source.py
git commit -m "feat: YFinanceSource.fetch_universe for NASDAQ-100 via Wikipedia"
```

---

## Task 18: YFinanceSource — `fetch_financial_report`

**Files:**
- Modify: `src/investment_agent/sources/yfinance_source.py`
- Create: `tests/fixtures/yfinance_financials.json`
- Modify: `tests/unit/test_yfinance_source.py`

- [ ] **Step 1: Fixture `tests/fixtures/yfinance_financials.json`**

```json
{
  "AAPL": {
    "quarterly_income_stmt": {
      "2025-03-31": {"Total Revenue": 95360000000, "Net Income": 23636000000},
      "2024-12-31": {"Total Revenue": 124300000000, "Net Income": 36330000000}
    },
    "quarterly_cashflow": {
      "2025-03-31": {"Free Cash Flow": 24400000000},
      "2024-12-31": {"Free Cash Flow": 39100000000}
    },
    "quarterly_balance_sheet": {
      "2025-03-31": {"Total Assets": 364980000000, "Stockholders Equity": 66050000000}
    }
  }
}
```

- [ ] **Step 2: 写测试**

```python
@pytest.mark.asyncio
async def test_fetch_financial_report_parses_quarterly(monkeypatch):
    import yfinance
    import pandas as pd
    from datetime import date

    fixture = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "yfinance_financials.json").read_text()
    )

    class FakeTicker:
        def __init__(self, ticker):
            self._data = fixture[ticker]

        @property
        def quarterly_income_stmt(self):
            return pd.DataFrame(self._data["quarterly_income_stmt"])

        @property
        def quarterly_cashflow(self):
            return pd.DataFrame(self._data["quarterly_cashflow"])

        @property
        def quarterly_balance_sheet(self):
            return pd.DataFrame(self._data["quarterly_balance_sheet"])

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)

    src = YFinanceSource(max_concurrent=2)
    reports = await src.fetch_financial_report("AAPL", periods=["2025-Q1"])
    # We expect Q1 2025 (period_end 2025-03-31) to match
    q1 = [r for r in reports if r.fiscal_period == "2025-Q1"]
    assert q1, f"No Q1 report found in {[r.fiscal_period for r in reports]}"
    assert float(q1[0].revenue) == 95_360_000_000
    assert float(q1[0].net_income) == 23_636_000_000
    assert float(q1[0].fcf) == 24_400_000_000
```

- [ ] **Step 3: 实现 fetch_financial_report**

替换 yfinance_source.py 中的方法：

```python
@retry_async(attempts=3, base_delay=1.0)
async def fetch_financial_report(
    self, ticker: str, periods: list[str]
) -> list[FinancialReportRecord]:
    """Fetch quarterly financial statements.

    `periods` is a list like ['2025-Q1', '2025-Q2'] used to filter which quarters to return.
    """
    async with self._limiter:
        t = await asyncio.to_thread(yf.Ticker, ticker)
        income = await asyncio.to_thread(lambda: t.quarterly_income_stmt)
        cashflow = await asyncio.to_thread(lambda: t.quarterly_cashflow)
        balance = await asyncio.to_thread(lambda: t.quarterly_balance_sheet)

    out: list[FinancialReportRecord] = []
    for col in income.columns:
        period_end = pd.to_datetime(col).date()
        period_label = f"{period_end.year}-Q{(period_end.month - 1) // 3 + 1}"
        if periods and period_label not in periods:
            continue
        rev = income.at["Total Revenue", col] if "Total Revenue" in income.index else None
        ni = income.at["Net Income", col] if "Net Income" in income.index else None
        fcf_val = None
        if "Free Cash Flow" in cashflow.index and col in cashflow.columns:
            fcf_val = cashflow.at["Free Cash Flow", col]
        ta = None
        te = None
        if col in balance.columns:
            if "Total Assets" in balance.index:
                ta = balance.at["Total Assets", col]
            if "Stockholders Equity" in balance.index:
                te = balance.at["Stockholders Equity", col]

        out.append(
            FinancialReportRecord(
                ticker=ticker,
                fiscal_period=period_label,
                period_end=period_end,
                revenue=_maybe_decimal(rev),
                net_income=_maybe_decimal(ni),
                fcf=_maybe_decimal(fcf_val),
                total_assets=_maybe_decimal(ta),
                total_equity=_maybe_decimal(te),
                raw_data={
                    "income": income[col].dropna().to_dict() if col in income.columns else {},
                    "cashflow": cashflow[col].dropna().to_dict() if col in cashflow.columns else {},
                    "balance": balance[col].dropna().to_dict() if col in balance.columns else {},
                },
            )
        )
    return out
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/unit/test_yfinance_source.py -v`
Expected: 7 passed

- [ ] **Step 5: Commit**

```bash
git add src/investment_agent/sources/yfinance_source.py tests/fixtures/yfinance_financials.json tests/unit/test_yfinance_source.py
git commit -m "feat: YFinanceSource.fetch_financial_report (quarterly)"
```

---

## Task 19: YFinanceSource — `fetch_news`

**Files:**
- Modify: `src/investment_agent/sources/yfinance_source.py`
- Create: `tests/fixtures/yfinance_news.json`
- Modify: `tests/unit/test_yfinance_source.py`

- [ ] **Step 1: Fixture `tests/fixtures/yfinance_news.json`**

```json
{
  "AAPL": [
    {
      "title": "Apple announces new AI features in iOS 19",
      "link": "https://example.com/news/1",
      "publisher": "Reuters",
      "providerPublishTime": 1748876400,
      "summary": "Apple unveiled significant on-device AI capabilities..."
    },
    {
      "title": "Apple supply chain risk: Foxconn warns of slower output",
      "link": "https://example.com/news/2",
      "publisher": "Bloomberg",
      "providerPublishTime": 1748789000,
      "summary": "Key supplier Foxconn lowered Q3 guidance..."
    }
  ]
}
```

- [ ] **Step 2: 写测试**

```python
@pytest.mark.asyncio
async def test_fetch_news_parses_recent_items(monkeypatch):
    import yfinance
    fixture = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "yfinance_news.json").read_text()
    )

    class FakeTicker:
        def __init__(self, ticker):
            self.news = fixture[ticker]

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)

    src = YFinanceSource(max_concurrent=2)
    items = await src.fetch_news("AAPL", days=7)
    assert len(items) == 2
    assert items[0].title.startswith("Apple announces")
    assert items[0].source == "Reuters"
    assert items[0].url == "https://example.com/news/1"
```

- [ ] **Step 3: 实现 fetch_news**

```python
@retry_async(attempts=3, base_delay=1.0)
async def fetch_news(self, ticker: str, days: int) -> list[NewsRecord]:
    from datetime import timedelta, timezone
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)

    async with self._limiter:
        raw = await asyncio.to_thread(lambda: yf.Ticker(ticker).news or [])

    out: list[NewsRecord] = []
    for item in raw:
        ts = item.get("providerPublishTime")
        if ts is None:
            continue
        published = datetime.fromtimestamp(int(ts), tz=timezone.utc)
        if published < cutoff:
            continue
        out.append(
            NewsRecord(
                ticker=ticker,
                published_at=published,
                source=str(item.get("publisher") or "yfinance"),
                title=str(item.get("title") or "(no title)"),
                url=str(item.get("link") or ""),
                summary=item.get("summary"),
            )
        )
    return out
```

- [ ] **Step 4: 运行测试 (注意 fixture 时间戳要是相对当前足够新；测试用 freezegun 锁时间)**

修改测试为：

```python
@pytest.mark.asyncio
async def test_fetch_news_parses_recent_items(monkeypatch):
    import yfinance
    from freezegun import freeze_time
    fixture = json.loads(
        (Path(__file__).parent.parent / "fixtures" / "yfinance_news.json").read_text()
    )

    class FakeTicker:
        def __init__(self, ticker):
            self.news = fixture[ticker]

    monkeypatch.setattr(yfinance, "Ticker", FakeTicker)

    src = YFinanceSource(max_concurrent=2)
    with freeze_time("2025-06-03 12:00:00"):
        items = await src.fetch_news("AAPL", days=7)
    assert len(items) == 2
```

Run: `pytest tests/unit/test_yfinance_source.py -v`
Expected: 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/investment_agent/sources/yfinance_source.py tests/fixtures/yfinance_news.json tests/unit/test_yfinance_source.py
git commit -m "feat: YFinanceSource.fetch_news"
```

---

## Task 20: YFinanceSource — `fetch_analyst_ratings`

**Files:**
- Modify: `src/investment_agent/sources/yfinance_source.py`
- Modify: `tests/unit/test_yfinance_source.py`

- [ ] **Step 1: 写测试**

```python
@pytest.mark.asyncio
async def test_fetch_analyst_ratings_parses_upgrades_downgrades(monkeypatch):
    import yfinance
    import pandas as pd
    from datetime import datetime
    from freezegun import freeze_time

    class FakeTicker:
        upgrades_downgrades = pd.DataFrame(
            {
                "Firm": ["Goldman Sachs", "Morgan Stanley"],
                "ToGrade": ["Buy", "Overweight"],
                "FromGrade": ["Neutral", "Equal-Weight"],
                "Action": ["up", "up"],
            },
            index=pd.to_datetime(["2025-06-02", "2025-05-29"]),
        )

    monkeypatch.setattr(yfinance, "Ticker", lambda t: FakeTicker())

    src = YFinanceSource(max_concurrent=2)
    with freeze_time("2025-06-03 12:00:00"):
        items = await src.fetch_analyst_ratings("AAPL", days=7)
    assert len(items) == 2
    assert items[0].firm in {"Goldman Sachs", "Morgan Stanley"}
    assert items[0].action in {"up", "down", "main", "init"}
```

- [ ] **Step 2: 实现 fetch_analyst_ratings**

```python
@retry_async(attempts=3, base_delay=1.0)
async def fetch_analyst_ratings(
    self, ticker: str, days: int
) -> list[AnalystRatingRecord]:
    from datetime import timedelta

    async with self._limiter:
        df = await asyncio.to_thread(lambda: yf.Ticker(ticker).upgrades_downgrades)
    if df is None or df.empty:
        return []

    cutoff = date.today() - timedelta(days=days)
    out: list[AnalystRatingRecord] = []
    df = df.reset_index().rename(columns={"index": "date"})
    if "date" not in df.columns and "Date" in df.columns:
        df = df.rename(columns={"Date": "date"})
    for _, row in df.iterrows():
        d = pd.to_datetime(row["date"]).date()
        if d < cutoff:
            continue
        out.append(
            AnalystRatingRecord(
                ticker=ticker,
                date=d,
                firm=str(row.get("Firm") or "Unknown"),
                action=str(row.get("Action") or "main"),
                from_rating=row.get("FromGrade"),
                to_rating=row.get("ToGrade"),
                price_target=None,
            )
        )
    return out
```

- [ ] **Step 3: 运行测试**

Run: `pytest tests/unit/test_yfinance_source.py -v`
Expected: 9 passed

- [ ] **Step 4: Commit**

```bash
git add src/investment_agent/sources/yfinance_source.py tests/unit/test_yfinance_source.py
git commit -m "feat: YFinanceSource.fetch_analyst_ratings"
```

---

## Task 21: 跨市场 Ticker 后缀支持验证

**Files:**
- Modify: `tests/unit/test_yfinance_source.py`

- [ ] **Step 1: 测试跨市场 ticker 解析（仅验证 source 接受这些 ticker，不必真实请求）**

```python
@pytest.mark.asyncio
async def test_cross_market_tickers_accepted(monkeypatch):
    """Verify .TW / .HK / .SZ / .SS ticker suffixes are accepted."""
    import yfinance

    monkeypatch.setattr(yfinance, "download", lambda *a, **k: __import__("pandas").DataFrame())

    src = YFinanceSource(max_concurrent=2)
    for ticker in ["2330.TW", "0700.HK", "002475.SZ", "600519.SS"]:
        result = await src.fetch_daily_prices(
            tickers=[ticker], start=date(2025, 6, 1), end=date(2025, 6, 2)
        )
        assert result == []  # empty df → empty result, but no exception
```

- [ ] **Step 2: 运行测试**

Run: `pytest tests/unit/test_yfinance_source.py::test_cross_market_tickers_accepted -v`
Expected: 1 passed

- [ ] **Step 3: Commit**

```bash
git add tests/unit/test_yfinance_source.py
git commit -m "test: verify cross-market ticker suffixes accepted by YFinanceSource"
```

---

## Task 22: CLI 主入口 + `data` 命令组（seed + backfill）

**Files:**
- Create: `src/investment_agent/cli/__init__.py`
- Create: `src/investment_agent/cli/main.py`
- Create: `src/investment_agent/cli/commands/__init__.py`
- Create: `src/investment_agent/cli/commands/data.py`
- Create: `tests/unit/test_cli_data.py`

- [ ] **Step 1: 创建包**

```bash
mkdir -p src/investment_agent/cli/commands
touch src/investment_agent/cli/__init__.py src/investment_agent/cli/commands/__init__.py
```

- [ ] **Step 2: 实现 `cli/main.py`**

```python
"""Typer entry point: `ia` command."""

import typer

from investment_agent.cli.commands import data as data_cmd

app = typer.Typer(help="Investment Agent CLI")
app.add_typer(data_cmd.app, name="data", help="Market data ingestion commands")


@app.callback()
def _bootstrap() -> None:
    from investment_agent.config import Settings
    from investment_agent.logging import configure_logging
    from investment_agent.storage.database import init_engine

    settings = Settings()
    configure_logging(settings.log_level)
    init_engine(settings.database_url)


if __name__ == "__main__":
    app()
```

- [ ] **Step 3: 实现 `cli/commands/data.py`**

```python
"""data sub-commands: seed, backfill, refresh."""

import asyncio
from datetime import date
from pathlib import Path

import typer
from rich import print as rprint

app = typer.Typer(help="Market data commands")


@app.command("seed")
def seed_universe(
    universe_yaml: Path = typer.Argument(
        Path("config/universes/nasdaq_100.yaml"),
        help="Path to universe YAML file",
    ),
) -> None:
    """Seed universe + constituents from YAML config."""
    from investment_agent.storage.seeders.nasdaq_100 import seed_nasdaq_100

    n = asyncio.run(seed_nasdaq_100(universe_yaml))
    rprint(f"[green]Seeded {n} new listings[/green]")


@app.command("backfill")
def backfill(
    tickers: str = typer.Option(..., help="Comma-separated ticker list"),
    start: str = typer.Option(..., help="ISO date, e.g. 2024-01-01"),
    end: str = typer.Option(date.today().isoformat(), help="ISO date"),
) -> None:
    """Backfill daily prices for the given tickers between dates."""
    from investment_agent.sources.yfinance_source import YFinanceSource
    from investment_agent.storage.database import session_scope
    from investment_agent.storage.models.market_data import DailyPrice
    from investment_agent.storage.models.reference import Listing
    from sqlalchemy import select

    ticker_list = [t.strip() for t in tickers.split(",") if t.strip()]
    start_d = date.fromisoformat(start)
    end_d = date.fromisoformat(end)

    async def _do() -> int:
        src = YFinanceSource(max_concurrent=10)
        records = await src.fetch_daily_prices(ticker_list, start_d, end_d)
        n = 0
        async with session_scope() as session:
            for ticker in set(r.ticker for r in records):
                stmt = select(Listing).where(Listing.ticker == ticker)
                listing = (await session.execute(stmt)).scalar_one_or_none()
                if listing is None:
                    rprint(f"[yellow]Listing not found for {ticker}, skipping[/yellow]")
                    continue
                for r in (rr for rr in records if rr.ticker == ticker):
                    existing = await session.get(DailyPrice, (listing.id, r.date))
                    if existing is None:
                        session.add(
                            DailyPrice(
                                listing_id=listing.id, date=r.date,
                                open=r.open, high=r.high, low=r.low, close=r.close,
                                volume=r.volume, adjusted_close=r.adjusted_close,
                            )
                        )
                        n += 1
        return n

    written = asyncio.run(_do())
    rprint(f"[green]Wrote {written} daily price rows[/green]")


@app.command("refresh")
def refresh(ticker: str = typer.Argument(...)) -> None:
    """Refresh today's daily price for a single ticker."""
    today = date.today()
    backfill(tickers=ticker, start=today.isoformat(), end=today.isoformat())
```

- [ ] **Step 4: 写 CLI 测试 `tests/unit/test_cli_data.py`**

```python
from typer.testing import CliRunner


def test_cli_help_lists_data_commands():
    from investment_agent.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["data", "--help"])
    assert result.exit_code == 0
    assert "seed" in result.output
    assert "backfill" in result.output
    assert "refresh" in result.output
```

- [ ] **Step 5: 运行测试**

Run: `pytest tests/unit/test_cli_data.py -v`
Expected: 1 passed

- [ ] **Step 6: 实际跑一次 seed (本地需要 .env 已配置)**

```bash
cp .env.example .env
# 编辑 .env 填入真实 API_KEY 等
alembic upgrade head
ia data seed
```

Expected: `Seeded 10 new listings`

- [ ] **Step 7: Commit**

```bash
git add src/investment_agent/cli/ tests/unit/test_cli_data.py
git commit -m "feat: CLI entry point with data seed/backfill/refresh"
```

---

# Phase 2: LLMClient + Pydantic-AI 集成

## Task 23: 模型成本表

**Files:**
- Create: `src/investment_agent/llm/__init__.py`
- Create: `src/investment_agent/llm/pricing.py`
- Create: `tests/unit/test_pricing.py`

- [ ] **Step 1: 创建包**

```bash
mkdir -p src/investment_agent/llm
touch src/investment_agent/llm/__init__.py
```

- [ ] **Step 2: 写测试 `tests/unit/test_pricing.py`**

```python
from decimal import Decimal

from investment_agent.llm.pricing import calculate_cost, get_pricing


def test_pricing_known_model():
    p = get_pricing("gpt-4o-mini")
    assert p.input_per_1m > 0
    assert p.output_per_1m > 0


def test_pricing_unknown_model_returns_default():
    p = get_pricing("never-heard-of-this-model")
    # default: assume 1.0 USD per 1M input, 3.0 USD per 1M output (conservative)
    assert p.input_per_1m == Decimal("1.0")
    assert p.output_per_1m == Decimal("3.0")


def test_calculate_cost():
    cost = calculate_cost(
        model="gpt-4o-mini", prompt_tokens=10_000, completion_tokens=2_000
    )
    # gpt-4o-mini: $0.15/M input, $0.60/M output
    # 10k * 0.15/M + 2k * 0.60/M = 0.0015 + 0.0012 = 0.0027
    assert cost == Decimal("0.0027")
```

- [ ] **Step 3: 实现 `llm/pricing.py`**

```python
"""Per-model pricing (USD per 1M tokens). Keep tightly aligned to provider docs."""

from dataclasses import dataclass
from decimal import Decimal


@dataclass(frozen=True)
class ModelPricing:
    input_per_1m: Decimal
    output_per_1m: Decimal


_PRICING: dict[str, ModelPricing] = {
    "gpt-5":           ModelPricing(Decimal("5.00"),  Decimal("15.00")),
    "gpt-4o":          ModelPricing(Decimal("2.50"),  Decimal("10.00")),
    "gpt-4o-mini":     ModelPricing(Decimal("0.15"),  Decimal("0.60")),
    "claude-opus-4-7": ModelPricing(Decimal("15.00"), Decimal("75.00")),
    "claude-sonnet-4-6": ModelPricing(Decimal("3.00"), Decimal("15.00")),
    "deepseek-chat":   ModelPricing(Decimal("0.14"),  Decimal("0.28")),
    "deepseek-reasoner": ModelPricing(Decimal("0.55"), Decimal("2.19")),
}

_DEFAULT = ModelPricing(Decimal("1.0"), Decimal("3.0"))


def get_pricing(model: str) -> ModelPricing:
    return _PRICING.get(model, _DEFAULT)


def calculate_cost(
    *, model: str, prompt_tokens: int, completion_tokens: int
) -> Decimal:
    p = get_pricing(model)
    return (
        Decimal(prompt_tokens) * p.input_per_1m / Decimal(1_000_000)
        + Decimal(completion_tokens) * p.output_per_1m / Decimal(1_000_000)
    )
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/unit/test_pricing.py -v`
Expected: 3 passed

- [ ] **Step 5: Commit**

```bash
git add src/investment_agent/llm/__init__.py src/investment_agent/llm/pricing.py tests/unit/test_pricing.py
git commit -m "feat: LLM model pricing table + cost calculator"
```

---

## Task 24: LLMCall 审计写入

**Files:**
- Create: `src/investment_agent/llm/tracking.py`
- Create: `tests/unit/test_llm_tracking.py`

- [ ] **Step 1: 写测试 `tests/unit/test_llm_tracking.py`**

```python
from datetime import datetime, timezone
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.llm.tracking import record_llm_call
from investment_agent.storage.models.ops import LLMCall


@pytest.mark.asyncio
async def test_record_llm_call_writes_row(db_session):
    await record_llm_call(
        db_session,
        purpose="strategy_score",
        model="gpt-4o-mini",
        provider="openai",
        prompt_tokens=1000,
        completion_tokens=200,
        started_at=datetime(2025, 6, 3, tzinfo=timezone.utc),
        latency_ms=1234,
        success=True,
        error_message=None,
        pipeline_run_id=None,
    )
    rows = (await db_session.execute(select(LLMCall))).scalars().all()
    assert len(rows) == 1
    assert rows[0].purpose == "strategy_score"
    assert rows[0].cost_usd == Decimal("0.00027")  # 0.15 + 0.12 per 1M


@pytest.mark.asyncio
async def test_record_llm_call_failure(db_session):
    await record_llm_call(
        db_session,
        purpose="deep_analysis",
        model="gpt-5",
        provider="openai",
        prompt_tokens=0,
        completion_tokens=0,
        started_at=datetime(2025, 6, 3, tzinfo=timezone.utc),
        latency_ms=50,
        success=False,
        error_message="rate limited",
        pipeline_run_id=None,
    )
    rows = (await db_session.execute(select(LLMCall))).scalars().all()
    assert len(rows) == 1
    assert rows[0].success is False
    assert rows[0].error_message == "rate limited"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `pytest tests/unit/test_llm_tracking.py -v`
Expected: `ImportError`

- [ ] **Step 3: 实现 `llm/tracking.py`**

```python
"""Insert LLMCall audit rows."""

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.llm.pricing import calculate_cost
from investment_agent.storage.models.ops import LLMCall


async def record_llm_call(
    session: AsyncSession,
    *,
    purpose: str,
    model: str,
    provider: str,
    prompt_tokens: int,
    completion_tokens: int,
    started_at: datetime,
    latency_ms: int,
    success: bool,
    error_message: str | None = None,
    pipeline_run_id: int | None = None,
) -> None:
    cost = calculate_cost(
        model=model,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
    )
    row = LLMCall(
        pipeline_run_id=pipeline_run_id,
        purpose=purpose,
        model=model,
        provider=provider,
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        cost_usd=cost,
        latency_ms=latency_ms,
        started_at=started_at,
        success=success,
        error_message=error_message,
    )
    session.add(row)
    await session.flush()
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/unit/test_llm_tracking.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/investment_agent/llm/tracking.py tests/unit/test_llm_tracking.py
git commit -m "feat: LLMCall audit writer"
```

---

## Task 25: LLMClient Protocol + OpenAICompatibleClient

**Files:**
- Modify: `src/investment_agent/domain/protocols.py`
- Create: `src/investment_agent/llm/openai_compatible.py`
- Create: `tests/unit/test_llm_client.py`

- [ ] **Step 1: 扩展 `domain/protocols.py` 加入 LLMClient Protocol + DTO**

在文件末尾追加：

```python
from typing import Any

from pydantic import BaseModel


class CompletionResult(BaseModel):
    text: str
    output: Any | None = None
    prompt_tokens: int
    completion_tokens: int
    latency_ms: int
    model: str
    provider: str


@runtime_checkable
class LLMClient(Protocol):
    tier_name: str

    async def complete(
        self,
        *,
        system_prompt: str,
        user_message: str,
        output_model: type[BaseModel] | None = None,
        tools: list | None = None,
        purpose: str,
        pipeline_run_id: int | None = None,
    ) -> CompletionResult: ...
```

- [ ] **Step 2: 写测试 `tests/unit/test_llm_client.py`**

```python
from datetime import datetime, timezone
from decimal import Decimal

import pytest
import respx
from httpx import Response
from pydantic import BaseModel

from investment_agent.config import LLMTierConfig
from investment_agent.llm.openai_compatible import OpenAICompatibleClient


class _Score(BaseModel):
    score: int
    rationale: str


@pytest.mark.asyncio
@respx.mock
async def test_complete_returns_plain_text(db_session):
    """No output_model — direct string."""
    respx.post("https://api.test.example/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": "hi there"}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
            },
        )
    )
    cfg = LLMTierConfig(
        base_url="https://api.test.example/v1", api_key="sk-test", model="test-model"
    )
    client = OpenAICompatibleClient(tier_name="tier2", config=cfg, session=db_session)
    out = await client.complete(
        system_prompt="you are helpful",
        user_message="say hi",
        purpose="test_call",
    )
    assert out.text == "hi there"
    assert out.prompt_tokens == 5
    assert out.completion_tokens == 2


@pytest.mark.asyncio
@respx.mock
async def test_complete_with_output_model_parses_json(db_session):
    respx.post("https://api.test.example/v1/chat/completions").mock(
        return_value=Response(
            200,
            json={
                "id": "x",
                "object": "chat.completion",
                "created": 1,
                "model": "test-model",
                "choices": [
                    {"index": 0, "message": {"role": "assistant", "content": '{"score": 87, "rationale": "solid fundamentals"}'}, "finish_reason": "stop"}
                ],
                "usage": {"prompt_tokens": 50, "completion_tokens": 20, "total_tokens": 70},
            },
        )
    )
    cfg = LLMTierConfig(
        base_url="https://api.test.example/v1", api_key="sk-test", model="test-model"
    )
    client = OpenAICompatibleClient(tier_name="tier2", config=cfg, session=db_session)
    out = await client.complete(
        system_prompt="score this",
        user_message="AAPL",
        output_model=_Score,
        purpose="strategy_score",
    )
    assert isinstance(out.output, _Score)
    assert out.output.score == 87
    assert out.output.rationale == "solid fundamentals"
```

- [ ] **Step 3: 实现 `llm/openai_compatible.py`**

```python
"""LLMClient implementation backed by openai SDK (OpenAI-compatible)."""

import time
from datetime import datetime, timezone
from typing import Any

from openai import AsyncOpenAI
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import LLMTierConfig
from investment_agent.domain.protocols import CompletionResult
from investment_agent.llm.tracking import record_llm_call


class OpenAICompatibleClient:
    def __init__(
        self,
        *,
        tier_name: str,
        config: LLMTierConfig,
        session: AsyncSession,
    ) -> None:
        self.tier_name = tier_name
        self._config = config
        self._session = session
        self._client = AsyncOpenAI(
            base_url=config.base_url, api_key=config.api_key
        )

    async def complete(
        self,
        *,
        system_prompt: str,
        user_message: str,
        output_model: type[BaseModel] | None = None,
        tools: list | None = None,
        purpose: str,
        pipeline_run_id: int | None = None,
    ) -> CompletionResult:
        started = datetime.now(timezone.utc)
        t0 = time.perf_counter()
        success = False
        error_msg: str | None = None
        prompt_tokens = completion_tokens = 0
        text = ""
        parsed: Any | None = None

        try:
            resp = await self._client.chat.completions.create(
                model=self._config.model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_message},
                ],
                response_format={"type": "json_object"} if output_model else None,
            )
            text = resp.choices[0].message.content or ""
            prompt_tokens = resp.usage.prompt_tokens if resp.usage else 0
            completion_tokens = resp.usage.completion_tokens if resp.usage else 0
            if output_model is not None:
                parsed = output_model.model_validate_json(text)
            success = True
        except Exception as e:
            error_msg = str(e)
            raise
        finally:
            latency_ms = int((time.perf_counter() - t0) * 1000)
            await record_llm_call(
                self._session,
                purpose=purpose,
                model=self._config.model,
                provider=_provider_name(self._config.base_url),
                prompt_tokens=prompt_tokens,
                completion_tokens=completion_tokens,
                started_at=started,
                latency_ms=latency_ms,
                success=success,
                error_message=error_msg,
                pipeline_run_id=pipeline_run_id,
            )

        return CompletionResult(
            text=text,
            output=parsed,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            latency_ms=latency_ms,
            model=self._config.model,
            provider=_provider_name(self._config.base_url),
        )


def _provider_name(base_url: str) -> str:
    if "openai.com" in base_url:
        return "openai"
    if "deepseek" in base_url:
        return "deepseek"
    if "anthropic" in base_url:
        return "anthropic"
    if "siliconflow" in base_url:
        return "siliconflow"
    if "openrouter" in base_url:
        return "openrouter"
    return base_url
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/unit/test_llm_client.py -v`
Expected: 2 passed

- [ ] **Step 5: Commit**

```bash
git add src/investment_agent/domain/protocols.py src/investment_agent/llm/openai_compatible.py tests/unit/test_llm_client.py
git commit -m "feat: OpenAICompatibleClient with LLMCall audit"
```

---

## Task 26: Pydantic-AI Agent 集成 — 工厂方法

**Files:**
- Create: `src/investment_agent/llm/agent_factory.py`
- Create: `tests/unit/test_agent_factory.py`

- [ ] **Step 1: 写测试 `tests/unit/test_agent_factory.py`**

```python
import pytest
from pydantic import BaseModel
from pydantic_ai import Agent

from investment_agent.config import LLMTierConfig
from investment_agent.llm.agent_factory import build_agent


class _Score(BaseModel):
    score: int


def test_build_agent_returns_pydantic_ai_agent():
    cfg = LLMTierConfig(
        base_url="https://api.openai.com/v1", api_key="sk-x", model="gpt-4o-mini"
    )
    agent = build_agent(
        config=cfg, system_prompt="you are a scorer", output_type=_Score
    )
    assert isinstance(agent, Agent)


def test_build_agent_without_output_type():
    cfg = LLMTierConfig(
        base_url="https://api.deepseek.com/v1", api_key="sk-x", model="deepseek-chat"
    )
    agent = build_agent(config=cfg, system_prompt="hello", output_type=None)
    assert isinstance(agent, Agent)
```

- [ ] **Step 2: 实现 `llm/agent_factory.py`**

```python
"""Build pydantic-ai Agent instances configured against our LLM tier config."""

from typing import TypeVar

from pydantic import BaseModel
from pydantic_ai import Agent
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.providers.openai import OpenAIProvider

from investment_agent.config import LLMTierConfig

OutputT = TypeVar("OutputT", bound=BaseModel)


def build_agent(
    *,
    config: LLMTierConfig,
    system_prompt: str,
    output_type: type[OutputT] | None = None,
) -> Agent:
    model = OpenAIModel(
        model_name=config.model,
        provider=OpenAIProvider(base_url=config.base_url, api_key=config.api_key),
    )
    kwargs = {"model": model, "system_prompt": system_prompt}
    if output_type is not None:
        kwargs["output_type"] = output_type
    return Agent(**kwargs)
```

- [ ] **Step 3: 运行测试**

Run: `pytest tests/unit/test_agent_factory.py -v`
Expected: 2 passed

- [ ] **Step 4: Commit**

```bash
git add src/investment_agent/llm/agent_factory.py tests/unit/test_agent_factory.py
git commit -m "feat: pydantic-ai Agent factory tied to our tier config"
```

---

## Task 27: CLI `admin cost` — 月度成本查询

**Files:**
- Create: `src/investment_agent/cli/commands/admin.py`
- Modify: `src/investment_agent/cli/main.py`
- Create: `tests/unit/test_cli_admin.py`

- [ ] **Step 1: 实现 `cli/commands/admin.py`**

```python
"""admin sub-commands: cost summary, db migrate."""

import asyncio
from datetime import date, datetime, timezone
from decimal import Decimal

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import func, select

from investment_agent.storage.database import session_scope
from investment_agent.storage.models.ops import LLMCall

app = typer.Typer(help="Admin commands")
_console = Console()


@app.command("cost")
def cost_summary(
    month: str = typer.Option(
        date.today().strftime("%Y-%m"),
        help="Month in YYYY-MM format",
    ),
) -> None:
    """Print monthly LLM cost grouped by model/provider/purpose."""
    year, mo = month.split("-")
    start = datetime(int(year), int(mo), 1, tzinfo=timezone.utc)
    if int(mo) == 12:
        end = datetime(int(year) + 1, 1, 1, tzinfo=timezone.utc)
    else:
        end = datetime(int(year), int(mo) + 1, 1, tzinfo=timezone.utc)

    async def _run() -> list[tuple[str, str, str, int, int, Decimal, int]]:
        async with session_scope() as session:
            stmt = (
                select(
                    LLMCall.provider,
                    LLMCall.model,
                    LLMCall.purpose,
                    func.sum(LLMCall.prompt_tokens),
                    func.sum(LLMCall.completion_tokens),
                    func.sum(LLMCall.cost_usd),
                    func.count(LLMCall.id),
                )
                .where(LLMCall.started_at >= start, LLMCall.started_at < end)
                .group_by(LLMCall.provider, LLMCall.model, LLMCall.purpose)
                .order_by(func.sum(LLMCall.cost_usd).desc())
            )
            return list((await session.execute(stmt)).all())

    rows = asyncio.run(_run())
    table = Table(title=f"LLM Cost Summary — {month}")
    table.add_column("Provider")
    table.add_column("Model")
    table.add_column("Purpose")
    table.add_column("Prompt Tokens", justify="right")
    table.add_column("Completion Tokens", justify="right")
    table.add_column("Cost (USD)", justify="right")
    table.add_column("Calls", justify="right")
    total = Decimal(0)
    for provider, model, purpose, pt, ct, cost, n in rows:
        table.add_row(
            provider, model, purpose,
            f"{pt:,}", f"{ct:,}", f"${cost:.4f}", str(n),
        )
        total += cost
    _console.print(table)
    _console.print(f"[bold]Total: ${total:.4f}[/bold]")


@app.command("db-migrate")
def db_migrate() -> None:
    """Run alembic upgrade head."""
    import subprocess
    subprocess.run(["alembic", "upgrade", "head"], check=True)
```

- [ ] **Step 2: 注册到 `cli/main.py`**

修改 `cli/main.py`：

```python
"""Typer entry point: `ia` command."""

import typer

from investment_agent.cli.commands import admin as admin_cmd
from investment_agent.cli.commands import data as data_cmd

app = typer.Typer(help="Investment Agent CLI")
app.add_typer(data_cmd.app, name="data", help="Market data ingestion commands")
app.add_typer(admin_cmd.app, name="admin", help="Admin and ops commands")


@app.callback()
def _bootstrap() -> None:
    from investment_agent.config import Settings
    from investment_agent.logging import configure_logging
    from investment_agent.storage.database import init_engine

    settings = Settings()
    configure_logging(settings.log_level)
    init_engine(settings.database_url)


if __name__ == "__main__":
    app()
```

- [ ] **Step 3: 写 CLI 测试 `tests/unit/test_cli_admin.py`**

```python
from typer.testing import CliRunner


def test_admin_cost_help():
    from investment_agent.cli.main import app

    runner = CliRunner()
    result = runner.invoke(app, ["admin", "cost", "--help"])
    assert result.exit_code == 0
    assert "month" in result.output.lower()
```

- [ ] **Step 4: 运行测试**

Run: `pytest tests/unit/test_cli_admin.py -v`
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add src/investment_agent/cli/commands/admin.py src/investment_agent/cli/main.py tests/unit/test_cli_admin.py
git commit -m "feat: admin cost summary CLI command"
```

---

## Task 28: 集成 smoke test — 跨层端到端

**Files:**
- Create: `tests/integration/test_data_pipeline_smoke.py`

- [ ] **Step 1: 写 smoke test**

```python
"""End-to-end smoke: seed universe + insert daily price + repository query."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy import select

from investment_agent.sources.records import DailyPriceRecord
from investment_agent.storage.models.market_data import DailyPrice
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.repositories.listing import ListingRepository


@pytest.mark.asyncio
async def test_full_data_path_seed_and_query(db_session, seeded_market):
    """Verify the seed -> insert daily price -> query path works end-to-end."""
    repo = ListingRepository(db_session)
    listing = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )

    record = DailyPriceRecord(
        ticker="AAPL", date=date(2025, 6, 3),
        open=Decimal("196.50"), high=Decimal("198.20"),
        low=Decimal("195.80"), close=Decimal("197.40"),
        volume=50_000_000, adjusted_close=Decimal("197.40"),
    )
    db_session.add(
        DailyPrice(
            listing_id=listing.id, date=record.date,
            open=record.open, high=record.high, low=record.low,
            close=record.close, volume=record.volume,
            adjusted_close=record.adjusted_close,
        )
    )
    await db_session.flush()

    stmt = select(DailyPrice).where(DailyPrice.listing_id == listing.id)
    rows = (await db_session.execute(stmt)).scalars().all()
    assert len(rows) == 1
    assert rows[0].close == Decimal("197.40")
```

- [ ] **Step 2: 运行**

Run: `pytest tests/integration/test_data_pipeline_smoke.py -v`
Expected: 1 passed

- [ ] **Step 3: 全量测试套件确认**

Run: `pytest -v`
Expected: 全部 unit + integration 测试通过（约 25 个）。

- [ ] **Step 4: 类型与 lint 检查**

Run: `ruff check . && mypy src/investment_agent`

Expected: 全部通过。如果 mypy 报某些第三方包类型错误，已在 pyproject.toml 中 `ignore_missing_imports`。

- [ ] **Step 5: Commit**

```bash
git add tests/integration/test_data_pipeline_smoke.py
git commit -m "test: end-to-end data path smoke test"
```

---

# Plan A 完成总结

完成 Plan A 后，系统具备：

✅ Docker 化的 Python 项目骨架（Mac 开发 + Linux 部署一致）  
✅ 完整数据库 Schema（22 张表）+ alembic 迁移 + 4 个迁移文件  
✅ `DataSource` Protocol + `YFinanceSource` 完整实现（含跨市场 ticker 支持）  
✅ `LLMClient` Protocol + `OpenAICompatibleClient`（任意 OpenAI-compatible provider）  
✅ Pydantic-AI Agent 工厂（为 Plan B/C 准备）  
✅ LLMCall 审计 + 成本计算  
✅ CLI：`ia data seed/backfill/refresh`, `ia admin cost/db-migrate`  
✅ 25+ 单测和集成测试覆盖

**手动验收（运行后即可确认）**：

```bash
# 1. 启动容器
docker compose up -d

# 2. 应用迁移
docker compose exec app alembic upgrade head

# 3. 初始化 universe
docker compose exec app ia data seed

# 4. 拉取真实数据（需要联网）
docker compose exec app ia data backfill --tickers AAPL,MSFT,NVDA --start 2025-05-01

# 5. 查询数据库
docker compose exec app sqlite3 /app/data/app.db \
  "SELECT ticker, date, close FROM daily_price JOIN listing USING(listing_id) ORDER BY date DESC LIMIT 10"

# 6. 验证 LLM 配置 (需配置真实 LLM_TIER2_API_KEY)
docker compose exec app python -c "
import asyncio
from investment_agent.config import Settings
from investment_agent.llm.openai_compatible import OpenAICompatibleClient
from investment_agent.storage.database import init_engine, session_scope

async def main():
    s = Settings()
    init_engine(s.database_url)
    async with session_scope() as sess:
        c = OpenAICompatibleClient(tier_name='tier2', config=s.llm_tier2, session=sess)
        r = await c.complete(system_prompt='hi', user_message='say one word', purpose='smoke')
        print(r.text)
asyncio.run(main())
"

# 7. 月度成本
docker compose exec app ia admin cost
```

如果以上 7 步都通过，Plan A 完整交付。可以进入 Plan B（策略 + Discovery Pipeline + Renderer + Notifier）。
