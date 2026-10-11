"""Explicit registry of the runtime settings (settings-plan S-D5, §3.2).

Runtime settings live in the DB (``app_setting``) and are edited in the Web
UI; the bootstrap layer (``APP_ENV``, ``DATABASE_URL``, ``API_KEY``,
``SETTINGS_ENCRYPTION_KEY`` ...) stays in env and is not listed here.

Key naming scheme (flat, used by the DB rows, the API, the CLI and history):

* env-sourced keys keep the ``Settings`` field name unchanged, e.g.
  ``ifind_budget_per_5h`` / ``llm_tier3_api_key``;
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
    ThinkingMode,
    VerificationScheduleConfig,
    validate_gateway_identifier,
    validate_gateway_url,
)

ApplyMode = Literal["live", "next_job", "reschedule"]
"""``live`` = the next read sees it; ``next_job`` = the worker's next job;
``reschedule`` = the scheduler rebuilds its jobs."""
SettingSource = Literal["env", "app_yaml"]

GROUPS: dict[str, str] = {
    "llm": "大模型",
    "ifind": "iFinD 代理",
    "collection": "数据采集",
    "verification": "iFinD 核验",
    "candidates": "候选池",
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


class WatchlistSchedule(BaseModel):
    """Daily watchlist refresh (app.yaml ``scheduler.watchlist_refresh_*``)."""

    model_config = ConfigDict(extra="forbid")

    timezone: str = "Asia/Shanghai"
    cron: str | None = None

    _timezone = field_validator("timezone")(_check_timezone)
    _cron = field_validator("cron")(_check_cron)


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
    snapshot: SnapshotSchedule = Field(default_factory=SnapshotSchedule)
    screen: ScreenSchedule = Field(default_factory=ScreenSchedule)
    candidates: CandidateSchedule = Field(default_factory=CandidateSchedule)
    verification: VerificationSchedule = Field(default_factory=VerificationSchedule)


class RuntimeSettings(BaseModel):
    """The full runtime layer; defaults and bounds mirror ``Settings``."""

    model_config = ConfigDict(extra="forbid")

    llm_tier3_base_url: str = "https://api.openai.com/v1"
    llm_tier3_model: str = "gpt-5"
    llm_tier3_api_key: str = ""
    llm_tier3_thinking: ThinkingMode = "auto"
    llm_rule_draft_thinking: ThinkingMode = "auto"
    llm_request_timeout_seconds: float = Field(default=120.0, gt=0)

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


_CRON_HELP = "标准 5 段 crontab（分 时 日 月 周），留空 = 停用；星期建议写 sun / mon 名称。"

SPECS: tuple[SettingSpec, ...] = (
    _env("llm_tier3_base_url", "llm", "接口地址", "OpenAI 兼容接口的 base URL。", "next_job"),
    _env("llm_tier3_model", "llm", "模型", "研究解释与规则草拟使用的模型名。", "next_job"),
    _env("llm_tier3_api_key", "llm", "API Key", "加密保存，只写不读。", "next_job", secret=True),
    _env(
        "llm_tier3_thinking", "llm", "推理模式",
        "auto = 不发送 thinking 参数；enabled / disabled 显式开关推理。", "next_job",
    ),
    _env(
        "llm_rule_draft_thinking", "llm", "规则草拟推理模式",
        "规则草拟单独的推理开关；auto = 沿用上一项。", "next_job",
    ),
    _env(
        "llm_request_timeout_seconds", "llm", "请求超时（秒）",
        "单次大模型请求的超时时间，须大于 0。", "next_job",
    ),
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
        "与命名空间一起决定本服务在网关上的固定身份；更换后手机需要重新发现。", "live",
    ),
    _yaml(
        "schedule.watchlist.cron", "观察池刷新 cron", _CRON_HELP,
        ("scheduler", "watchlist_refresh_cron"),
    ),
    _yaml(
        "schedule.watchlist.timezone", "观察池刷新时区", "IANA 时区名，如 Asia/Shanghai。",
        ("scheduler", "watchlist_refresh_timezone"),
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
        meta["type"] = annotation.__name__
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
