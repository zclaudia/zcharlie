# Investment Agent · Plan B: Strategy Framework + Discovery Pipeline + Renderer/Notifier

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task.

**Goal:** 把 Plan A 搭好的基础设施变成可工作的每日 Discovery 系统：定义策略框架并落地 3 个具体策略（value / growth / risk_aggressive），实现完整 7 阶段 Discovery Pipeline（含 Auxiliary Tracking 数据拉取与派生），Meta-Agent 合成共识/分歧/被忽略清单，Markdown Renderer 与 File Notifier 完成日报产出。

**Architecture:** 同 Plan A — Python 3.13 + asyncio 单进程 + SQLAlchemy async + Pydantic-AI。新增模块按 Plan A 已定义的 Protocol（StrategyAgent / Renderer / Notifier）实现，业务逻辑严格单向依赖。

**Tech Stack（新增依赖）:** jinja2 (prompt 模板渲染) + pyyaml (策略配置) + pydantic-ai 1.x（已装）+ structlog（已装）。

**Plan Scope:** Phase 3 (15-20h) + Phase 4 (11-16h) + Phase 6 (5-8h) = **31-44 小时**，**22 个任务**

**Prerequisites:**
- Plan A 完成（commit `334cbab` 是最后一个 Plan A commit）
- `.env` 已配置真实的 `LLM_TIER2_*` / `LLM_TIER3_*`（实施过程中 Task 14/16/17 需要真实 LLM 调用通过 smoke test；其余 Task 用 mock）
- `ia data seed` 已跑过，data/app.db 有 NASDAQ_100 universe + 10 个起步成份股

**Spec：** `docs/superpowers/specs/2026-06-07-investment-agent-design.md` (§3, §4)

**后续 Plans：**
- Plan C: Attribution Pipeline (Phase 5)
- Plan D: FastAPI + APScheduler + Docker 加固 + 测试覆盖 (Phase 7+8+9)

---

## 文件结构总览（Plan B 结束时新增/修改）

```
investment-agent (= 仓库根)
├── strategies/                            ← 新增：策略 YAML 真相源
│   ├── value.yaml
│   ├── growth.yaml
│   └── risk_aggressive.yaml
├── prompts/                               ← 新增：LLM 提示词
│   └── strategies/
│       ├── value/
│       │   ├── scoring.j2
│       │   └── deep.j2
│       ├── growth/
│       │   ├── scoring.j2
│       │   └── deep.j2
│       ├── risk_aggressive/
│       │   ├── scoring.j2
│       │   └── deep.j2
│       └── relationship_extraction/
│           └── extract.j2
├── src/investment_agent/
│   ├── domain/
│   │   ├── protocols.py                   ← 修改：加 StrategyAgent + Renderer + Notifier Protocol
│   │   └── strategy_types.py              ← 新增：ScreeningRule / ScoringContext / DeepAnalysisContext / ListingSnapshot / StockScore / DeepAnalysis 等 DTO
│   ├── strategies/                        ← 新增
│   │   ├── __init__.py
│   │   ├── base.py                        ← BaseStrategyAgent
│   │   ├── screening_rule.py              ← Condition DSL evaluator
│   │   ├── loader.py                      ← strategies/*.yaml → DB sync
│   │   ├── prompt_loader.py               ← Jinja2 wrapper
│   │   ├── schemas.py                     ← ScoringSchema / DeepSchema (per-strategy)
│   │   └── registry.py                    ← 全局策略注册表
│   ├── pipelines/                         ← 新增
│   │   ├── __init__.py
│   │   ├── discovery.py                   ← DiscoveryPipeline orchestrator
│   │   └── steps.py                       ← 7 个 Step 函数
│   ├── relationships/                     ← 新增
│   │   ├── __init__.py
│   │   ├── extractor.py                   ← LLM 提取 ExtractedRelationship
│   │   └── resolver.py                    ← fuzzy match counterparty → company_id
│   ├── auxiliary/                         ← 新增
│   │   ├── __init__.py
│   │   └── derivation.py                  ← 派生 Job：扫 company_relationship → 更新 auxiliary_listing
│   ├── meta/                              ← 新增
│   │   ├── __init__.py
│   │   └── synthesizer.py                 ← Meta-Agent 算法合成
│   ├── renderers/                         ← 新增
│   │   ├── __init__.py
│   │   ├── base.py                        ← RenderedContent + Renderer 工厂
│   │   └── markdown_renderer.py
│   ├── notifiers/                         ← 新增
│   │   ├── __init__.py
│   │   ├── base.py                        ← NotificationResult
│   │   └── file_notifier.py
│   ├── storage/repositories/              ← 新增 repository
│   │   ├── strategy.py
│   │   ├── pipeline_run.py
│   │   ├── stock_score.py
│   │   ├── auxiliary.py
│   │   ├── company.py                     ← 公司 + Sector + Universe 查询
│   │   ├── fundamental.py                 ← FundamentalSnapshot + FinancialReport
│   │   └── price.py                       ← DailyPrice
│   └── cli/commands/
│       ├── pipeline.py                    ← 新增：ia pipeline discover/list/show
│       ├── strategy.py                    ← 新增：ia strategy list/show/sync
│       └── auxiliary.py                   ← 新增：ia auxiliary list/add/remove/refresh
└── tests/
    ├── unit/
    │   ├── test_screening_rule.py
    │   ├── test_strategy_loader.py
    │   ├── test_meta_synthesizer.py
    │   ├── test_markdown_renderer.py
    │   ├── test_file_notifier.py
    │   ├── test_relationship_extractor.py
    │   ├── test_auxiliary_derivation.py
    │   └── test_repositories_v2.py
    ├── integration/
    │   ├── test_discovery_pipeline.py     ← 完整 e2e (含 fake LLM + fake yfinance)
    │   └── test_strategy_registry.py
    └── fixtures/
        ├── fake_universe_snapshot.json
        ├── fake_llm_scoring.json
        └── fake_llm_deep_analysis.json
```

---

# Phase 3: 策略框架 + 3 个策略

## Task 1: 新增 Protocol（StrategyAgent / Renderer / Notifier）+ 共享 DTO

**Files:**
- Modify: `src/investment_agent/domain/protocols.py`
- Create: `src/investment_agent/domain/strategy_types.py`

- [ ] **Step 1: 实现 `domain/strategy_types.py`**

```python
"""Strategy-layer DTOs: ListingSnapshot, StockScore, DeepAnalysis, ScoringContext, DeepAnalysisContext."""

from datetime import date
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _DTOBase(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)


class ListingSnapshot(_DTOBase):
    """One stock's current state — used as Tier 1 screening input."""

    listing_id: int
    company_id: int
    ticker: str
    market_code: str
    company_name: str
    sector_code: str | None = None

    # FundamentalSnapshot fields (latest as of as_of date)
    as_of: date
    pe: Decimal | None = None
    pb: Decimal | None = None
    peg: Decimal | None = None
    ps: Decimal | None = None
    roe: Decimal | None = None
    roa: Decimal | None = None
    fcf_yield: Decimal | None = None
    debt_to_equity: Decimal | None = None
    market_cap: Decimal | None = None

    # Latest financial report extracts (used by some ScreeningRule conditions)
    revenue_ttm: Decimal | None = None
    net_income_ttm: Decimal | None = None
    revenue_growth_yoy: Decimal | None = None
    gross_margin: Decimal | None = None
    beta: Decimal | None = None
    volatility_30d: Decimal | None = None


class ScoringContext(_DTOBase):
    """Tier 2 LLM scoring input — listing + recent peers + sector context."""

    listing: ListingSnapshot
    peer_summary: list[dict] = Field(default_factory=list)  # 同行业前 5
    recent_news_titles: list[str] = Field(default_factory=list)
    recent_rating_changes: list[dict] = Field(default_factory=list)


class DeepAnalysisContext(_DTOBase):
    """Tier 3 deep analysis input — much richer."""

    listing: ListingSnapshot
    financials_history: list[dict] = Field(default_factory=list)  # 最近 5 年财报
    peer_comparables: list[dict] = Field(default_factory=list)
    known_relationships: list[dict] = Field(default_factory=list)  # CompanyRelationship 摘要
    auxiliary_context: list[dict] = Field(default_factory=list)  # 关联公司近期数据
    recent_news_full: list[dict] = Field(default_factory=list)


class StockScore(_DTOBase):
    """Tier 2 output — structured score."""

    score: int = Field(ge=0, le=100)
    confidence: float = Field(ge=0.0, le=1.0)
    bull_thesis: str
    bear_thesis: str
    key_metrics_explained: dict[str, str] = Field(default_factory=dict)


class DeepAnalysis(_DTOBase):
    """Tier 3 output — full deep dive."""

    business_model: str
    moat_assessment: str
    risk_factors: list[str]
    upstream_dependencies: list[str]
    downstream_customers: list[str]
    competitor_overview: list[str]
    valuation_summary: str
    extracted_relationships: list["ExtractedRelationship"] = Field(default_factory=list)


class ExtractedRelationship(_DTOBase):
    """LLM-extracted company relationship — written to company_relationship table."""

    counterparty_name: str
    counterparty_ticker: str | None = None
    relationship_type: str  # 'supplier_of' | 'competes_with' | 'subsidiary_of' | 'partner_of'
    strength: float = Field(ge=0.0, le=1.0)
    revenue_share_pct: float | None = None
    description: str
    evidence: str


# Re-export for typing
DeepAnalysis.model_rebuild()
```

- [ ] **Step 2: 扩展 `domain/protocols.py` — 追加 StrategyAgent / Renderer / Notifier Protocol**

在文件末尾追加：

```python
# === Strategy / Renderer / Notifier (Plan B) ===

from datetime import date as _date
from investment_agent.domain.strategy_types import (
    DeepAnalysis,
    DeepAnalysisContext,
    ListingSnapshot,
    ScoringContext,
    StockScore,
)


@runtime_checkable
class StrategyAgent(Protocol):
    code: str
    version: str

    async def screen(
        self, candidates: list[ListingSnapshot]
    ) -> list[ListingSnapshot]: ...

    async def score(
        self, listing: ListingSnapshot, context: ScoringContext
    ) -> StockScore: ...

    async def deep_analyze(
        self, listing: ListingSnapshot, context: DeepAnalysisContext
    ) -> DeepAnalysis: ...


class RenderedContent(BaseModel):
    format: str  # 'markdown' | 'html' | 'json'
    body: str
    metadata: dict[str, object] = Field(default_factory=dict)


@runtime_checkable
class Renderer(Protocol):
    async def render_discovery(
        self,
        run_id: int,
        consensus_picks: list[dict],
        divergent_picks: list[dict],
        overlooked_picks: list[dict],
        scores_by_listing: dict[int, list[dict]],
    ) -> RenderedContent: ...

    async def render_attribution(
        self, run_id: int, payload: dict
    ) -> RenderedContent: ...


class NotificationResult(BaseModel):
    notifier_name: str
    success: bool
    target: str = ""  # e.g. file path / chat id
    error: str | None = None


@runtime_checkable
class Notifier(Protocol):
    name: str

    async def send(
        self, content: RenderedContent, channel_metadata: dict[str, object]
    ) -> NotificationResult: ...
```

(注意：顶部已有 `from pydantic import BaseModel`，无需再 import；`Field` 需要新加 `from pydantic import Field`，确保在文件顶部 import 区添加。)

- [ ] **Step 3: 自检 import 链**

```bash
source .venv/bin/activate && python -c "
from investment_agent.domain.protocols import StrategyAgent, Renderer, Notifier, RenderedContent, NotificationResult
from investment_agent.domain.strategy_types import ListingSnapshot, StockScore, DeepAnalysis, ExtractedRelationship
print('ok')
"
```
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add src/investment_agent/domain/
git commit -m "feat: StrategyAgent + Renderer + Notifier Protocols + strategy DTOs"
```

---

## Task 2: ScreeningRule DSL + 求值器

**Files:**
- Create: `src/investment_agent/strategies/__init__.py`
- Create: `src/investment_agent/strategies/screening_rule.py`
- Create: `tests/unit/test_screening_rule.py`

- [ ] **Step 1: 写测试 `tests/unit/test_screening_rule.py`**

```python
from decimal import Decimal

import pytest

from investment_agent.domain.strategy_types import ListingSnapshot
from investment_agent.strategies.screening_rule import (
    Condition,
    ScreeningRule,
    evaluate_rule,
)


def _snap(**kw) -> ListingSnapshot:
    base = dict(
        listing_id=1, company_id=1, ticker="AAPL", market_code="NASDAQ",
        company_name="Apple", as_of="2025-06-03",
    )
    base.update(kw)
    return ListingSnapshot.model_validate(base)


def test_required_lt_passes():
    rule = ScreeningRule(required=[Condition(metric="pe", op="<", value=30)])
    assert evaluate_rule(rule, _snap(pe=Decimal("25"))).passed is True


def test_required_lt_fails():
    rule = ScreeningRule(required=[Condition(metric="pe", op="<", value=30)])
    result = evaluate_rule(rule, _snap(pe=Decimal("35")))
    assert result.passed is False
    assert "pe" in result.failed_conditions[0]


def test_between():
    rule = ScreeningRule(required=[Condition(metric="pe", op="between", value=(0, 25))])
    assert evaluate_rule(rule, _snap(pe=Decimal("18"))).passed is True
    assert evaluate_rule(rule, _snap(pe=Decimal("26"))).passed is False


def test_excluded_blocks():
    rule = ScreeningRule(
        required=[],
        excluded=[Condition(metric="net_income_ttm", op="<", value=0)],
    )
    assert evaluate_rule(rule, _snap(net_income_ttm=Decimal("-1000000"))).passed is False
    assert evaluate_rule(rule, _snap(net_income_ttm=Decimal("1000000"))).passed is True


def test_missing_value_treated_as_failure_for_required():
    """如果某指标是 None，required 视为失败。"""
    rule = ScreeningRule(required=[Condition(metric="pe", op="<", value=30)])
    assert evaluate_rule(rule, _snap(pe=None)).passed is False


def test_preferred_does_not_block():
    """preferred 不通过不影响 passed，但记录 preferred_misses。"""
    rule = ScreeningRule(
        required=[],
        preferred=[Condition(metric="fcf_yield", op=">", value=0.05)],
    )
    result = evaluate_rule(rule, _snap(fcf_yield=Decimal("0.02")))
    assert result.passed is True
    assert len(result.preferred_misses) == 1
    assert result.preferred_score == 0.0


def test_preferred_score_calculation():
    rule = ScreeningRule(
        required=[],
        preferred=[
            Condition(metric="fcf_yield", op=">", value=0.05),
            Condition(metric="roe", op=">", value=0.15),
        ],
    )
    result = evaluate_rule(
        rule,
        _snap(fcf_yield=Decimal("0.08"), roe=Decimal("0.20")),
    )
    assert result.preferred_score == 1.0  # 全满足


def test_unknown_op_raises():
    with pytest.raises(ValueError, match="Unknown op"):
        rule = ScreeningRule(required=[Condition(metric="pe", op="approx", value=10)])
        evaluate_rule(rule, _snap(pe=Decimal("10")))
```

- [ ] **Step 2: 运行测试确认失败**

`pytest tests/unit/test_screening_rule.py -v` → ImportError

- [ ] **Step 3: 实现 `strategies/screening_rule.py`**

```python
"""ScreeningRule DSL: declarative Tier 1 algorithmic filtering."""

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from investment_agent.domain.strategy_types import ListingSnapshot

Op = Literal["<", "<=", ">", ">=", "==", "between"]


class Condition(BaseModel):
    metric: str  # snake_case attribute name on ListingSnapshot
    op: Op
    value: float | tuple[float, float]


class ScreeningRule(BaseModel):
    required: list[Condition] = Field(default_factory=list)
    excluded: list[Condition] = Field(default_factory=list)
    preferred: list[Condition] = Field(default_factory=list)


class ScreeningResult(BaseModel):
    passed: bool
    failed_conditions: list[str] = Field(default_factory=list)
    preferred_misses: list[str] = Field(default_factory=list)
    preferred_score: float = 0.0  # 0..1


def _get_metric(snap: ListingSnapshot, metric: str) -> Decimal | None:
    return getattr(snap, metric, None)


def _eval_condition(value: Decimal | None, op: Op, target) -> bool:
    """Returns True iff condition passes. Missing value → False."""
    if value is None:
        return False
    v = float(value)
    if op == "<":
        return v < float(target)
    if op == "<=":
        return v <= float(target)
    if op == ">":
        return v > float(target)
    if op == ">=":
        return v >= float(target)
    if op == "==":
        return v == float(target)
    if op == "between":
        lo, hi = target
        return float(lo) <= v <= float(hi)
    raise ValueError(f"Unknown op: {op}")


def evaluate_rule(rule: ScreeningRule, snap: ListingSnapshot) -> ScreeningResult:
    failed: list[str] = []
    misses: list[str] = []

    for cond in rule.required:
        v = _get_metric(snap, cond.metric)
        if not _eval_condition(v, cond.op, cond.value):
            failed.append(f"{cond.metric} {cond.op} {cond.value} (actual: {v})")

    for cond in rule.excluded:
        v = _get_metric(snap, cond.metric)
        if _eval_condition(v, cond.op, cond.value):
            failed.append(
                f"excluded:{cond.metric} {cond.op} {cond.value} (actual: {v})"
            )

    pref_passes = 0
    for cond in rule.preferred:
        v = _get_metric(snap, cond.metric)
        if _eval_condition(v, cond.op, cond.value):
            pref_passes += 1
        else:
            misses.append(f"{cond.metric} {cond.op} {cond.value}")

    pref_score = (
        pref_passes / len(rule.preferred) if rule.preferred else 0.0
    )

    return ScreeningResult(
        passed=len(failed) == 0,
        failed_conditions=failed,
        preferred_misses=misses,
        preferred_score=pref_score,
    )
```

- [ ] **Step 4: 测试通过**

`pytest tests/unit/test_screening_rule.py -v` → 8 passed

- [ ] **Step 5: Commit**

```bash
git add src/investment_agent/strategies/ tests/unit/test_screening_rule.py
git commit -m "feat: ScreeningRule DSL + evaluator (Tier 1 algorithmic filter)"
```

---

## Task 3: Prompt Loader（Jinja2 模板包装）

**Files:**
- Create: `src/investment_agent/strategies/prompt_loader.py`
- Create: `tests/unit/test_prompt_loader.py`

新增依赖：`jinja2`。在 `pyproject.toml` `dependencies` 列表追加 `"jinja2>=3.1"`，然后 `pip install -e ".[dev]"`。

- [ ] **Step 1: 添加依赖**

修改 `pyproject.toml`，在 dependencies 中加 `"jinja2>=3.1"`。运行 `pip install -e ".[dev]"`。

- [ ] **Step 2: 写测试 `tests/unit/test_prompt_loader.py`**

```python
from pathlib import Path

import pytest

from investment_agent.strategies.prompt_loader import PromptLoader


def test_render_with_variables(tmp_path):
    tmpl_dir = tmp_path / "prompts"
    tmpl_dir.mkdir()
    (tmpl_dir / "scoring.j2").write_text(
        "Analyze {{ ticker }} with PE={{ pe }}."
    )
    loader = PromptLoader(tmpl_dir)
    out = loader.render("scoring", ticker="AAPL", pe=28.5)
    assert out == "Analyze AAPL with PE=28.5."


def test_render_missing_template_raises(tmp_path):
    loader = PromptLoader(tmp_path)
    with pytest.raises(FileNotFoundError, match="ghost"):
        loader.render("ghost", ticker="X")


def test_render_supports_dicts_and_lists(tmp_path):
    tmpl_dir = tmp_path / "p"
    tmpl_dir.mkdir()
    (tmpl_dir / "deep.j2").write_text(
        "Peers:\n{% for p in peers %}- {{ p.ticker }}: PE={{ p.pe }}\n{% endfor %}"
    )
    loader = PromptLoader(tmpl_dir)
    out = loader.render(
        "deep",
        peers=[{"ticker": "MSFT", "pe": 32}, {"ticker": "GOOG", "pe": 25}],
    )
    assert "MSFT: PE=32" in out
    assert "GOOG: PE=25" in out
```

- [ ] **Step 3: 实现 `prompt_loader.py`**

```python
"""Jinja2-based prompt template loader."""

from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, StrictUndefined


class PromptLoader:
    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)
        self._env = Environment(
            loader=FileSystemLoader(str(self._base_dir)),
            undefined=StrictUndefined,  # 缺少变量直接抛错，避免静默 bug
            autoescape=False,
            keep_trailing_newline=True,
        )

    def render(self, template_name: str, **vars: Any) -> str:
        path = self._base_dir / f"{template_name}.j2"
        if not path.exists():
            raise FileNotFoundError(
                f"Prompt template not found: {template_name}.j2 in {self._base_dir}"
            )
        return self._env.get_template(f"{template_name}.j2").render(**vars)
```

- [ ] **Step 4: 测试通过 & commit**

```bash
pytest tests/unit/test_prompt_loader.py -v  # 3 passed
git add pyproject.toml src/investment_agent/strategies/prompt_loader.py tests/unit/test_prompt_loader.py
git commit -m "feat: Jinja2-based PromptLoader for strategy templates"
```

---

## Task 4: BaseStrategyAgent + Schemas Registry

**Files:**
- Create: `src/investment_agent/strategies/schemas.py`
- Create: `src/investment_agent/strategies/base.py`
- Create: `tests/unit/test_base_strategy_agent.py`

- [ ] **Step 1: 实现 `strategies/schemas.py`**

```python
"""Per-strategy output schemas. Subclass StockScore / DeepAnalysis with extra fields if needed."""

from investment_agent.domain.strategy_types import DeepAnalysis, StockScore


class ValueStockScore(StockScore):
    intrinsic_value_estimate: float | None = None
    margin_of_safety_pct: float | None = None


class ValueDeepAnalysis(DeepAnalysis):
    dcf_summary: str | None = None
    owner_earnings_yield: float | None = None


class GrowthStockScore(StockScore):
    tam_estimate: str | None = None
    revenue_growth_3y_cagr: float | None = None


class GrowthDeepAnalysis(DeepAnalysis):
    growth_drivers: list[str] = []
    expansion_runway: str | None = None


class RiskAggressiveStockScore(StockScore):
    catalyst_summary: str
    short_interest_pct: float | None = None
    iv_rank: float | None = None


class RiskAggressiveDeepAnalysis(DeepAnalysis):
    technical_setup: str | None = None
    sentiment_score: float | None = None


# Registry: strategy_code -> (scoring_schema, deep_schema)
SCHEMAS_BY_CODE: dict[str, tuple[type, type]] = {
    "value": (ValueStockScore, ValueDeepAnalysis),
    "growth": (GrowthStockScore, GrowthDeepAnalysis),
    "risk_aggressive": (RiskAggressiveStockScore, RiskAggressiveDeepAnalysis),
}
```

- [ ] **Step 2: 实现 `strategies/base.py`**

```python
"""BaseStrategyAgent: concrete strategy = config-driven subclass."""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pydantic_ai import Agent

from investment_agent.domain.strategy_types import (
    DeepAnalysis,
    DeepAnalysisContext,
    ListingSnapshot,
    ScoringContext,
    StockScore,
)
from investment_agent.llm.agent_factory import build_agent
from investment_agent.strategies.prompt_loader import PromptLoader
from investment_agent.strategies.screening_rule import (
    ScreeningResult,
    ScreeningRule,
    evaluate_rule,
)


@dataclass
class StrategyConfig:
    code: str
    version: str
    name: str
    description: str
    screening_rule: ScreeningRule
    scoring_prompt_template: str   # e.g. 'strategies/value/scoring'
    deep_prompt_template: str
    scoring_schema: type[StockScore]
    deep_schema: type[DeepAnalysis]


class BaseStrategyAgent:
    """All concrete strategies inherit this. Subclasses just set `config`."""

    config: StrategyConfig

    def __init__(self, prompts_dir: Path, tier2_agent: Agent, tier3_agent: Agent) -> None:
        self._prompts = PromptLoader(prompts_dir)
        self._tier2_agent = tier2_agent
        self._tier3_agent = tier3_agent

    @property
    def code(self) -> str:
        return self.config.code

    @property
    def version(self) -> str:
        return self.config.version

    async def screen(
        self, candidates: list[ListingSnapshot]
    ) -> list[ListingSnapshot]:
        """Tier 1: pure algorithmic. Returns candidates that pass `required` + not in `excluded`."""
        out: list[ListingSnapshot] = []
        for snap in candidates:
            result: ScreeningResult = evaluate_rule(self.config.screening_rule, snap)
            if result.passed:
                out.append(snap)
        return out

    async def score(
        self, listing: ListingSnapshot, context: ScoringContext
    ) -> StockScore:
        """Tier 2: LLM-driven structured scoring."""
        prompt = self._prompts.render(
            self.config.scoring_prompt_template,
            listing=listing.model_dump(),
            context=context.model_dump(),
        )
        result = await self._tier2_agent.run(prompt)
        score = result.output
        # output_type was set to subclass via build_agent; mypy can't see that
        assert isinstance(score, self.config.scoring_schema)
        return score

    async def deep_analyze(
        self, listing: ListingSnapshot, context: DeepAnalysisContext
    ) -> DeepAnalysis:
        prompt = self._prompts.render(
            self.config.deep_prompt_template,
            listing=listing.model_dump(),
            context=context.model_dump(),
        )
        result = await self._tier3_agent.run(prompt)
        analysis = result.output
        assert isinstance(analysis, self.config.deep_schema)
        return analysis
```

- [ ] **Step 3: 写测试 `tests/unit/test_base_strategy_agent.py`**（仅测 `screen`，LLM 部分 mock 在 Task 6 测试中覆盖）

```python
from decimal import Decimal

import pytest

from investment_agent.domain.strategy_types import ListingSnapshot
from investment_agent.strategies.base import BaseStrategyAgent, StrategyConfig
from investment_agent.strategies.schemas import ValueDeepAnalysis, ValueStockScore
from investment_agent.strategies.screening_rule import Condition, ScreeningRule


class _StubAgent:
    """Stub for Pydantic-AI Agent (used here only because BaseStrategyAgent's __init__ requires them)."""


def _make_agent(rule: ScreeningRule) -> BaseStrategyAgent:
    cfg = StrategyConfig(
        code="test", version="v1.0", name="t", description="t",
        screening_rule=rule,
        scoring_prompt_template="x/scoring",
        deep_prompt_template="x/deep",
        scoring_schema=ValueStockScore,
        deep_schema=ValueDeepAnalysis,
    )

    class _A(BaseStrategyAgent):
        pass
    _A.config = cfg
    return _A(prompts_dir=__import__("pathlib").Path("."), tier2_agent=_StubAgent(), tier3_agent=_StubAgent())


def _snap(**kw):
    base = dict(listing_id=1, company_id=1, ticker="AAPL", market_code="NASDAQ",
                company_name="Apple", as_of="2025-06-03")
    base.update(kw)
    return ListingSnapshot.model_validate(base)


@pytest.mark.asyncio
async def test_screen_passes_when_required_met():
    rule = ScreeningRule(required=[Condition(metric="pe", op="<", value=30)])
    agent = _make_agent(rule)
    candidates = [_snap(pe=Decimal("25")), _snap(pe=Decimal("35"))]
    out = await agent.screen(candidates)
    assert len(out) == 1
    assert out[0].pe == Decimal("25")


@pytest.mark.asyncio
async def test_screen_returns_empty_when_none_pass():
    rule = ScreeningRule(required=[Condition(metric="pe", op="<", value=5)])
    agent = _make_agent(rule)
    out = await agent.screen([_snap(pe=Decimal("25"))])
    assert out == []
```

- [ ] **Step 4: 测试 + commit**

```bash
pytest tests/unit/test_base_strategy_agent.py -v  # 2 passed
git add src/investment_agent/strategies/{schemas,base}.py tests/unit/test_base_strategy_agent.py
git commit -m "feat: BaseStrategyAgent (declarative config + Tier 1 screen + Tier 2/3 LLM)"
```

---

## Task 5: Strategy Loader（YAML → DB + concrete instances）

**Files:**
- Create: `src/investment_agent/storage/repositories/strategy.py`
- Create: `src/investment_agent/strategies/loader.py`
- Create: `strategies/value.yaml`, `growth.yaml`, `risk_aggressive.yaml`
- Create: `tests/unit/test_strategy_loader.py`

- [ ] **Step 1: 实现 `repositories/strategy.py`**

```python
"""Strategy repository — DB mirror of strategies/*.yaml."""

from sqlalchemy import select

from investment_agent.storage.models.analysis import Strategy
from investment_agent.storage.repositories.base import Repository


class StrategyRepository(Repository[Strategy]):
    model = Strategy

    async def list_all(self) -> list[Strategy]:
        stmt = select(Strategy).order_by(Strategy.code)
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_enabled(self) -> list[Strategy]:
        stmt = select(Strategy).where(Strategy.enabled.is_(True)).order_by(Strategy.code)
        return list((await self.session.execute(stmt)).scalars().all())

    async def upsert(
        self,
        *,
        code: str,
        name: str,
        version: str,
        description: str | None,
        config_json: dict,
        enabled: bool = True,
    ) -> Strategy:
        existing = await self.session.get(Strategy, code)
        if existing is None:
            existing = Strategy(
                code=code, name=name, version=version,
                description=description, config_json=config_json, enabled=enabled,
            )
            self.session.add(existing)
        else:
            existing.name = name
            existing.version = version
            existing.description = description
            existing.config_json = config_json
            existing.enabled = enabled
        await self.session.flush()
        return existing
```

- [ ] **Step 2: 创建 3 个 strategy YAML 文件**

`strategies/value.yaml`:

```yaml
code: value
version: v1.0
name: Value Investor
description: |
  Classic Graham/Buffett value investing: low PE/PB, healthy ROE, positive FCF,
  manageable leverage. Targets undervalued large/mid-caps with proven cash generation.
screening_rule:
  required:
    - { metric: pe, op: between, value: [0, 25] }
    - { metric: pb, op: "<", value: 4.0 }
    - { metric: roe, op: ">", value: 0.10 }
    - { metric: debt_to_equity, op: "<", value: 2.0 }
  excluded:
    - { metric: net_income_ttm, op: "<", value: 0 }
  preferred:
    - { metric: fcf_yield, op: ">", value: 0.05 }
scoring_prompt: strategies/value/scoring
deep_prompt: strategies/value/deep
```

`strategies/growth.yaml`:

```yaml
code: growth
version: v1.0
name: Growth Investor
description: |
  Aggressive growth: high revenue/earnings growth, expanding gross margins, large TAM.
  PE tolerance up to 60 because the future cash flows are heavily back-loaded.
screening_rule:
  required:
    - { metric: revenue_growth_yoy, op: ">", value: 0.25 }
    - { metric: gross_margin, op: ">", value: 0.40 }
    - { metric: pe, op: between, value: [0, 60] }
  excluded:
    - { metric: net_income_ttm, op: "<", value: -500000000 }  # 容许 5 亿以内的亏损
  preferred:
    - { metric: roe, op: ">", value: 0.20 }
scoring_prompt: strategies/growth/scoring
deep_prompt: strategies/growth/deep
```

`strategies/risk_aggressive.yaml`:

```yaml
code: risk_aggressive
version: v1.0
name: Risk-Aggressive / Catalyst-Driven
description: |
  Small/mid-cap with high beta, catalyst-driven, sentiment-sensitive. Looks for
  asymmetric upside via short squeezes, earnings surprises, product launches.
screening_rule:
  required:
    - { metric: market_cap, op: "<", value: 50000000000 }
    - { metric: beta, op: ">", value: 1.3 }
  excluded: []
  preferred:
    - { metric: volatility_30d, op: ">", value: 0.04 }
scoring_prompt: strategies/risk_aggressive/scoring
deep_prompt: strategies/risk_aggressive/deep
```

- [ ] **Step 3: 实现 `strategies/loader.py`**

```python
"""Load strategies/*.yaml → sync to DB → build concrete StrategyAgent instances."""

from pathlib import Path

import yaml

from investment_agent.domain.strategy_types import DeepAnalysis, StockScore
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.strategy import StrategyRepository
from investment_agent.strategies.base import BaseStrategyAgent, StrategyConfig
from investment_agent.strategies.schemas import SCHEMAS_BY_CODE
from investment_agent.strategies.screening_rule import (
    Condition,
    ScreeningRule,
)


def _condition_from_yaml(raw: dict) -> Condition:
    val = raw["value"]
    if isinstance(val, list):
        val = tuple(val)
    return Condition(metric=raw["metric"], op=raw["op"], value=val)


def _rule_from_yaml(raw: dict) -> ScreeningRule:
    return ScreeningRule(
        required=[_condition_from_yaml(c) for c in raw.get("required", [])],
        excluded=[_condition_from_yaml(c) for c in raw.get("excluded", [])],
        preferred=[_condition_from_yaml(c) for c in raw.get("preferred", [])],
    )


def load_strategy_config(yaml_path: Path) -> StrategyConfig:
    with yaml_path.open() as f:
        raw = yaml.safe_load(f)
    code = raw["code"]
    if code not in SCHEMAS_BY_CODE:
        raise ValueError(
            f"Strategy code '{code}' has no registered schemas in schemas.py"
        )
    scoring_schema, deep_schema = SCHEMAS_BY_CODE[code]
    return StrategyConfig(
        code=code,
        version=raw["version"],
        name=raw["name"],
        description=raw.get("description", ""),
        screening_rule=_rule_from_yaml(raw["screening_rule"]),
        scoring_prompt_template=raw["scoring_prompt"],
        deep_prompt_template=raw["deep_prompt"],
        scoring_schema=scoring_schema,
        deep_schema=deep_schema,
    )


async def sync_strategies_to_db(strategies_dir: Path) -> int:
    """Read all *.yaml in strategies_dir, upsert into strategy table."""
    n = 0
    async with session_scope() as session:
        repo = StrategyRepository(session)
        for yaml_path in sorted(strategies_dir.glob("*.yaml")):
            with yaml_path.open() as f:
                raw = yaml.safe_load(f)
            await repo.upsert(
                code=raw["code"],
                name=raw["name"],
                version=raw["version"],
                description=raw.get("description"),
                config_json=raw,
                enabled=True,
            )
            n += 1
    return n
```

- [ ] **Step 4: 写测试 `tests/unit/test_strategy_loader.py`**

```python
from pathlib import Path

import pytest
import yaml

from investment_agent.strategies.loader import (
    load_strategy_config,
    sync_strategies_to_db,
)
from investment_agent.strategies.schemas import ValueStockScore


def test_load_strategy_config_value(tmp_path):
    p = tmp_path / "value.yaml"
    p.write_text(yaml.dump({
        "code": "value", "version": "v1.0", "name": "Value Investor",
        "description": "test",
        "screening_rule": {
            "required": [{"metric": "pe", "op": "<", "value": 25}],
            "excluded": [],
            "preferred": [],
        },
        "scoring_prompt": "strategies/value/scoring",
        "deep_prompt": "strategies/value/deep",
    }))
    cfg = load_strategy_config(p)
    assert cfg.code == "value"
    assert cfg.scoring_schema is ValueStockScore
    assert cfg.screening_rule.required[0].metric == "pe"


def test_unknown_code_raises(tmp_path):
    p = tmp_path / "unknown.yaml"
    p.write_text(yaml.dump({
        "code": "ghost", "version": "v1.0", "name": "x", "description": "x",
        "screening_rule": {"required": [], "excluded": [], "preferred": []},
        "scoring_prompt": "a", "deep_prompt": "b",
    }))
    with pytest.raises(ValueError, match="no registered schemas"):
        load_strategy_config(p)


@pytest.mark.asyncio
async def test_sync_strategies_inserts_rows(tmp_path, db_session):
    # `db_session` fixture init's engine; sync uses session_scope which reuses it
    s_dir = tmp_path / "strategies"
    s_dir.mkdir()
    (s_dir / "value.yaml").write_text(yaml.dump({
        "code": "value", "version": "v1.0", "name": "Value", "description": "x",
        "screening_rule": {"required": [], "excluded": [], "preferred": []},
        "scoring_prompt": "a", "deep_prompt": "b",
    }))
    n = await sync_strategies_to_db(s_dir)
    assert n == 1

    # Verify
    from investment_agent.storage.models.analysis import Strategy
    from sqlalchemy import select

    rows = (await db_session.execute(select(Strategy))).scalars().all()
    # Note: sync_strategies_to_db creates its own session; results may be in a different session.
    # If empty, the fixture pattern needs adjustment — see test_strategy_loader.py concerns below.
```

**注意**：上面第三个测试 `test_sync_strategies_inserts_rows` 有 session 隔离问题（`sync_strategies_to_db` 使用 `session_scope()` 创建独立 session，与测试 fixture 不共享）。在实施时，把第三个测试改成验证 sync 函数返回值（`n == 1`）并跳过 DB 行检查；DB 行的验证留给 Task 7 的 CLI 集成测试。

- [ ] **Step 5: 测试 + commit**

```bash
pytest tests/unit/test_strategy_loader.py -v  # 至少 2 passed
git add src/investment_agent/storage/repositories/strategy.py \
       src/investment_agent/strategies/loader.py \
       strategies/ \
       tests/unit/test_strategy_loader.py
git commit -m "feat: strategy YAML loader + StrategyRepository + 3 strategy configs"
```

---

## Task 6: 3 个策略的 Prompt 模板（Tier 2 scoring + Tier 3 deep）

**Files (6 个 Jinja2 模板):**
- `prompts/strategies/value/scoring.j2`, `deep.j2`
- `prompts/strategies/growth/scoring.j2`, `deep.j2`
- `prompts/strategies/risk_aggressive/scoring.j2`, `deep.j2`

每个模板基本结构：

**Scoring 模板**（约 50-80 行）：
- 角色设定（"你是一位价值投资分析师 ..."）
- 任务描述（"基于以下数据给 {{ listing.ticker }} 打 0-100 分，输出 JSON 结构"）
- 数据上下文（{{ listing }} 关键指标、{{ context.peer_summary }}、{{ context.recent_news_titles }}）
- 评分维度（每个策略不同：value 关注估值/安全边际；growth 关注 TAM/增长；risk_aggressive 关注 catalyst/技术面）
- 输出 JSON schema 描述（Pydantic-AI 会强制校验，但 prompt 里也给出示例帮助 LLM）

**Deep 模板**（约 80-150 行）：
- 角色设定
- 任务（输出 DeepAnalysis JSON）
- 数据上下文（更丰富：{{ context.financials_history }}、{{ context.peer_comparables }}、{{ context.known_relationships }}、{{ context.auxiliary_context }}）
- 各章节说明（商业模式 / 护城河 / 风险因子 / 上下游 / 竞争对手 / 估值 / extracted_relationships）

- [ ] **Step 1: 创建所有 6 个模板文件**

由于 prompt 工程是 Plan B 最依赖具体业务经验的部分，**这 6 个模板的具体内容由实施者根据 Plan B 起步时的最新 LLM 表现 + 用户偏好确定**，本 Task 仅强制以下结构约束：

1. 所有 6 个模板都使用 Jinja2 语法（`{{ var }}` 和 `{% for ... %}`）
2. Scoring 模板**必须**在最后强制 LLM 输出 JSON（Pydantic-AI 会再次校验）
3. Deep 模板**必须**在最后要求 LLM 输出 `extracted_relationships` 数组（即使为空），用于 Task 13 提取
4. 每个模板顶部用 HTML 注释 `<!-- VERSION: v1.0 -->` 标注版本（便于人工 review / 调试时识别）

实施者起步可以用以下"骨架"作为 6 个模板的起点（实际使用前需要根据真实 LLM 输出调优）：

`prompts/strategies/value/scoring.j2`:

```jinja2
<!-- VERSION: v1.0 -->
You are a senior value investor in the style of Warren Buffett and Benjamin Graham.
Your task: score the stock {{ listing.ticker }} ({{ listing.company_name }}) on a 0-100 scale
from a value-investing perspective.

# Stock Data
- Ticker: {{ listing.ticker }}
- Sector: {{ listing.sector_code }}
- Market Cap: {{ listing.market_cap }}
- PE: {{ listing.pe }} | PB: {{ listing.pb }} | PEG: {{ listing.peg }}
- ROE: {{ listing.roe }} | ROA: {{ listing.roa }}
- FCF Yield: {{ listing.fcf_yield }}
- Debt/Equity: {{ listing.debt_to_equity }}
- Revenue Growth YoY: {{ listing.revenue_growth_yoy }}

# Peers
{% for p in context.peer_summary %}
- {{ p.ticker }}: PE={{ p.pe }}, ROE={{ p.roe }}
{% endfor %}

# Recent News (Titles)
{% for t in context.recent_news_titles %}
- {{ t }}
{% endfor %}

# Recent Analyst Rating Changes
{% for r in context.recent_rating_changes %}
- {{ r.firm }} {{ r.action }} → {{ r.to_rating }}
{% endfor %}

# Scoring Rubric (Value Lens)

1. Valuation (40 pts): How undervalued is the current price relative to intrinsic value?
2. Quality (25 pts): ROE, FCF generation, debt levels, earnings consistency.
3. Moat (20 pts): Competitive advantages, brand, switching costs, network effects.
4. Catalyst (10 pts): Near-term events that could close the value gap.
5. Risks Deductions (-5 to 0 pts): Major headwinds, governance concerns, secular decline.

# Output

Return a valid JSON object matching:
{
  "score": <int 0-100>,
  "confidence": <float 0-1>,
  "bull_thesis": "<3-5 sentences>",
  "bear_thesis": "<3-5 sentences>",
  "key_metrics_explained": {"pe": "...", "roe": "...", ...},
  "intrinsic_value_estimate": <float or null>,
  "margin_of_safety_pct": <float or null>
}
```

(其余 5 个模板按类似骨架，根据 strategy 的"评分维度"和 schema 字段调整。Growth 模板强调 TAM、扩张性；Risk 模板强调 catalyst、技术面。Deep 模板格式相似但要求更丰富的输出。**实施者请用 1-2 小时迭代调试 prompts**，可以用真实 LLM 调用对几只股票测试输出质量，调整 prompt 直到 confidence > 0.7 且 bull/bear thesis 有实质内容。)

- [ ] **Step 2: Commit prompts**

```bash
git add prompts/
git commit -m "feat: 6 strategy prompt templates (value/growth/risk_aggressive × scoring/deep) v1.0"
```

---

## Task 7: 策略 Registry + CLI `ia strategy`

**Files:**
- Create: `src/investment_agent/strategies/registry.py`
- Create: `src/investment_agent/cli/commands/strategy.py`
- Modify: `src/investment_agent/cli/main.py` (注册 strategy 命令组)
- Create: `tests/unit/test_strategy_registry.py`

- [ ] **Step 1: 实现 `strategies/registry.py`**

```python
"""Strategy registry — instantiates concrete StrategyAgent objects from YAML configs."""

from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import LLMTierConfig
from investment_agent.llm.agent_factory import build_agent
from investment_agent.strategies.base import BaseStrategyAgent
from investment_agent.strategies.loader import load_strategy_config


PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_STRATEGIES_DIR = PROJECT_ROOT / "strategies"
DEFAULT_PROMPTS_DIR = PROJECT_ROOT / "prompts"


def build_strategy_agent(
    *,
    yaml_path: Path,
    tier2_config: LLMTierConfig,
    tier3_config: LLMTierConfig,
    prompts_root: Path = DEFAULT_PROMPTS_DIR,
) -> BaseStrategyAgent:
    cfg = load_strategy_config(yaml_path)

    tier2_agent = build_agent(
        config=tier2_config, system_prompt="", output_type=cfg.scoring_schema,
    )
    tier3_agent = build_agent(
        config=tier3_config, system_prompt="", output_type=cfg.deep_schema,
    )

    class _Concrete(BaseStrategyAgent):
        pass
    _Concrete.config = cfg
    return _Concrete(
        prompts_dir=prompts_root,
        tier2_agent=tier2_agent,
        tier3_agent=tier3_agent,
    )


def load_all_strategies(
    *,
    tier2_config: LLMTierConfig,
    tier3_config: LLMTierConfig,
    strategies_dir: Path = DEFAULT_STRATEGIES_DIR,
    prompts_root: Path = DEFAULT_PROMPTS_DIR,
) -> dict[str, BaseStrategyAgent]:
    out: dict[str, BaseStrategyAgent] = {}
    for yaml_path in sorted(strategies_dir.glob("*.yaml")):
        agent = build_strategy_agent(
            yaml_path=yaml_path,
            tier2_config=tier2_config,
            tier3_config=tier3_config,
            prompts_root=prompts_root,
        )
        out[agent.code] = agent
    return out
```

- [ ] **Step 2: 实现 CLI `cli/commands/strategy.py`**

```python
"""ia strategy sub-commands: list / show / sync."""

import asyncio

import typer
from rich import print as rprint
from rich.table import Table

app = typer.Typer(help="Strategy management commands")


@app.command("list")
def list_strategies() -> None:
    """List all strategies registered in DB."""
    from investment_agent.storage.database import session_scope
    from investment_agent.storage.repositories.strategy import StrategyRepository

    async def _do():
        async with session_scope() as session:
            repo = StrategyRepository(session)
            return await repo.list_all()

    rows = asyncio.run(_do())
    table = Table(title="Strategies")
    table.add_column("Code")
    table.add_column("Name")
    table.add_column("Version")
    table.add_column("Enabled")
    for s in rows:
        table.add_row(s.code, s.name, s.version, "✓" if s.enabled else "✗")
    rprint(table)


@app.command("show")
def show_strategy(code: str) -> None:
    """Show one strategy's config."""
    import json
    from investment_agent.storage.database import session_scope
    from investment_agent.storage.repositories.strategy import StrategyRepository

    async def _do():
        async with session_scope() as session:
            repo = StrategyRepository(session)
            return await repo.get(code)

    s = asyncio.run(_do())
    if s is None:
        rprint(f"[red]Strategy '{code}' not found[/red]")
        raise typer.Exit(1)
    rprint(f"[bold]{s.name}[/bold] (code={s.code}, version={s.version})")
    rprint(f"Description: {s.description}")
    rprint("Config:")
    rprint(json.dumps(s.config_json, indent=2, ensure_ascii=False))


@app.command("sync")
def sync_strategies() -> None:
    """Sync strategies/*.yaml → DB."""
    from investment_agent.strategies.loader import sync_strategies_to_db
    from investment_agent.strategies.registry import DEFAULT_STRATEGIES_DIR

    n = asyncio.run(sync_strategies_to_db(DEFAULT_STRATEGIES_DIR))
    rprint(f"[green]Synced {n} strategies to DB[/green]")
```

- [ ] **Step 3: 注册到 `cli/main.py`**

修改 `src/investment_agent/cli/main.py`，加上：

```python
from investment_agent.cli.commands import strategy as strategy_cmd
# ...
app.add_typer(strategy_cmd.app, name="strategy", help="Strategy management")
```

- [ ] **Step 4: 测试**

```python
# tests/unit/test_strategy_registry.py

from typer.testing import CliRunner


def test_cli_strategy_help():
    from investment_agent.cli.main import app
    r = CliRunner().invoke(app, ["strategy", "--help"])
    assert r.exit_code == 0
    assert "list" in r.output
    assert "sync" in r.output
    assert "show" in r.output
```

- [ ] **Step 5: 实际 sync 一次（含 DB 落地验证）**

```bash
source .venv/bin/activate
ia strategy sync   # 应该输出 "Synced 3 strategies to DB"
ia strategy list   # 应该看到 value/growth/risk_aggressive 三行
ia strategy show value | head -20
```

- [ ] **Step 6: Commit**

```bash
git add src/investment_agent/strategies/registry.py \
       src/investment_agent/cli/commands/strategy.py \
       src/investment_agent/cli/main.py \
       tests/unit/test_strategy_registry.py
git commit -m "feat: strategy registry + CLI ia strategy list/show/sync"
```

---

# Phase 4: Discovery Pipeline + Auxiliary Tracking

## Task 8: 新增 Repositories（PipelineRun / StockScore / Listing 扩展 / Fundamental / Price）

**Files:**
- Create: `src/investment_agent/storage/repositories/pipeline_run.py`
- Create: `src/investment_agent/storage/repositories/stock_score.py`
- Create: `src/investment_agent/storage/repositories/company.py`
- Create: `src/investment_agent/storage/repositories/fundamental.py`
- Create: `src/investment_agent/storage/repositories/price.py`
- Create: `tests/unit/test_repositories_v2.py`

每个 repo 实现简单 CRUD + 1-2 个领域查询。骨架：

**pipeline_run.py:**

```python
from datetime import datetime, timezone

from sqlalchemy import select

from investment_agent.storage.models.analysis import PipelineRun
from investment_agent.storage.repositories.base import Repository


class PipelineRunRepository(Repository[PipelineRun]):
    model = PipelineRun

    async def create(
        self, *, kind: str, universe_code: str | None = None,
        listing_id: int | None = None, triggered_by: str = "cli",
    ) -> PipelineRun:
        run = PipelineRun(
            kind=kind,
            universe_code=universe_code,
            listing_id=listing_id,
            triggered_by=triggered_by,
            started_at=datetime.now(timezone.utc),
            status="running",
            metadata_json={},
        )
        self.session.add(run)
        await self.session.flush()
        return run

    async def mark_success(self, run_id: int, metadata: dict | None = None) -> None:
        run = await self.get(run_id)
        if run is None:
            raise ValueError(f"PipelineRun {run_id} not found")
        run.status = "success"
        run.finished_at = datetime.now(timezone.utc)
        if metadata:
            run.metadata_json = {**run.metadata_json, **metadata}
        await self.session.flush()

    async def mark_failed(self, run_id: int, error_summary: str) -> None:
        run = await self.get(run_id)
        if run is None:
            raise ValueError(f"PipelineRun {run_id} not found")
        run.status = "failed"
        run.error_summary = error_summary
        run.finished_at = datetime.now(timezone.utc)
        await self.session.flush()

    async def mark_partial(self, run_id: int, error_summary: str) -> None:
        run = await self.get(run_id)
        if run is None:
            raise ValueError(f"PipelineRun {run_id} not found")
        run.status = "partial"
        run.error_summary = error_summary
        run.finished_at = datetime.now(timezone.utc)
        await self.session.flush()
```

**stock_score.py:**

```python
from sqlalchemy import select

from investment_agent.storage.models.analysis import StockScore
from investment_agent.storage.repositories.base import Repository


class StockScoreRepository(Repository[StockScore]):
    model = StockScore

    async def upsert(
        self,
        *,
        pipeline_run_id: int,
        listing_id: int,
        strategy_code: str,
        tier: int,
        score: int,
        confidence: float,
        bull_thesis: str | None = None,
        bear_thesis: str | None = None,
        key_metrics: dict | None = None,
        deep_analysis: str | None = None,
    ) -> StockScore:
        stmt = select(StockScore).where(
            StockScore.pipeline_run_id == pipeline_run_id,
            StockScore.listing_id == listing_id,
            StockScore.strategy_code == strategy_code,
        )
        existing = (await self.session.execute(stmt)).scalar_one_or_none()
        if existing is None:
            existing = StockScore(
                pipeline_run_id=pipeline_run_id,
                listing_id=listing_id,
                strategy_code=strategy_code,
                tier=tier,
                score=score,
                confidence=confidence,
                bull_thesis=bull_thesis,
                bear_thesis=bear_thesis,
                key_metrics=key_metrics or {},
                deep_analysis=deep_analysis,
            )
            self.session.add(existing)
        else:
            existing.tier = tier
            existing.score = score
            existing.confidence = confidence
            existing.bull_thesis = bull_thesis
            existing.bear_thesis = bear_thesis
            existing.key_metrics = key_metrics or {}
            if deep_analysis is not None:
                existing.deep_analysis = deep_analysis
        await self.session.flush()
        return existing

    async def by_run(self, run_id: int) -> list[StockScore]:
        stmt = select(StockScore).where(StockScore.pipeline_run_id == run_id)
        return list((await self.session.execute(stmt)).scalars().all())
```

**company.py / fundamental.py / price.py:** 类似骨架，实现 `upsert_*` 和 `latest_*` 查询。每个 repo 约 30-60 行。

- [ ] **Step 1: 创建上述 5 个 repo 文件**（具体内容按骨架，每个 30-60 行）

- [ ] **Step 2: 写测试 `test_repositories_v2.py`**

测试每个 repo 的核心 upsert + 查询路径，约 8-12 个测试用例。每个 ~15 行。

- [ ] **Step 3: Commit**

```bash
git add src/investment_agent/storage/repositories/ tests/unit/test_repositories_v2.py
git commit -m "feat: repos for PipelineRun, StockScore, Company, Fundamental, Price"
```

---

## Task 9: Pipeline Step 1 — 数据准备（concurrent fetching）

**Files:**
- Create: `src/investment_agent/pipelines/__init__.py`
- Create: `src/investment_agent/pipelines/steps.py`
- Create: `tests/integration/test_discovery_pipeline.py` (起步)

`steps.py` 包含 7 个 step 函数，本 Task 实现 step1_data_ingestion。

```python
"""Discovery pipeline step functions. Each step is async and idempotent."""

import asyncio
from datetime import date, datetime, timedelta
from decimal import Decimal

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import Settings
from investment_agent.domain.protocols import DataSource
from investment_agent.domain.strategy_types import ListingSnapshot
from investment_agent.sources.records import (
    AnalystRatingRecord, DailyPriceRecord, FinancialReportRecord,
    FundamentalSnapshotRecord, ListingRef, NewsRecord,
)
from investment_agent.sources.retry import RateLimiter
from investment_agent.storage.models.market_data import (
    AnalystRatingChange, DailyPrice, FinancialReport,
    FundamentalSnapshot, NewsItem,
)
from investment_agent.storage.models.reference import Listing, UniverseMembership
from investment_agent.storage.models.user_domain import AuxiliaryListing
from sqlalchemy import select

log = structlog.get_logger(__name__)


async def step1_data_ingestion(
    session: AsyncSession,
    *,
    universe_code: str,
    data_source: DataSource,
    as_of: date,
    pipeline_run_id: int,
) -> tuple[list[Listing], list[Listing]]:
    """Pull EOD prices, fundamentals, news, ratings for all universe listings
    plus all enabled auxiliary listings.

    Returns (universe_listings, auxiliary_listings).
    """
    # 1. Fetch universe listings from DB
    stmt = (
        select(Listing)
        .join(UniverseMembership, UniverseMembership.listing_id == Listing.id)
        .where(
            UniverseMembership.universe_code == universe_code,
            (UniverseMembership.valid_to.is_(None)) | (UniverseMembership.valid_to >= as_of),
            UniverseMembership.valid_from <= as_of,
        )
    )
    universe_listings = list((await session.execute(stmt)).scalars().all())

    # 2. Fetch auxiliary listings
    aux_stmt = (
        select(Listing)
        .join(AuxiliaryListing, AuxiliaryListing.listing_id == Listing.id)
        .where(AuxiliaryListing.enabled.is_(True))
    )
    aux_listings = list((await session.execute(aux_stmt)).scalars().all())

    # 3. Concurrently fetch data for universe + aux (auxiliary is lighter — no fundamentals snapshot)
    settings = Settings()
    limiter = RateLimiter(max_concurrent=settings.yfinance_max_concurrent)

    async def _fetch_universe_one(listing: Listing) -> dict:
        results = {"ticker": listing.ticker, "errors": []}
        async with limiter:
            try:
                # Daily price (last 5 trading days to fill gaps)
                prices = await data_source.fetch_daily_prices(
                    [listing.ticker], start=as_of - timedelta(days=7), end=as_of,
                )
                results["prices"] = prices
            except Exception as e:
                results["errors"].append(f"prices: {e}")

            try:
                snap = await data_source.fetch_fundamental_snapshot(listing.ticker, as_of)
                results["snapshot"] = snap
            except Exception as e:
                results["errors"].append(f"snapshot: {e}")

            try:
                news = await data_source.fetch_news(listing.ticker, days=1)
                results["news"] = news
            except Exception as e:
                results["errors"].append(f"news: {e}")

            try:
                ratings = await data_source.fetch_analyst_ratings(listing.ticker, days=1)
                results["ratings"] = ratings
            except Exception as e:
                results["errors"].append(f"ratings: {e}")
        return results

    async def _fetch_aux_one(listing: Listing) -> dict:
        """Auxiliary: only prices + news + ratings (no fundamental snapshot)."""
        results = {"ticker": listing.ticker, "errors": []}
        async with limiter:
            try:
                prices = await data_source.fetch_daily_prices(
                    [listing.ticker], start=as_of - timedelta(days=7), end=as_of,
                )
                results["prices"] = prices
            except Exception as e:
                results["errors"].append(f"prices: {e}")
            try:
                news = await data_source.fetch_news(listing.ticker, days=1)
                results["news"] = news
            except Exception as e:
                results["errors"].append(f"news: {e}")
        return results

    universe_results = await asyncio.gather(
        *[_fetch_universe_one(l) for l in universe_listings], return_exceptions=False,
    )
    aux_results = await asyncio.gather(
        *[_fetch_aux_one(l) for l in aux_listings], return_exceptions=False,
    )

    # 4. Persist
    listings_by_ticker = {l.ticker: l for l in universe_listings + aux_listings}
    for batch_results in (universe_results, aux_results):
        for r in batch_results:
            listing = listings_by_ticker[r["ticker"]]
            for record in r.get("prices", []):
                await _upsert_daily_price(session, listing.id, record)
            if "snapshot" in r:
                await _upsert_fundamental_snapshot(session, listing.id, r["snapshot"])
            for record in r.get("news", []):
                await _insert_news_if_new(session, listing.company_id, record)
            for record in r.get("ratings", []):
                await _insert_rating_if_new(session, listing.company_id, record)

    await session.flush()
    log.info(
        "pipeline.step1.complete",
        universe_n=len(universe_listings), aux_n=len(aux_listings),
        run_id=pipeline_run_id,
    )
    return universe_listings, aux_listings


async def _upsert_daily_price(session, listing_id: int, record: DailyPriceRecord) -> None:
    existing = await session.get(DailyPrice, (listing_id, record.date))
    if existing is None:
        session.add(DailyPrice(
            listing_id=listing_id, date=record.date,
            open=record.open, high=record.high, low=record.low,
            close=record.close, volume=record.volume,
            adjusted_close=record.adjusted_close,
        ))


async def _upsert_fundamental_snapshot(session, listing_id: int, record: FundamentalSnapshotRecord) -> None:
    existing = await session.get(FundamentalSnapshot, (listing_id, record.date))
    if existing is None:
        session.add(FundamentalSnapshot(
            listing_id=listing_id, date=record.date,
            pe=record.pe, pb=record.pb, peg=record.peg, ps=record.ps,
            roe=record.roe, roa=record.roa, fcf_yield=record.fcf_yield,
            debt_to_equity=record.debt_to_equity, market_cap=record.market_cap,
        ))


async def _insert_news_if_new(session, company_id: int, record: NewsRecord) -> None:
    # Dedupe by URL
    stmt = select(NewsItem).where(NewsItem.url == record.url)
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        return
    session.add(NewsItem(
        company_id=company_id, published_at=record.published_at,
        source=record.source, title=record.title, url=record.url,
        summary=record.summary,
    ))


async def _insert_rating_if_new(session, company_id: int, record: AnalystRatingRecord) -> None:
    # Dedupe by (company, date, firm, action)
    stmt = select(AnalystRatingChange).where(
        AnalystRatingChange.company_id == company_id,
        AnalystRatingChange.date == record.date,
        AnalystRatingChange.firm == record.firm,
        AnalystRatingChange.action == record.action,
    )
    if (await session.execute(stmt)).scalar_one_or_none() is not None:
        return
    session.add(AnalystRatingChange(
        company_id=company_id, date=record.date, firm=record.firm,
        action=record.action, from_rating=record.from_rating,
        to_rating=record.to_rating, price_target=record.price_target,
    ))
```

- [ ] **Step 1: 实现 `pipelines/steps.py` step1**（见上）

- [ ] **Step 2: 集成测试骨架 `tests/integration/test_discovery_pipeline.py`**

```python
"""Discovery pipeline end-to-end with fake DataSource + fake LLM."""

from datetime import date
from decimal import Decimal

import pytest

from investment_agent.pipelines.steps import step1_data_ingestion
from investment_agent.sources.records import (
    DailyPriceRecord, FundamentalSnapshotRecord, NewsRecord, AnalystRatingRecord,
)


class FakeDataSource:
    name = "fake"
    supported_markets = ["NASDAQ"]

    async def fetch_universe(self, code, as_of):
        return []

    async def fetch_daily_prices(self, tickers, start, end):
        return [
            DailyPriceRecord(
                ticker=t, date=date(2025, 6, 3),
                open=Decimal("100"), high=Decimal("101"), low=Decimal("99"),
                close=Decimal("100.5"), volume=1000000, adjusted_close=Decimal("100.5"),
            ) for t in tickers
        ]

    async def fetch_fundamental_snapshot(self, ticker, as_of):
        return FundamentalSnapshotRecord(
            ticker=ticker, date=as_of, pe=Decimal("20"), pb=Decimal("3"),
            peg=None, ps=None, roe=Decimal("0.15"), roa=Decimal("0.08"),
            fcf_yield=Decimal("0.06"), debt_to_equity=Decimal("1.0"),
            market_cap=Decimal("100000000000"),
        )

    async def fetch_financial_report(self, ticker, periods):
        return []

    async def fetch_news(self, ticker, days):
        return []

    async def fetch_analyst_ratings(self, ticker, days):
        return []


@pytest.mark.asyncio
async def test_step1_ingestion_populates_price_and_fundamental(db_session, seeded_market):
    from investment_agent.storage.models.reference import Universe, UniverseMembership
    from investment_agent.storage.repositories.listing import ListingRepository

    # Setup: universe + listing
    db_session.add(Universe(code="TEST_U", name="Test"))
    repo = ListingRepository(db_session)
    listing = await repo.upsert_listing(
        market_code="NASDAQ", ticker="AAPL", company_name="Apple Inc."
    )
    db_session.add(UniverseMembership(
        universe_code="TEST_U", listing_id=listing.id, valid_from=date(2024, 1, 1),
    ))
    await db_session.flush()

    # Create a fake pipeline_run row
    from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
    run = await PipelineRunRepository(db_session).create(
        kind="discovery", universe_code="TEST_U",
    )

    # Run step 1
    universe_listings, aux_listings = await step1_data_ingestion(
        db_session, universe_code="TEST_U", data_source=FakeDataSource(),
        as_of=date(2025, 6, 3), pipeline_run_id=run.id,
    )
    assert len(universe_listings) == 1
    assert len(aux_listings) == 0

    # Verify price + snapshot persisted
    from sqlalchemy import select
    from investment_agent.storage.models.market_data import DailyPrice, FundamentalSnapshot
    prices = (await db_session.execute(select(DailyPrice))).scalars().all()
    assert len(prices) == 1
    snaps = (await db_session.execute(select(FundamentalSnapshot))).scalars().all()
    assert len(snaps) == 1
```

- [ ] **Step 3: 测试 + commit**

```bash
pytest tests/integration/test_discovery_pipeline.py -v
git add src/investment_agent/pipelines/ tests/integration/test_discovery_pipeline.py
git commit -m "feat: Discovery Pipeline Step 1 (concurrent data ingestion + auxiliary)"
```

---

## Task 10: Pipeline Step 2 — Tier 1 算法粗筛

在 `pipelines/steps.py` 追加 `step2_tier1_screening`：

```python
async def step2_tier1_screening(
    session: AsyncSession,
    *,
    listings: list[Listing],
    strategies: dict[str, BaseStrategyAgent],
    as_of: date,
) -> dict[str, list[ListingSnapshot]]:
    """For each enabled strategy, run its `screen()` against universe listings.

    Returns: {strategy_code: [passing snapshots]}.
    """
    snapshots = await _build_listing_snapshots(session, listings, as_of)

    out: dict[str, list[ListingSnapshot]] = {}
    for code, agent in strategies.items():
        passed = await agent.screen(snapshots)
        out[code] = passed
        log.info(
            "pipeline.step2.screened",
            strategy=code, total_candidates=len(snapshots), passed=len(passed),
        )
    return out


async def _build_listing_snapshots(
    session: AsyncSession, listings: list[Listing], as_of: date,
) -> list[ListingSnapshot]:
    """Build ListingSnapshot DTOs from DB state."""
    from investment_agent.storage.models.market_data import (
        FundamentalSnapshot, FinancialReport,
    )
    out: list[ListingSnapshot] = []
    for l in listings:
        snap_row = await session.get(FundamentalSnapshot, (l.id, as_of))
        # FinancialReport: latest by period_end
        from sqlalchemy import select as _sel
        stmt = (
            _sel(FinancialReport)
            .where(FinancialReport.company_id == l.company_id)
            .order_by(FinancialReport.period_end.desc())
            .limit(4)  # 4 quarters TTM
        )
        recent_4q = list((await session.execute(stmt)).scalars().all())

        revenue_ttm = sum(filter(None, (q.revenue for q in recent_4q))) or None
        net_income_ttm = sum(filter(None, (q.net_income for q in recent_4q))) or None

        out.append(ListingSnapshot(
            listing_id=l.id, company_id=l.company_id,
            ticker=l.ticker, market_code=l.market_code,
            company_name=l.company.canonical_name if l.company else "",
            sector_code=l.company.sector_code if l.company else None,
            as_of=as_of,
            pe=snap_row.pe if snap_row else None,
            pb=snap_row.pb if snap_row else None,
            peg=snap_row.peg if snap_row else None,
            ps=snap_row.ps if snap_row else None,
            roe=snap_row.roe if snap_row else None,
            roa=snap_row.roa if snap_row else None,
            fcf_yield=snap_row.fcf_yield if snap_row else None,
            debt_to_equity=snap_row.debt_to_equity if snap_row else None,
            market_cap=snap_row.market_cap if snap_row else None,
            revenue_ttm=revenue_ttm,
            net_income_ttm=net_income_ttm,
        ))
    return out
```

加测试覆盖 step2（mock 3 个简单 stub strategy，验证不同 ScreeningRule 产出不同候选列表）。

Commit: `feat: Discovery Pipeline Step 2 (Tier 1 algorithmic screening)`

---

## Task 11: Pipeline Step 3 — Tier 2 LLM 评分（带 Semaphore）

在 `steps.py` 追加 `step3_tier2_scoring`：

```python
async def step3_tier2_scoring(
    session: AsyncSession,
    *,
    candidates_by_strategy: dict[str, list[ListingSnapshot]],
    strategies: dict[str, BaseStrategyAgent],
    pipeline_run_id: int,
    max_concurrent: int = 8,
    timeout_sec: int = 90,
) -> int:
    """Run Tier 2 LLM scoring for each (strategy, listing) pair.

    Returns total number of scores persisted.
    """
    from investment_agent.storage.repositories.stock_score import StockScoreRepository

    semaphore = asyncio.Semaphore(max_concurrent)
    score_repo = StockScoreRepository(session)
    n = 0

    async def _score_one(strategy_code: str, listing: ListingSnapshot) -> None:
        nonlocal n
        agent = strategies[strategy_code]
        # Build minimal ScoringContext from DB (peers, news, ratings)
        ctx = await _build_scoring_context(session, listing)

        async with semaphore:
            try:
                score = await asyncio.wait_for(
                    agent.score(listing, ctx), timeout=timeout_sec,
                )
                await score_repo.upsert(
                    pipeline_run_id=pipeline_run_id,
                    listing_id=listing.listing_id,
                    strategy_code=strategy_code,
                    tier=2,
                    score=score.score,
                    confidence=score.confidence,
                    bull_thesis=score.bull_thesis,
                    bear_thesis=score.bear_thesis,
                    key_metrics=score.key_metrics_explained,
                )
                n += 1
            except Exception as e:
                log.warning(
                    "pipeline.step3.score_failed",
                    strategy=strategy_code, ticker=listing.ticker, error=str(e),
                )

    tasks = []
    for code, candidates in candidates_by_strategy.items():
        for listing in candidates:
            tasks.append(_score_one(code, listing))

    await asyncio.gather(*tasks)
    log.info("pipeline.step3.complete", scored=n, run_id=pipeline_run_id)
    return n


async def _build_scoring_context(session, listing: ListingSnapshot):
    """Build ScoringContext. For V1 just fetch top-5 peers + recent news/ratings."""
    from investment_agent.domain.strategy_types import ScoringContext

    # TODO: peer query — for V1 just return empty list. Future plan can expand.
    return ScoringContext(
        listing=listing,
        peer_summary=[],
        recent_news_titles=[],
        recent_rating_changes=[],
    )
```

加测试用 **fake StrategyAgent** 验证 step3 把 score upsert 到 DB 且 respects threshold.

Commit: `feat: Discovery Pipeline Step 3 (Tier 2 LLM scoring with semaphore)`

---

## Task 12: Pipeline Step 4 — Tier 3 深度分析（带 cache）

在 `steps.py` 追加 `step4_tier3_deep_analysis` + DeepAnalysisCache 逻辑：

```python
async def step4_tier3_deep_analysis(
    session: AsyncSession,
    *,
    pipeline_run_id: int,
    strategies: dict[str, BaseStrategyAgent],
    top_n: int = 10,
    cache_days: int = 90,
    max_concurrent: int = 4,
    timeout_sec: int = 180,
) -> list[int]:
    """Select Top-N candidates (cross-strategy avg), run deep analysis with cache.

    Returns list of listing_ids that received deep analysis.
    """
    from investment_agent.storage.models.analysis import StockScore
    from investment_agent.storage.models.ops import DeepAnalysisCache
    from sqlalchemy import select, func

    # 1. Compute Top-N by cross-strategy avg score
    stmt = (
        select(
            StockScore.listing_id,
            func.avg(StockScore.score).label("avg_score"),
        )
        .where(StockScore.pipeline_run_id == pipeline_run_id, StockScore.tier == 2)
        .group_by(StockScore.listing_id)
        .order_by(func.avg(StockScore.score).desc())
        .limit(top_n)
    )
    top_rows = list((await session.execute(stmt)).all())
    top_listing_ids = [r[0] for r in top_rows]

    # 2. For each Top-N × each enabled strategy, run deep analysis (with cache)
    semaphore = asyncio.Semaphore(max_concurrent)

    async def _deep_one(listing_id: int, strategy_code: str) -> None:
        # Cache key: (listing, latest_financial_report_period)
        # ... (cache lookup logic)
        # If miss: call agent.deep_analyze(...)
        # Persist to StockScore.deep_analysis + DeepAnalysisCache
        pass

    tasks = []
    for lid in top_listing_ids:
        for code in strategies:
            tasks.append(_deep_one(lid, code))

    await asyncio.gather(*tasks)
    log.info("pipeline.step4.complete", top_n=len(top_listing_ids), run_id=pipeline_run_id)
    return top_listing_ids
```

具体 cache 逻辑：

```python
async def _check_or_run_deep_analysis(
    session, listing_id: int, strategy_code: str,
    agent: BaseStrategyAgent, snapshot: ListingSnapshot,
    cache_days: int, pipeline_run_id: int,
):
    from investment_agent.storage.models.ops import DeepAnalysisCache
    from datetime import datetime, timezone, timedelta

    # Find latest financial report period for this listing's company
    from investment_agent.storage.models.market_data import FinancialReport
    stmt = (
        select(FinancialReport.fiscal_period)
        .where(FinancialReport.company_id == snapshot.company_id)
        .order_by(FinancialReport.period_end.desc())
        .limit(1)
    )
    period_row = (await session.execute(stmt)).scalar_one_or_none()
    if period_row is None:
        period_row = "unknown"

    # Look up cache
    cache_stmt = select(DeepAnalysisCache).where(
        DeepAnalysisCache.listing_id == listing_id,
        DeepAnalysisCache.latest_financial_report_period == period_row,
        DeepAnalysisCache.strategy_code == strategy_code,
    )
    cached = (await session.execute(cache_stmt)).scalar_one_or_none()
    cutoff = datetime.now(timezone.utc) - timedelta(days=cache_days)
    if cached is not None and cached.generated_at > cutoff:
        return cached.analysis_json

    # Cache miss — call LLM
    ctx = await _build_deep_analysis_context(session, snapshot)
    analysis = await agent.deep_analyze(snapshot, ctx)
    analysis_json = analysis.model_dump()

    # Persist cache + StockScore.deep_analysis
    if cached is None:
        session.add(DeepAnalysisCache(
            listing_id=listing_id,
            latest_financial_report_period=period_row,
            strategy_code=strategy_code,
            analysis_json=analysis_json,
            generated_at=datetime.now(timezone.utc),
        ))
    else:
        cached.analysis_json = analysis_json
        cached.generated_at = datetime.now(timezone.utc)
    return analysis_json
```

测试覆盖：cache hit / cache miss / cache 过期。

Commit: `feat: Discovery Pipeline Step 4 (Tier 3 deep analysis with 90-day cache)`

---

## Task 13: Relationship Extractor + Resolver

**Files:**
- Create: `src/investment_agent/relationships/__init__.py`
- Create: `src/investment_agent/relationships/extractor.py`
- Create: `src/investment_agent/relationships/resolver.py`
- Create: `prompts/strategies/relationship_extraction/extract.j2`
- Create: `tests/unit/test_relationship_extractor.py`

`extractor.py`:
- 输入：DeepAnalysis 对象（已含 `extracted_relationships: list[ExtractedRelationship]`）
- 调用 resolver 把 counterparty_name → company_id（fuzzy match 或新建 placeholder Company）
- 写入 `CompanyRelationship` 表，初始 `confidence < 0.7`，`source='llm_extracted'`，`valid_from=today`

`resolver.py`:
```python
async def resolve_counterparty(
    session: AsyncSession,
    counterparty_name: str,
    counterparty_ticker: str | None = None,
) -> int:
    """Return company_id. Create placeholder Company if unknown."""
    from investment_agent.storage.models.reference import Company, Listing
    from sqlalchemy import select, func

    # 1. If ticker provided, try Listing match first
    if counterparty_ticker:
        listing = (await session.execute(
            select(Listing).where(Listing.ticker == counterparty_ticker)
        )).scalar_one_or_none()
        if listing:
            return listing.company_id

    # 2. Fuzzy match by canonical_name (LIKE %name%)
    stmt = (
        select(Company)
        .where(func.lower(Company.canonical_name).like(f"%{counterparty_name.lower()}%"))
        .limit(1)
    )
    found = (await session.execute(stmt)).scalar_one_or_none()
    if found:
        return found.id

    # 3. Create placeholder Company
    new_company = Company(
        canonical_name=counterparty_name,
        description=f"Placeholder (auto-created from LLM extraction)",
    )
    session.add(new_company)
    await session.flush()
    return new_company.id
```

Tests:
- Test resolver matches existing company by ticker
- Test resolver matches by fuzzy name
- Test resolver creates placeholder when no match
- Test extractor writes CompanyRelationship rows with correct confidence/source

Commit: `feat: relationship extractor + counterparty resolver (LLM → DB)`

---

## Task 14: Auxiliary Tracking 派生 Job

**Files:**
- Create: `src/investment_agent/auxiliary/__init__.py`
- Create: `src/investment_agent/auxiliary/derivation.py`
- Create: `src/investment_agent/cli/commands/auxiliary.py`
- Create: `tests/unit/test_auxiliary_derivation.py`

`derivation.py`:
```python
"""Derive auxiliary_listing entries from company_relationship (strength ≥ threshold)."""

from datetime import datetime, timezone

from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.config import load_app_yaml
from investment_agent.storage.models.reference import Listing
from investment_agent.storage.models.relationship import CompanyRelationship
from investment_agent.storage.models.user_domain import AuxiliaryListing


async def derive_auxiliary_listings(
    session: AsyncSession,
    *,
    strength_threshold: float = 0.5,
    max_per_company: int = 5,
    total_cap: int = 200,
) -> dict:
    """Scan CompanyRelationship table; add high-strength counterparties
    that are not in current universes as AuxiliaryListings.

    Returns: {"added": N, "removed": N, "current_total": N}
    """
    # 1. Get candidates: counterparties with strength ≥ threshold
    stmt = (
        select(CompanyRelationship)
        .where(CompanyRelationship.strength >= strength_threshold)
        .where(CompanyRelationship.valid_to.is_(None))
    )
    relationships = list((await session.execute(stmt)).scalars().all())

    # 2. Group by `to_company_id`, count, filter to top max_per_company per source company
    # 3. For each counterparty, find Listings (any market) and ensure AuxiliaryListing exists
    # 4. Respect total_cap

    added = removed = 0
    # ... full implementation per spec §2.6
    return {"added": added, "removed": removed, "current_total": 0}
```

CLI:
- `ia auxiliary list` — show current auxiliary listings
- `ia auxiliary add <ticker>` — manual add
- `ia auxiliary remove <listing_id>` — manual disable
- `ia auxiliary refresh` — run derivation job

Commit: `feat: Auxiliary Tracking derivation job + CLI ia auxiliary`

---

## Task 15: Pipeline Step 1 完整覆盖 auxiliary listings

(In Task 9 we already wrote step1 fetching aux listings. This task is verification + edge cases:)

- Verify step1 picks up auxiliary listings even when their tickers cross markets (.TW / .HK / .SZ)
- Add test: auxiliary listing with `enabled=False` is skipped
- Add test: auxiliary listing whose company has no Listing entry creates one on the fly

Commit: `test: auxiliary tracking edge cases in step1 ingestion`

---

## Task 16: DiscoveryPipeline Orchestrator

**Files:**
- Create: `src/investment_agent/pipelines/discovery.py`

```python
"""DiscoveryPipeline: orchestrates 7 steps end-to-end."""

import asyncio
from datetime import date, datetime, timezone

import structlog

from investment_agent.config import LLMTierConfig, Settings, load_app_yaml
from investment_agent.domain.protocols import DataSource
from investment_agent.pipelines import steps
from investment_agent.storage.database import session_scope
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.strategies.base import BaseStrategyAgent
from investment_agent.strategies.registry import load_all_strategies

log = structlog.get_logger(__name__)


class DiscoveryPipeline:
    def __init__(
        self,
        *,
        data_source: DataSource,
        strategies: dict[str, BaseStrategyAgent],
        settings: Settings,
        app_config_path: str = "config/app.yaml",
    ) -> None:
        self._data_source = data_source
        self._strategies = strategies
        self._settings = settings
        from pathlib import Path
        self._app_config = load_app_yaml(Path(app_config_path))

    async def run(self, universe_code: str, as_of: date) -> int:
        """Run full Discovery pipeline. Returns pipeline_run_id."""
        async with session_scope() as session:
            run_repo = PipelineRunRepository(session)
            run = await run_repo.create(
                kind="discovery", universe_code=universe_code,
                triggered_by="api",
            )
            run_id = run.id

        partial_errors: list[str] = []

        try:
            # Step 1: Data ingestion
            async with session_scope() as session:
                universe, aux = await steps.step1_data_ingestion(
                    session, universe_code=universe_code,
                    data_source=self._data_source, as_of=as_of,
                    pipeline_run_id=run_id,
                )

            # Step 2: Tier 1 screening
            async with session_scope() as session:
                # Need to re-load listings in this session
                from sqlalchemy import select
                from investment_agent.storage.models.reference import Listing
                ids = [l.id for l in universe]
                stmt = select(Listing).where(Listing.id.in_(ids))
                universe_in_session = list((await session.execute(stmt)).scalars().all())
                candidates_by_strategy = await steps.step2_tier1_screening(
                    session, listings=universe_in_session,
                    strategies=self._strategies, as_of=as_of,
                )

            # Step 3: Tier 2 LLM scoring
            async with session_scope() as session:
                n_scored = await steps.step3_tier2_scoring(
                    session,
                    candidates_by_strategy=candidates_by_strategy,
                    strategies=self._strategies,
                    pipeline_run_id=run_id,
                    max_concurrent=self._settings.llm_max_concurrent,
                )

            # Step 4: Tier 3 deep analysis
            async with session_scope() as session:
                top_ids = await steps.step4_tier3_deep_analysis(
                    session, pipeline_run_id=run_id,
                    strategies=self._strategies,
                    top_n=self._app_config.pipeline.tier3_top_n,
                    cache_days=self._app_config.pipeline.deep_analysis_cache_days,
                )

            # Step 5: Meta-Agent synthesis
            async with session_scope() as session:
                from investment_agent.meta.synthesizer import synthesize_discovery_report
                report = await synthesize_discovery_report(session, run_id)

            # Step 6: Render + dispatch
            async with session_scope() as session:
                from investment_agent.renderers.markdown_renderer import MarkdownRenderer
                from investment_agent.notifiers.file_notifier import FileNotifier
                # ... render + send
                pass

            # Step 7: Mark success
            async with session_scope() as session:
                await PipelineRunRepository(session).mark_success(
                    run_id, metadata={"errors": partial_errors},
                )

        except Exception as e:
            log.exception("pipeline.failed", run_id=run_id)
            async with session_scope() as session:
                await PipelineRunRepository(session).mark_failed(run_id, str(e))
            raise

        return run_id
```

Commit: `feat: DiscoveryPipeline orchestrator (Steps 1-7)`

---

## Task 17: CLI `ia pipeline discover`

**Files:**
- Create: `src/investment_agent/cli/commands/pipeline.py`
- Modify: `src/investment_agent/cli/main.py`

```python
"""ia pipeline sub-commands."""

import asyncio
from datetime import date

import typer
from rich import print as rprint

app = typer.Typer(help="Pipeline commands")


@app.command("discover")
def discover(
    universe: str = typer.Option("NASDAQ_100"),
    as_of: str = typer.Option(date.today().isoformat()),
) -> None:
    """Run Discovery pipeline once."""
    from investment_agent.config import Settings
    from investment_agent.pipelines.discovery import DiscoveryPipeline
    from investment_agent.sources.yfinance_source import YFinanceSource
    from investment_agent.strategies.registry import load_all_strategies

    settings = Settings()
    strategies = load_all_strategies(
        tier2_config=settings.llm_tier2, tier3_config=settings.llm_tier3,
    )
    pipeline = DiscoveryPipeline(
        data_source=YFinanceSource(max_concurrent=settings.yfinance_max_concurrent),
        strategies=strategies, settings=settings,
    )
    run_id = asyncio.run(pipeline.run(universe, date.fromisoformat(as_of)))
    rprint(f"[green]PipelineRun {run_id} complete[/green]")


@app.command("list")
def list_runs(limit: int = 20) -> None:
    """List recent pipeline runs."""
    # ... query pipeline_run table, render with rich.Table
    pass


@app.command("show")
def show_run(run_id: int) -> None:
    """Show details of a single run."""
    pass
```

Commit: `feat: CLI ia pipeline discover/list/show`

---

# Phase 6: Meta-Agent + Renderer + Notifier

## Task 18: Meta-Agent Synthesizer

**Files:**
- Create: `src/investment_agent/meta/__init__.py`
- Create: `src/investment_agent/meta/synthesizer.py`
- Create: `tests/unit/test_meta_synthesizer.py`

```python
"""Meta-Agent: pure-algorithmic synthesis of consensus / divergent / overlooked picks."""

from collections import defaultdict
from statistics import mean

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from investment_agent.storage.models.analysis import (
    DiscoveryReport, StockScore,
)


async def synthesize_discovery_report(
    session: AsyncSession,
    run_id: int,
    *,
    consensus_min_score: int = 70,
    consensus_max_spread: int = 20,
    divergent_min_spread: int = 40,
    overlooked_champion_score: int = 80,
    overlooked_other_max: int = 50,
) -> DiscoveryReport:
    """Algorithmic categorization. No LLM. Writes one DiscoveryReport row."""

    # 1. Pull all scores for this run, group by listing_id
    stmt = select(StockScore).where(StockScore.pipeline_run_id == run_id)
    rows = list((await session.execute(stmt)).scalars().all())

    by_listing: dict[int, list[StockScore]] = defaultdict(list)
    for r in rows:
        by_listing[r.listing_id].append(r)

    consensus = []
    divergent = []
    overlooked = []

    for listing_id, listing_scores in by_listing.items():
        scores = [s.score for s in listing_scores]
        avg = mean(scores)
        spread = max(scores) - min(scores)
        supporting = [s for s in listing_scores if s.score >= consensus_min_score]
        opposing = [s for s in listing_scores if s.score <= 40]

        # Consensus
        if len(supporting) >= 2 and avg >= consensus_min_score and spread < consensus_max_spread:
            consensus.append({
                "listing_id": listing_id,
                "avg_score": round(avg, 1),
                "supporting": [s.strategy_code for s in supporting],
            })
            continue

        # Divergent
        if spread >= divergent_min_spread and supporting and opposing:
            divergent.append({
                "listing_id": listing_id,
                "spread": spread,
                "bull": [s.strategy_code for s in supporting],
                "bear": [s.strategy_code for s in opposing],
            })
            continue

        # Overlooked
        if len(supporting) == 1 and supporting[0].score >= overlooked_champion_score:
            others = [s for s in listing_scores if s.strategy_code != supporting[0].strategy_code]
            if all(s.score < overlooked_other_max for s in others):
                overlooked.append({
                    "listing_id": listing_id,
                    "champion": supporting[0].strategy_code,
                    "champion_score": supporting[0].score,
                })

    # Sort and limit
    consensus.sort(key=lambda x: x["avg_score"], reverse=True)
    divergent.sort(key=lambda x: x["spread"], reverse=True)
    overlooked.sort(key=lambda x: x["champion_score"], reverse=True)

    report = DiscoveryReport(
        pipeline_run_id=run_id,
        consensus_picks=consensus[:10],
        divergent_picks=divergent[:10],
        overlooked_picks=overlooked[:10],
    )
    # Upsert
    existing = await session.get(DiscoveryReport, run_id)
    if existing is None:
        session.add(report)
    else:
        existing.consensus_picks = report.consensus_picks
        existing.divergent_picks = report.divergent_picks
        existing.overlooked_picks = report.overlooked_picks
    await session.flush()
    return report
```

Tests cover all branches: consensus / divergent / overlooked detection with crafted score sets.

Commit: `feat: Meta-Agent synthesizer (consensus/divergent/overlooked, pure algorithmic)`

---

## Task 19: Markdown Renderer

**Files:**
- Create: `src/investment_agent/renderers/__init__.py`
- Create: `src/investment_agent/renderers/markdown_renderer.py`
- Create: `tests/unit/test_markdown_renderer.py`

```python
"""Markdown renderer for Discovery + Attribution reports."""

from datetime import date

from investment_agent.domain.protocols import RenderedContent


class MarkdownRenderer:
    async def render_discovery(
        self, run_id: int,
        consensus_picks: list[dict],
        divergent_picks: list[dict],
        overlooked_picks: list[dict],
        scores_by_listing: dict[int, list[dict]],
    ) -> RenderedContent:
        body = f"# Discovery Report — Run {run_id}\n\n"
        body += f"_Generated: {date.today().isoformat()}_\n\n"

        body += "## 共识股 (Consensus)\n\n"
        if consensus_picks:
            body += "| Ticker | Avg Score | Supporting Strategies |\n|---|---|---|\n"
            for p in consensus_picks:
                body += f"| {p.get('ticker', p['listing_id'])} | {p['avg_score']} | {', '.join(p['supporting'])} |\n"
        else:
            body += "_No consensus picks today._\n"
        body += "\n"

        body += "## 强分歧股 (Divergent)\n\n"
        # ... similar structure
        body += "\n## 被忽略的优质股 (Overlooked)\n\n"
        # ... similar structure

        body += "\n---\n\n## 详细评分（按策略）\n\n"
        for lid, score_dicts in scores_by_listing.items():
            body += f"### Listing {lid}\n"
            for sd in score_dicts:
                body += f"- **{sd['strategy_code']}**: {sd['score']}/100 (conf {sd['confidence']:.2f})\n"
                body += f"  - Bull: {sd.get('bull_thesis', '')[:200]}\n"
                body += f"  - Bear: {sd.get('bear_thesis', '')[:200]}\n"
            body += "\n"

        return RenderedContent(format="markdown", body=body, metadata={"run_id": run_id})

    async def render_attribution(self, run_id: int, payload: dict) -> RenderedContent:
        # Placeholder for Plan C
        return RenderedContent(format="markdown", body="# Attribution\n\n_TBD in Plan C_\n")
```

Tests cover: empty pick lists, populated pick lists, ticker rendering.

Commit: `feat: Markdown renderer for Discovery reports`

---

## Task 20: File Notifier

**Files:**
- Create: `src/investment_agent/notifiers/__init__.py`
- Create: `src/investment_agent/notifiers/file_notifier.py`
- Create: `tests/unit/test_file_notifier.py`

```python
"""File notifier — writes RenderedContent to data/reports/YYYY-MM-DD/."""

from datetime import date
from pathlib import Path

from investment_agent.domain.protocols import NotificationResult, RenderedContent


class FileNotifier:
    name = "file"

    def __init__(self, base_dir: Path) -> None:
        self._base_dir = Path(base_dir)

    async def send(
        self, content: RenderedContent, channel_metadata: dict
    ) -> NotificationResult:
        date_str = channel_metadata.get("date") or date.today().isoformat()
        report_kind = channel_metadata.get("kind", "report")
        run_id = channel_metadata.get("run_id", "unknown")

        day_dir = self._base_dir / date_str
        day_dir.mkdir(parents=True, exist_ok=True)

        ext = "md" if content.format == "markdown" else content.format
        out_path = day_dir / f"{report_kind}_{run_id}.{ext}"
        out_path.write_text(content.body)

        return NotificationResult(
            notifier_name=self.name, success=True, target=str(out_path),
        )
```

Tests: writes file, creates day directory, handles different formats.

Commit: `feat: FileNotifier writes RenderedContent to data/reports/YYYY-MM-DD/`

---

## Task 21: Pipeline Step 7 — Hit-Rate Review（简化对账）

在 `steps.py` 追加 `step7_hit_rate_review`：

```python
async def step7_hit_rate_review(
    session: AsyncSession,
    *,
    pipeline_run_id: int,
    lookback_days: int = 7,
) -> dict:
    """Compare yesterday's top picks (from prior PipelineRun) against today's price action.

    Returns metrics dict like {"hit_rate": 0.6, "n_picks": 10, "n_outperformed": 6}.
    Writes summary into pipeline_run.metadata_json.
    """
    # 1. Find previous Discovery run for same universe
    # 2. Pull its top consensus_picks (say top 5)
    # 3. For each, compute today's return vs SPY/QQQ return
    # 4. Compute hit_rate = % of picks that outperformed benchmark
    pass
```

Tests: with crafted history data, verify hit_rate computation.

Commit: `feat: Discovery Pipeline Step 7 (hit-rate review against benchmark)`

---

## Task 22: 集成 Smoke Test — 完整 Discovery Pipeline 端到端

**File:**
- Modify: `tests/integration/test_discovery_pipeline.py` (扩展)

```python
@pytest.mark.asyncio
async def test_full_discovery_pipeline_e2e(db_session, seeded_market, monkeypatch):
    """Full pipeline with fake DataSource + fake StrategyAgents → DB + markdown file."""
    # 1. Setup: universe + 3 listings, sync strategies, ensure prompt files exist
    # 2. Build pipeline with fake DataSource (deterministic) + fake strategies (stub agents that return fixed StockScore objects)
    # 3. Run pipeline.run()
    # 4. Assert:
    #    - PipelineRun status = 'success'
    #    - StockScore rows exist for each (strategy × listing)
    #    - DiscoveryReport row exists with consensus/divergent/overlooked
    #    - data/reports/<today>/discovery_<run_id>.md exists and non-empty
    pass
```

Final verification:

```bash
source .venv/bin/activate
pytest tests/ -q  # all green (estimate 50+ tests)
ruff check src/investment_agent
mypy src/investment_agent  # 0 errors
```

Commit: `test: full Discovery Pipeline end-to-end smoke test`

---

# Plan B 完成总结

完成后系统具备：

✅ **3 个具体策略**（value / growth / risk_aggressive）配置 + prompts 完整就位  
✅ **完整 Discovery Pipeline** 端到端运行，含数据准备 / Tier1 算法 / Tier2 LLM / Tier3 深度 / Meta-Agent / 渲染 / Hit-rate 对账  
✅ **Auxiliary Tracking** 派生 Job + CLI 管理  
✅ **公司关系图** 由 Tier 3 输出 LLM 提取并自动入库  
✅ **Markdown 日报** 自动落到 `data/reports/YYYY-MM-DD/`  
✅ **CLI `ia pipeline discover` / `ia strategy ...` / `ia auxiliary ...`** 全部可用

**手动验收**（Plan B 全部完成后跑）：

```bash
source .venv/bin/activate

# 1. 同步策略到 DB
ia strategy sync
ia strategy list   # 应该看到 3 个策略

# 2. 跑完整 Discovery（需 .env 里有真实 LLM key）
ia pipeline discover --universe NASDAQ_100
# 预计耗时：8-15 分钟（首次拉数据较慢；二次复用 cache 后 3-6 分钟）

# 3. 查看日报
ls data/reports/$(date +%Y-%m-%d)/
cat data/reports/$(date +%Y-%m-%d)/discovery_*.md

# 4. 检查关系图自动生成
sqlite3 data/app.db "SELECT * FROM company_relationship LIMIT 10"

# 5. 触发 Auxiliary 派生
ia auxiliary refresh
ia auxiliary list

# 6. 月度成本
ia admin cost
```

如以上 6 步都顺利通过，Plan B 完整交付，进入 Plan C（Attribution Pipeline）。
