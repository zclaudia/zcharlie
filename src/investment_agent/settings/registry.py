"""Explicit registry of the runtime settings (settings-plan S-D5, §3.2).

Runtime settings live in the DB (``app_setting``) and are edited in the Web
UI; the bootstrap layer (``APP_ENV``, ``DATABASE_URL``, ``API_KEY``,
``SETTINGS_ENCRYPTION_KEY`` ...) stays in env and is not listed here.

Key naming scheme (flat, used by the DB rows, the API, the CLI and history):

* env-sourced keys keep the ``Settings`` field name unchanged, e.g.
  ``ifind_budget_per_5h`` / ``llm_catalog``;
* schedule keys are dotted ``schedule.<section>.<field>``, mirroring
  ``config/app.yaml``: ``schedule.screen.cron``, ``schedule.snapshot.universes``,
  ``schedule.verification.calibration_count``. The one rename is the
  ``scheduler`` section's watchlist refresh pair, which becomes
  ``schedule.watchlist.cron`` / ``schedule.watchlist.timezone``
  (``SettingSpec.yaml_path`` records the app.yaml location of every key).

A dotted key is also the attribute path into ``RuntimeSettings``
(``runtime.schedule.screen.cron``). ``a_share_calendar_path`` stays in
app.yaml: it is a deployment resource path, not a business parameter.

``RuntimeSettings`` repeats the ``Settings`` field constraints (a test keeps
the two in sync) and composes the existing app.yaml schedule models, adding
timezone and cron checks. Crons are parsed the way the scheduler parses them
(standard crontab weekdays, 0 / 7 = Sunday) in the section's timezone.
"""

from dataclasses import dataclass
from typing import Any, Literal, get_args, get_origin
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import annotated_types
from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator
from pydantic.fields import FieldInfo

from investment_agent.config import (
    CandidateScheduleConfig,
    ScreenScheduleConfig,
    SnapshotScheduleConfig,
    VerificationScheduleConfig,
    validate_gateway_identifier,
    validate_gateway_url,
)

ApplyMode = Literal["live", "next_job", "reschedule"]
"""``live`` = the next read sees it; ``next_job`` = the worker's next job;
``reschedule`` = the scheduler rebuilds its jobs."""
SettingSource = Literal["env", "app_yaml"]

GROUPS: dict[str, str] = {
    "agent": "投资 Agent",
    "notifications": "可选外部通知",
    "llm": "模型",
    "ifind": "iFinD 代理",
    "collection": "数据采集",
    "verification": "iFinD 核验",
    "candidates": "候选池",
    "peers": "行业与同行参照",
    "materials": "研究材料来源",
    "gateway": "手机网关",
    "schedule": "调度",
}


@dataclass(frozen=True)
class SettingSpec:
    key: str
    group: str
    label: str
    help: str
    apply: ApplyMode
    source: SettingSource
    secret: bool = False
    yaml_path: tuple[str, str] | None = None
    """``(section, field)`` in config/app.yaml for ``source == "app_yaml"``."""

    @property
    def path(self) -> tuple[str, ...]:
        """Attribute path into ``RuntimeSettings``."""

        return tuple(self.key.split("."))


def _check_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError(f"unknown timezone '{value}' (use an IANA name, e.g. Asia/Shanghai)") from exc
    return value


def _check_cron(value: str | None, info: ValidationInfo) -> str | None:
    if value is None:
        return None
    # The scheduler's own parser: it translates crontab weekday numbers
    # (0 = Sunday) that APScheduler would read from Monday = 0.
    from investment_agent.scheduler.runner import _parse_cron

    timezone = info.data.get("timezone")
    try:
        _parse_cron(value, timezone=timezone)
    except (ValueError, TypeError) as exc:
        raise ValueError(f"invalid cron '{value}': {exc}") from exc
    return value


def _check_universes(value: list[str]) -> list[str]:
    from investment_agent.providers.baostock_reference import INDEX_CODES

    unknown = sorted(set(value) - set(INDEX_CODES))
    if unknown:
        raise ValueError(f"unsupported universes {unknown}; use {INDEX_CODES}")
    return value


class CronSchedule(BaseModel):
    """A daily ``(timezone, cron)`` scheduler section.

    Serves the three app.yaml blocks that are field-for-field identical:
    ``scheduler.watchlist_refresh_*``, ``scheduler.portfolio_valuation_*`` and
    ``scheduler.zone_detection_*``.
    """

    model_config = ConfigDict(extra="forbid")

    timezone: str = "Asia/Shanghai"
    cron: str | None = None

    _timezone = field_validator("timezone")(_check_timezone)
    _cron = field_validator("cron")(_check_cron)


# Compatibility aliases: the per-section names stay importable/annotatable.
WatchlistSchedule = CronSchedule
PortfolioValuationSchedule = CronSchedule
ZoneDetectionSchedule = CronSchedule
ReviewBatchSchedule = CronSchedule


class SnapshotSchedule(SnapshotScheduleConfig):
    model_config = ConfigDict(extra="forbid")

    _universes = field_validator("universes")(_check_universes)
    _timezone = field_validator("timezone")(_check_timezone)
    _crons = field_validator(
        "universe_sync_cron",
        "industry_sync_cron",
        "market_snapshot_cron",
        "close_quote_cron",
        "close_quote_retry_cron",
        "financial_weekly_cron",
        "financial_deadline_cron",
        "earnings_event_cron",
        "calendar_probe_cron",
    )(_check_cron)


class ScreenSchedule(ScreenScheduleConfig):
    model_config = ConfigDict(extra="forbid")

    _universes = field_validator("universes")(_check_universes)
    _timezone = field_validator("timezone")(_check_timezone)
    _cron = field_validator("cron")(_check_cron)


class CandidateSchedule(CandidateScheduleConfig):
    model_config = ConfigDict(extra="forbid")

    _timezone = field_validator("timezone")(_check_timezone)
    _cron = field_validator("expiry_sweep_cron")(_check_cron)


class VerificationSchedule(VerificationScheduleConfig):
    model_config = ConfigDict(extra="forbid")

    _timezone = field_validator("timezone")(_check_timezone)
    _crons = field_validator("dispatch_cron", "calibration_cron")(_check_cron)


class ScheduleSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    watchlist: WatchlistSchedule = Field(default_factory=WatchlistSchedule)
    portfolio: PortfolioValuationSchedule = Field(default_factory=PortfolioValuationSchedule)
    zone_detection: ZoneDetectionSchedule = Field(default_factory=ZoneDetectionSchedule)
    review_batch: ReviewBatchSchedule = Field(default_factory=ReviewBatchSchedule)
    snapshot: SnapshotSchedule = Field(default_factory=SnapshotSchedule)
    screen: ScreenSchedule = Field(default_factory=ScreenSchedule)
    candidates: CandidateSchedule = Field(default_factory=CandidateSchedule)
    verification: VerificationSchedule = Field(default_factory=VerificationSchedule)


class RuntimeSettings(BaseModel):
    """The full runtime layer; defaults and bounds mirror ``Settings``."""

    model_config = ConfigDict(extra="forbid")

    # Independent Agent settings; empty endpoint/model/key is intentionally unconfigured.
    # Investor execution controls are separate from immutable model parameters.
    investor_agent_task_tool_calls: int = Field(default=50, gt=0)
    investor_agent_daily_tool_calls: int = Field(default=200, gt=0)
    disclosure_originals_enabled: bool = False
    materials_sec_refresh_enabled: bool = False
    materials_hk_refresh_enabled: bool = False
    portfolio_ledger_enabled: bool = False
    account_fit_enabled: bool = False
    candidate_ranking_enabled: bool = False
    supplemental_rules_enabled: bool = False
    supplemental_updates_enabled: bool = False
    supplemental_disclosure_polling_enabled: bool = False
    portfolio_valuation_max_staleness_days: int = Field(default=5, ge=0, le=30)
    investor_agent_enabled: bool = False
    investor_agent_background_enabled: bool = False
    research_quality_enabled: bool = False
    research_quality_execution_enabled: bool = False
    agent_queue_max_running: int = Field(default=4, ge=1, le=4)
    agent_queue_interactive_capacity: int = Field(default=128, ge=1, le=128)
    agent_queue_review_capacity: int = Field(default=96, ge=1, le=96)
    agent_queue_exploration_capacity: int = Field(default=32, ge=1, le=32)
    @field_validator("notification_webhook_url")
    @classmethod
    def valid_webhook_url(cls, value: str) -> str:
        if value:
            from investment_agent.investor.notification_http import validate_endpoint
            validate_endpoint(value)
        return value

    notification_external_enabled: bool = False
    notification_webhook_url: str = Field(default="", repr=False)
    notification_webhook_token: str = Field(default="", repr=False)
    notification_webhook_idempotent: bool = False
    investor_agent_max_leads_per_batch: int = Field(default=20, ge=0, le=20)
    investor_agent_max_new_tasks_per_batch: int = Field(default=3, ge=0, le=3)
    investor_agent_task_requests: int = Field(default=20, gt=0)
    investor_agent_task_tokens: int = Field(default=100000, gt=0)
    investor_agent_daily_requests: int = Field(default=100, gt=0)
    investor_agent_daily_tokens: int = Field(default=500000, gt=0)
    investor_agent_task_seconds: float = Field(default=600.0, gt=0)

    llm_catalog: str = Field(default="", repr=False)
    model_slots: str = ""

    # Multi-perspective review. Seats are a variable-length list inside
    # ``review_panels`` rather than fixed model slots.
    review_panel_enabled: bool = False
    review_panel_auto_enabled: bool = False
    review_panel_schedule_enabled: bool = False
    review_panel_seat_requests: int = Field(default=8, gt=0, le=100)
    review_panel_seat_tool_calls: int = Field(default=12, gt=0, le=200)
    review_panel_seat_tokens: int = Field(default=60000, gt=0, le=2_000_000)
    review_panel_daily_rounds: int = Field(default=20, ge=0, le=200)
    review_panel_daily_tokens: int = Field(default=1_500_000, gt=0, le=100_000_000)
    review_panel_batch_max_rounds: int = Field(default=10, ge=0, le=50)
    review_panel_auto_cooldown_trading_days: int = Field(default=3, ge=0, le=30)
    review_panel_max_running_seats: int = Field(default=2, ge=1, le=24)
    review_panels: str = ""

    @field_validator("review_panels")
    @classmethod
    def valid_review_panels(cls, value: str) -> str:
        from investment_agent.review_panel.panels import (
            ReviewPanelError,
            validate_review_panels,
        )

        try:
            return validate_review_panels(value)
        except ReviewPanelError as exc:
            raise ValueError(str(exc)) from None

    @field_validator("llm_catalog")
    @classmethod
    def valid_llm_catalog(cls, value: str) -> str:
        from investment_agent.domain.llm_catalog import validate_catalog
        return validate_catalog(value)

    @field_validator("model_slots")
    @classmethod
    def valid_model_slots(cls, value: str) -> str:
        from investment_agent.settings.agents import SlotError, validate_model_slots
        try:
            return validate_model_slots(value)
        except SlotError as exc:
            raise ValueError(str(exc)) from None

    ifind_proxy_url: str = "http://127.0.0.1:7100"
    ifind_proxy_api_key: str = ""

    ifind_collect_rate_per_minute: float = Field(default=40.0, gt=0, le=600)
    ifind_collect_batch_retries: int = Field(default=2, ge=0, le=10)
    akshare_min_interval_seconds: float = Field(default=1.0, ge=0, le=60)
    snapshot_completeness_threshold: float = Field(default=0.95, ge=0, le=1)

    ifind_budget_per_5h: int = Field(default=150, ge=1, le=10000)
    ifind_budget_window_hours: float = Field(default=5.0, gt=0, le=48)
    ifind_verify_max_requests: int = Field(default=30, ge=1, le=500)

    candidate_expiry_days: int = Field(default=14, ge=1, le=365)
    candidate_dismiss_cooldown_days: int = Field(default=30, ge=0, le=365)

    # Candidate-package prospective shadows (method packages card 19): the
    # constraints mirror ``Settings``; a registry test keeps them in sync.
    method_candidate_shadow_min_positives: int = Field(default=1, ge=1)
    method_candidate_shadow_max_per_proposal: int = Field(default=8, ge=1)

    # Industry & peer context (WP1): defaults and bounds mirror ``Settings``.
    industry_peer_context_enabled: bool = False
    peer_collect_min_interval_seconds: float = Field(default=1.0, ge=0, le=60)
    peer_collect_retries: int = Field(default=2, ge=0, le=10)
    peer_collect_admission_cooldown_seconds: float = Field(
        default=86400.0, ge=0, le=30 * 86400
    )

    gateway_enabled: bool = False
    gateway_url: str = ""
    gateway_credential: str = Field(default="", repr=False)
    gateway_namespace: str = Field(default="", max_length=64)
    gateway_instance_id: str = Field(default="zcharlie", min_length=1, max_length=64)

    @field_validator("gateway_url")
    @classmethod
    def _valid_gateway_url(cls, value: str) -> str:
        return validate_gateway_url(value)

    @field_validator("gateway_namespace", "gateway_instance_id")
    @classmethod
    def _valid_gateway_identifier(cls, value: str) -> str:
        return validate_gateway_identifier(value)

    schedule: ScheduleSettings = Field(default_factory=ScheduleSettings)


def _env(
    key: str, group: str, label: str, help: str, apply: ApplyMode, *, secret: bool = False
) -> SettingSpec:
    return SettingSpec(key, group, label, help, apply, "env", secret=secret)


def _yaml(key: str, label: str, help: str, yaml_path: tuple[str, str]) -> SettingSpec:
    return SettingSpec(key, "schedule", label, help, "reschedule", "app_yaml", yaml_path=yaml_path)


_CRON_HELP = "标准 5 段 crontab（minute hour day month weekday），留空 = 停用；星期建议写 sun / mon，例如 30 19 * * 1-5。"

SPECS: tuple[SettingSpec, ...] = (
    _env("agent_queue_max_running", "agent", "Agent 最大并发", "本机持久队列共享上限 1–4；单 worker 仍依次执行。", "next_job"),
    _env("agent_queue_interactive_capacity", "agent", "交互队列容量", "1–128；满时拒绝新任务，既有任务不重建。", "next_job"),
    _env("agent_queue_review_capacity", "agent", "复核队列容量", "1–96；后台账号按最近服务公平轮转。", "next_job"),
    _env("agent_queue_exploration_capacity", "agent", "探索队列容量", "1–32；探索最多一项同时运行。", "next_job"),
    _env("investor_agent_task_tool_calls", "agent", "单任务工具调用上限", "独立执行额度。", "next_job"),
    _env("investor_agent_daily_tool_calls", "agent", "每日工具调用上限", "独立执行额度。", "next_job"),
    _env("disclosure_originals_enabled", "agent", "启用公告原件取证", "默认关闭；仅显式授权深圳CNInfo有界发现/获取，关闭仍可读历史。每次执行边界复查。", "next_job"),
    _env("portfolio_ledger_enabled", "agent", "启用真实账本录入", "A1b事实录入默认关闭；不执行交易。", "next_job"),
    _env("account_fit_enabled", "agent", "启用账户适配检查", "默认关闭；需同时启用真实账本入口，关闭时仍可读取历史结果。", "next_job"),
    _env("candidate_ranking_enabled", "agent", "启用本账户候选排序", "默认关闭；还需账户适配和真实账本入口，关闭时仍可读取历史快照。", "next_job"),
    _env("supplemental_rules_enabled", "agent", "启用补充事项评估", "默认关闭；控制类别、公司及个股追加规则的独立管理与评估，关闭后运行中的评估停止新增应用。", "live"),
    _env("supplemental_updates_enabled", "agent", "启用补充事项本地更新", "默认关闭；允许持久本地来源变化触发复核调度，不开启联网采集。", "live"),
    _env("supplemental_disclosure_polling_enabled", "agent", "启用补充公告定时采集", "默认关闭；仅在显式来源授权和公告原件开关均满足时允许受限轮询。", "live"),
    _env(
        "portfolio_valuation_max_staleness_days", "agent", "估值价格时效（交易日）",
        "收盘价超过该交易日数视为过期，不参与市值与 NAV；未知年份按受限原因处理。",
        "next_job",
    ),
    _env("investor_agent_enabled", "agent", "启用投资 Agent", "投资 Agent 执行控制；每个边界重新检查。", "next_job"),
    _env("investor_agent_background_enabled", "agent", "启用后台研究", "Investor Agent 执行控制；每个边界重新检查。", "next_job"),
    _env("research_quality_enabled", "agent", "启用研究质量对照", "默认关闭；控制新建与新写入，关闭后授权历史和操作收据仍可读取。", "live"),
    _env("research_quality_execution_enabled", "agent", "允许研究对照执行", "默认关闭；执行还须通过原任务授权和预算，关闭后停止新发送，保留机械结算。", "live"),
    _env("investor_agent_max_leads_per_batch", "agent", "每批探索线索上限", "最多20；仍受当前投资约定限制。", "next_job"),
    _env("investor_agent_max_new_tasks_per_batch", "agent", "每批新研究任务上限", "最多3；不是产出目标。", "next_job"),
    _env("investor_agent_task_requests", "agent", "单任务请求上限", "Investor Agent 执行控制；每个边界重新检查。", "next_job"),
    _env("investor_agent_task_tokens", "agent", "单任务 token 上限", "Investor Agent 执行控制；每个边界重新检查。", "next_job"),
    _env("investor_agent_daily_requests", "agent", "每日请求上限", "Investor Agent 执行控制；每个边界重新检查。", "next_job"),
    _env("investor_agent_daily_tokens", "agent", "每日 token 上限", "Investor Agent 执行控制；每个边界重新检查。", "next_job"),
    _env("investor_agent_task_seconds", "agent", "单任务时间上限（秒）", "Investor Agent 执行控制；每个边界重新检查。", "next_job"),
    _env("model_slots", "agent", "模型槽位", "各使用者选择的模型引用（provider/model）。模型缺失或停用时显示为不可用，不会改用其他模型。", "next_job"),
    _env("review_panels", "agent", "评审组配置", "JSON：默认评审组与各评审组的席位（视角 + provider/model）；为空表示尚未配置。", "next_job"),
    _env("review_panel_enabled", "agent", "启用多视角评审", "默认关闭；关闭时不创建新轮次，历史轮次仍可查看。", "next_job"),
    _env("review_panel_auto_enabled", "agent", "启用数据变化自动评审", "默认关闭；需总开关同时打开，并受同股冷却与每日额度约束。", "next_job"),
    _env("review_panel_schedule_enabled", "agent", "启用定期批量评审", "默认关闭；需总开关同时打开，按交易日历每周批量运行。", "next_job"),
    _env("review_panel_seat_requests", "agent", "单席请求上限", "每个席位一次运行最多发出的模型请求数，超过后保留已有输出并标记为部分完成。", "next_job"),
    _env("review_panel_seat_tool_calls", "agent", "单席工具调用上限", "每个席位一次运行最多调用的只读工具次数。", "next_job"),
    _env("review_panel_seat_tokens", "agent", "单席 token 上限", "每个席位一次运行的 token 上限；价格未知的模型照常执行该上限。", "next_job"),
    _env("review_panel_daily_rounds", "agent", "每日轮次上限", "每天最多创建的评审轮次数，0 表示不限制；额度按整轮预留。", "next_job"),
    _env("review_panel_daily_tokens", "agent", "每日 token 上限", "每天评审可预留的 token 总量，创建轮次前整轮检查和预留。", "next_job"),
    _env("review_panel_batch_max_rounds", "agent", "每批最多轮次", "定期批量单次最多创建的轮次数，0 表示不运行批量。", "next_job"),
    _env("review_panel_auto_cooldown_trading_days", "agent", "自动触发同股冷却（交易日）", "数据变化触发的同一股票在多少个交易日内不再自动创建新轮次。", "next_job"),
    _env("review_panel_max_running_seats", "agent", "同时运行席位数上限", "限制评审席位的并发数，避免批量评审挤占交互任务。", "next_job"),
    _env(
        "method_candidate_shadow_min_positives", "agent", "候选影子最小正例数",
        "包提案批准前需要的匹配成功正例前瞻影子数，≥1；只影响证据收集成本，不降低覆盖门槛。", "live",
    ),
    _env(
        "method_candidate_shadow_max_per_proposal", "agent", "候选影子每提案上限",
        "每个包提案允许发起的候选影子任务总数，≥1；达到上限后拒绝新的发起（含批量）。", "live",
    ),
    _env("notification_external_enabled", "notifications", "我启用外部通知", "默认关闭。仅从此刻的新变化发送状态及引用，不发送私有提示词或原文。", "next_job"),
    _env("notification_webhook_url", "notifications", "HTTPS webhook 地址", "用户指定的公网 HTTPS 地址；加密保存，拒绝重定向、私网及环境导入。", "next_job", secret=True),
    _env("notification_webhook_token", "notifications", "Webhook Bearer 凭证", "加密保存；为空则不发送认证头。", "next_job", secret=True),
    _env("notification_webhook_idempotent", "notifications", "接收端保证幂等", "只有接收端保证 Idempotency-Key 去重时启用；否则确认未知后停止，不自动重试。", "next_job"),
    _env("llm_catalog", "llm", "Provider 与模型目录", "多个 Provider 及其模型。每个模型有稳定引用 provider/model，并标明能力标签。这里不记录谁在使用它。凭证加密保存，只写不读。", "next_job", secret=True),
    _env("ifind_proxy_url", "ifind", "代理地址", "iFinD 数据代理的 URL。", "next_job"),
    _env(
        "ifind_proxy_api_key", "ifind", "代理 API Key", "加密保存，只写不读。", "next_job",
        secret=True,
    ),
    _env(
        "ifind_collect_rate_per_minute", "collection", "iFinD 采集速率（次/分钟）",
        "worker 内所有快照采集共享的令牌桶速率，范围 (0, 600]。", "next_job",
    ),
    _env(
        "ifind_collect_batch_retries", "collection", "批次重试次数",
        "代理临时故障时每批额外重试次数，0–10，0 = 不重试。", "next_job",
    ),
    _env(
        "akshare_min_interval_seconds", "collection", "AkShare 最小调用间隔（秒）",
        "东方财富财务采集串行调用的最小间隔，0–60。", "next_job",
    ),
    _env(
        "snapshot_completeness_threshold", "collection", "快照完整度阈值",
        "行情 / 财务采集完整度低于该比例（0–1）时发出需处理通知。", "next_job",
    ),
    _env(
        "ifind_budget_per_5h", "verification", "iFinD 调用预算",
        "滚动窗口内 iFinD 代理调用总次数上限，1–10000；超出后核验任务顺延。", "next_job",
    ),
    _env(
        "ifind_budget_window_hours", "verification", "预算窗口（小时）",
        "调用预算的滚动窗口长度，范围 (0, 48]。", "next_job",
    ),
    _env(
        "ifind_verify_max_requests", "verification", "单次核验最多请求数",
        "一次 ifind_verify 最多处理的核验请求，1–500，其余等下一个调度槽。", "next_job",
    ),
    _env(
        "candidate_expiry_days", "candidates", "候选过期天数",
        "未处理的候选在多少天后自动过期，1–365。", "live",
    ),
    _env(
        "candidate_dismiss_cooldown_days", "candidates", "忽略冷却天数",
        "被忽略的股票在多少天内不再被筛选重新提名，0–365。", "live",
    ),
    _env(
        "industry_peer_context_enabled", "peers", "启用行业与同行参照",
        "默认关闭；打开后只暴露离线已导入的名单与结果，各边界（页面 / API / Agent）自行检查。", "live",
    ),
    _env(
        "peer_collect_min_interval_seconds", "peers", "同行采集最小间隔（秒）",
        "年度参照采集串行调用的最小间隔，0–60；具体频率随来源条件在 WP7 落地。", "next_job",
    ),
    _env(
        "peer_collect_retries", "peers", "同行采集重试次数",
        "暂态错误每家公司的额外重试次数，0–10，0 = 不重试；429 遵从 Retry-After。", "next_job",
    ),
    _env(
        "peer_collect_admission_cooldown_seconds", "peers", "同行补采准入冷却（秒）",
        "上次核查后该窗口内的重复缺口只返回原缺口不再入队；确认新年报、已知更正或显式重试不受限，0–2592000。",
        "next_job",
    ),
    _env("materials_sec_refresh_enabled", "materials", "启用 SEC 材料刷新", "运维开关；来源仍须分别通过许可与技术验收，并受全局保险约束。", "live"),
    _env("materials_hk_refresh_enabled", "materials", "启用港股材料刷新", "运维开关；来源仍须分别通过许可与技术验收，并受全局保险约束。", "live"),
    _env(
        "gateway_enabled", "gateway", "启用手机网关",
        "让本服务作为 Backend 接入 zclaudia-gateway，手机经网关访问；保存后约 10 秒内自动连接 / 断开。",
        "live",
    ),
    _env(
        "gateway_url", "gateway", "网关地址",
        "zclaudia-gateway 的公网地址，如 https://gateway.example.com。", "live",
    ),
    _env(
        "gateway_credential", "gateway", "注册凭证",
        "网关签发的 Backend 凭证（zgb_ 开头），加密保存，只写不读。", "live", secret=True,
    ),
    _env(
        "gateway_namespace", "gateway", "命名空间",
        "必须与凭证签发时的 namespace 一致；手机端只能看到同一命名空间的服务。", "live",
    ),
    _env(
        "gateway_instance_id", "gateway", "实例标识",
        "新版网关的服务身份由注册凭证决定，此字段仅作展示；旧式未绑定凭证才用它与命名空间一起固定身份，须保持稳定。", "live",
    ),
    _yaml(
        "schedule.watchlist.cron", "观察池刷新 cron", _CRON_HELP,
        ("scheduler", "watchlist_refresh_cron"),
    ),
    _yaml(
        "schedule.review_batch.cron", "多视角评审批量 cron", _CRON_HELP,
        ("scheduler", "review_batch_cron"),
    ),
    _yaml(
        "schedule.review_batch.timezone", "多视角评审批量时区",
        "IANA 时区名，如 Asia/Shanghai。", ("scheduler", "review_batch_timezone"),
    ),
    _yaml(
        "schedule.watchlist.timezone", "观察池刷新时区", "IANA 时区名，如 Asia/Shanghai。",
        ("scheduler", "watchlist_refresh_timezone"),
    ),
    _yaml(
        "schedule.portfolio.cron", "持仓估值 cron", _CRON_HELP,
        ("scheduler", "portfolio_valuation_cron"),
    ),
    _yaml(
        "schedule.portfolio.timezone", "持仓估值时区", "IANA 时区名，如 Asia/Shanghai。",
        ("scheduler", "portfolio_valuation_timezone"),
    ),
    _yaml(
        "schedule.zone_detection.cron", "区间检测 cron", _CRON_HELP,
        ("scheduler", "zone_detection_cron"),
    ),
    _yaml(
        "schedule.zone_detection.timezone", "区间检测时区", "IANA 时区名，如 Asia/Shanghai。",
        ("scheduler", "zone_detection_timezone"),
    ),
    _yaml(
        "schedule.snapshot.universes", "快照股票池", "采集的指数成分股池，如 CSI300。",
        ("snapshot", "universes"),
    ),
    _yaml("schedule.snapshot.timezone", "快照时区", "以下快照 cron 的时区。", ("snapshot", "timezone")),
    _yaml(
        "schedule.snapshot.universe_sync_cron", "成分股同步 cron", _CRON_HELP,
        ("snapshot", "universe_sync_cron"),
    ),
    _yaml(
        "schedule.snapshot.industry_sync_cron", "行业同步 cron",
        _CRON_HELP + "仅在每月首个交易日执行。", ("snapshot", "industry_sync_cron"),
    ),
    _yaml(
        "schedule.snapshot.market_snapshot_cron", "行情快照 cron",
        _CRON_HELP + "仅在交易日执行。", ("snapshot", "market_snapshot_cron"),
    ),
    _yaml(
        "schedule.snapshot.close_quote_cron", "收盘快照 cron",
        _CRON_HELP + "仅在交易日执行；腾讯批量报价写入当日收盘价（15:00 后）。",
        ("snapshot", "close_quote_cron"),
    ),
    _yaml(
        "schedule.snapshot.close_quote_retry_cron", "收盘快照补采 cron",
        _CRON_HELP + "仅在交易日执行；补采 15:10 仍未收盘 / 未报价的标的。",
        ("snapshot", "close_quote_retry_cron"),
    ),
    _yaml(
        "schedule.snapshot.financial_weekly_cron", "财务周采 cron", _CRON_HELP,
        ("snapshot", "financial_weekly_cron"),
    ),
    _yaml(
        "schedule.snapshot.financial_deadline_cron", "披露截止后财务采集 cron",
        _CRON_HELP + "仅在披露截止日后的首个交易日执行，并检查逾期报告。",
        ("snapshot", "financial_deadline_cron"),
    ),
    _yaml(
        "schedule.snapshot.earnings_event_cron", "财报事件采集 cron",
        _CRON_HELP + "仅在 1 / 4 / 7 / 10 月的交易日执行；东方财富业绩预告与预约披露时间每日、主营构成每周。",
        ("snapshot", "earnings_event_cron"),
    ),
    _yaml(
        "schedule.snapshot.calendar_probe_cron", "交易日历核对 cron",
        _CRON_HELP + "仅在日历到期前 60 天内执行：双源核对下一年，一致时生成候选文件并通知。",
        ("snapshot", "calendar_probe_cron"),
    ),
    _yaml(
        "schedule.screen.universes", "筛选股票池", "每周模板筛选的指数成分股池。",
        ("screen", "universes"),
    ),
    _yaml("schedule.screen.timezone", "筛选时区", "筛选 cron 的时区。", ("screen", "timezone")),
    _yaml("schedule.screen.cron", "每周筛选 cron", _CRON_HELP, ("screen", "cron")),
    _yaml(
        "schedule.screen.hit_cap", "筛选命中上限", "每次筛选最多保留的命中数，1–500。",
        ("screen", "hit_cap"),
    ),
    _yaml(
        "schedule.candidates.timezone", "候选过期扫描时区", "过期扫描 cron 的时区。",
        ("candidates", "timezone"),
    ),
    _yaml(
        "schedule.candidates.expiry_sweep_cron", "候选过期扫描 cron", _CRON_HELP,
        ("candidates", "expiry_sweep_cron"),
    ),
    _yaml(
        "schedule.verification.timezone", "核验时区", "核验 cron 的时区。",
        ("verification", "timezone"),
    ),
    _yaml(
        "schedule.verification.dispatch_cron", "核验派发 cron",
        _CRON_HELP + "有到期请求时派发一次核验任务。", ("verification", "dispatch_cron"),
    ),
    _yaml(
        "schedule.verification.calibration_cron", "每周校准 cron", _CRON_HELP,
        ("verification", "calibration_cron"),
    ),
    _yaml(
        "schedule.verification.calibration_count", "校准抽样数",
        "每周抽样校准的观察池股票数，0–200。", ("verification", "calibration_count"),
    ),
)

SPECS_BY_KEY: dict[str, SettingSpec] = {spec.key: spec for spec in SPECS}
SECRET_KEYS: frozenset[str] = frozenset(spec.key for spec in SPECS if spec.secret)
ENV_KEYS: tuple[str, ...] = tuple(spec.key for spec in SPECS if spec.source == "env")


def flatten(runtime: RuntimeSettings) -> dict[str, Any]:
    """``{key: JSON-compatible value}`` for every registered key."""

    dumped = runtime.model_dump(mode="json")
    flat: dict[str, Any] = {}
    for spec in SPECS:
        node: Any = dumped
        for part in spec.path:
            node = node[part]
        flat[spec.key] = node
    return flat


def unflatten(flat: dict[str, Any]) -> dict[str, Any]:
    """Nested ``RuntimeSettings`` input from ``{key: value}`` (registered keys only)."""

    nested: dict[str, Any] = {}
    for key, value in flat.items():
        *parents, leaf = SPECS_BY_KEY[key].path
        node = nested
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return nested


def defaults() -> dict[str, Any]:
    return flatten(RuntimeSettings())


def _field_info(spec: SettingSpec) -> FieldInfo:
    model: type[BaseModel] = RuntimeSettings
    *parents, leaf = spec.path
    for part in parents:
        annotation = model.model_fields[part].annotation
        assert isinstance(annotation, type) and issubclass(annotation, BaseModel)
        model = annotation
    return model.model_fields[leaf]


def input_spec(spec: SettingSpec) -> dict[str, Any]:
    """How a UI should edit ``spec``: input type, bounds, options, nullability.

    ``type`` is one of ``secret`` / ``select`` / ``multi`` / ``int`` /
    ``float`` / ``cron`` / ``timezone`` / ``url`` / ``text``. Bounds mirror the
    pydantic constraints (``min`` / ``max`` inclusive unless ``*_exclusive``).
    """

    info = _field_info(spec)
    annotation = info.annotation
    args = get_args(annotation)
    nullable = type(None) in args
    if nullable:
        annotation = next(arg for arg in args if arg is not type(None))
    meta: dict[str, Any] = {"nullable": nullable}
    if spec.secret:
        meta["type"] = "secret"
    elif get_origin(annotation) is Literal:
        meta.update(type="select", options=list(get_args(annotation)))
    elif get_origin(annotation) is list:
        from investment_agent.providers.baostock_reference import INDEX_CODES

        meta.update(type="multi", options=list(INDEX_CODES))
        for item in info.metadata:
            if isinstance(item, annotated_types.MinLen):
                meta["min_items"] = item.min_length
    elif annotation in (int, float):
        meta["type"] = "int" if annotation is int else "float"
        for item in info.metadata:
            if isinstance(item, annotated_types.Gt):
                meta.update(min=item.gt, min_exclusive=True)
            elif isinstance(item, annotated_types.Ge):
                meta.update(min=item.ge, min_exclusive=False)
            elif isinstance(item, annotated_types.Lt):
                meta.update(max=item.lt, max_exclusive=True)
            elif isinstance(item, annotated_types.Le):
                meta.update(max=item.le, max_exclusive=False)
    elif spec.key.endswith("cron"):
        meta["type"] = "cron"
    elif spec.key.endswith("timezone"):
        meta["type"] = "timezone"
    elif spec.key.endswith("_url"):
        meta["type"] = "url"
    else:
        meta["type"] = "text"
    return meta


def cron_timezone_key(spec: SettingSpec) -> str | None:
    """The timezone key of a schedule cron's section (``None`` for other keys)."""

    if spec.yaml_path is None or not spec.key.endswith("cron"):
        return None
    section = spec.path[1]
    return f"schedule.{section}.timezone"
