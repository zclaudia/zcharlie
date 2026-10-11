"""Application configuration: env-based Settings + YAML config."""

import logging
import re
from pathlib import Path
from typing import Literal, Self
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

_log = logging.getLogger(__name__)

PROD_MIN_API_KEY_LENGTH = 32
_PLACEHOLDER_KEY_PREFIX = "change-me"
_weak_key_warned = False


ThinkingMode = Literal["auto", "enabled", "disabled"]


class LLMTierConfig(BaseModel):
    base_url: str
    api_key: str
    model: str
    timeout_seconds: float = 120.0
    thinking: ThinkingMode = "auto"
    """Reasoning toggle sent as ``thinking: {type: ...}`` (DeepSeek-style
    OpenAI-compatible extension). ``auto`` sends nothing, so endpoints that do
    not know the parameter are unaffected."""


def validate_gateway_url(value: str) -> str:
    """A zclaudia-gateway base URL, or ``""`` (unset)."""

    value = value.strip()
    if not value:
        return value
    parts = urlsplit(value)
    if parts.scheme not in ("http", "https", "ws", "wss") or not parts.hostname:
        raise ValueError("gateway URL must start with http(s):// or ws(s):// and include a host")
    return value.rstrip("/")


_GATEWAY_ID = re.compile(r"[A-Za-z0-9._-]*")


def validate_gateway_identifier(value: str) -> str:
    """A gateway namespace / instance id: letters, digits, ``.``, ``_``, ``-``."""

    value = value.strip()
    if not _GATEWAY_ID.fullmatch(value):
        raise ValueError("use only letters, digits, '.', '_' and '-'")
    return value


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore")

    app_env: Literal["dev", "prod"] = "dev"
    log_level: str = "INFO"
    data_dir: Path = Path("./data")

    api_key: str

    database_url: str = "sqlite+aiosqlite:///./data/app.db"

    # Tier 3: research explanation model (any OpenAI-compatible endpoint).
    llm_tier3_base_url: str = "https://api.openai.com/v1"
    # Runtime setting: once stored in the DB (settings UI / ``ia settings``)
    # it may be dropped from env; an empty key fails at the first LLM call.
    llm_tier3_api_key: str = ""
    llm_tier3_model: str = "gpt-5"
    llm_tier3_thinking: ThinkingMode = "auto"
    # Rule drafting picks parameters within template ranges and is validated
    # afterwards; reasoning tokens dominate its latency. ``auto`` = tier default.
    llm_rule_draft_thinking: ThinkingMode = "auto"

    llm_request_timeout_seconds: float = Field(default=120.0, gt=0)

    # A-share research MVP data gateway.
    ifind_proxy_url: str = "http://127.0.0.1:7100"
    ifind_proxy_api_key: str = ""

    # Snapshot collection (P1-A3). The worker process throttles every proxy
    # call of its snapshot collectors through one shared token bucket.
    ifind_collect_rate_per_minute: float = Field(default=40.0, gt=0, le=600)
    # Extra attempts per batch for transient proxy failures (0 = no retry).
    ifind_collect_batch_retries: int = Field(default=2, ge=0, le=10)
    # AkShare / Eastmoney financial collector (P1-A11): calls are serialized and
    # spaced at least this many seconds apart.
    akshare_min_interval_seconds: float = Field(default=1.0, ge=0, le=60)
    # Below this completeness ratio a market/financial run raises an
    # ``action_required`` notification.
    snapshot_completeness_threshold: float = Field(default=0.95, ge=0, le=1)
    # iFinD verification budget (P1-A13): at most this many ifind-proxy calls
    # (every source counted by ``ifind_call_ledger``) in a rolling window of
    # ``ifind_budget_window_hours``; verification jobs defer beyond it.
    ifind_budget_per_5h: int = Field(default=150, ge=1, le=10000)
    ifind_budget_window_hours: float = Field(default=5.0, gt=0, le=48)
    # Requests one ``ifind_verify`` run picks at most (the rest wait for the
    # next dispatch slot).
    ifind_verify_max_requests: int = Field(default=30, ge=1, le=500)

    # Candidates (P1-D1): an open candidate expires after this many days; a
    # dismissed listing is not re-proposed by the screen within the cooldown.
    candidate_expiry_days: int = Field(default=14, ge=1, le=365)
    candidate_dismiss_cooldown_days: int = Field(default=30, ge=0, le=365)

    # Mobile gateway (zclaudia-gateway, Protocol v4): the API registers itself as
    # a Backend so a phone can reach it through the gateway's /api/proxy.
    # ``gateway_credential`` is the enrollment token (zgb_...) issued by the
    # gateway; ``gateway_namespace`` must match the one it was issued for.
    gateway_enabled: bool = False
    gateway_url: str = ""
    gateway_credential: str = Field(default="", repr=False)
    gateway_namespace: str = Field(default="", max_length=64)
    # Stable per installation: with the namespace it fixes the backend's identity
    # on the gateway (a changed id is a different backend to the phone).
    gateway_instance_id: str = Field(default="zcharlie", min_length=1, max_length=64)

    @field_validator("gateway_url")
    @classmethod
    def _valid_gateway_url(cls, value: str) -> str:
        return validate_gateway_url(value)

    @field_validator("gateway_namespace", "gateway_instance_id")
    @classmethod
    def _valid_gateway_identifier(cls, value: str) -> str:
        return validate_gateway_identifier(value)

    # Browser web session for the same-origin /app UI.
    web_session_ttl_minutes: int = 720
    # Dedicated session-signing secret; empty falls back to deriving from API_KEY.
    web_session_secret: str = ""
    # Set True whenever the UI is served over HTTPS (reverse proxy with TLS).
    web_cookie_secure: bool = False
    # Public origin (e.g. "https://research.example.com") accepted by the CSRF
    # origin check when a reverse proxy rewrites the Host header.
    web_public_origin: str = ""

    # Fernet master key for the secret runtime settings stored in the DB
    # (``investment_agent.settings``). Deliberately separate from API_KEY /
    # WEB_SESSION_SECRET (rotating those must not lose stored secrets) and
    # kept out of DATA_DIR (same volume as the DB and its backups).
    settings_encryption_key: str = ""

    @model_validator(mode="after")
    def _validate_api_key(self) -> Self:
        global _weak_key_warned
        key = self.api_key
        if not key or not key.strip():
            raise ValueError("API_KEY must not be empty")
        weak = len(key) < PROD_MIN_API_KEY_LENGTH or key.startswith(_PLACEHOLDER_KEY_PREFIX)
        if weak and self.app_env == "prod":
            raise ValueError(
                f"API_KEY is too weak for APP_ENV=prod: use at least "
                f"{PROD_MIN_API_KEY_LENGTH} random characters and not a "
                f"'{_PLACEHOLDER_KEY_PREFIX}' placeholder "
                '(python -c "import secrets;print(secrets.token_urlsafe(32))")'
            )
        if weak and not _weak_key_warned:
            _weak_key_warned = True
            _log.warning(
                "API_KEY is weak (short or placeholder); acceptable for dev only, "
                "APP_ENV=prod will refuse to start with it"
            )
        return self

    @property
    def llm_tier3(self) -> LLMTierConfig:
        return LLMTierConfig(
            base_url=self.llm_tier3_base_url,
            api_key=self.llm_tier3_api_key,
            model=self.llm_tier3_model,
            timeout_seconds=self.llm_request_timeout_seconds,
            thinking=self.llm_tier3_thinking,
        )

    @property
    def llm_rule_draft(self) -> LLMTierConfig:
        """Tier-3 config for rule drafting (its own thinking override)."""

        config = self.llm_tier3
        if self.llm_rule_draft_thinking == "auto":
            return config
        return config.model_copy(update={"thinking": self.llm_rule_draft_thinking})


class SchedulerConfig(BaseModel):
    watchlist_refresh_cron: str | None = None
    watchlist_refresh_timezone: str = "Asia/Shanghai"
    a_share_calendar_path: str = "config/calendars/a_share.yaml"


class SnapshotScheduleConfig(BaseModel):
    """PIT snapshot collection schedule (cron fields in ``timezone``).

    A ``None`` cron disables that job. Gates (trading day, first trading day
    of the month, first trading day after a disclosure deadline) are applied
    when a job fires, using the scheduler's trading calendar.
    """

    universes: list[str] = Field(default_factory=lambda: ["CSI300"], min_length=1)
    timezone: str = "Asia/Shanghai"
    universe_sync_cron: str | None = "0 8 * * 1"
    industry_sync_cron: str | None = "0 8 * * *"
    # baostock publishes the day's bars in the evening; 18:30 is a margin over
    # the observed availability (re-verify on a normal trading day).
    market_snapshot_cron: str | None = "30 18 * * 1-5"
    # Tencent same-day close (mrd-intake B2) right after the 15:00 close, and
    # a retry for listings still pending (feed lag, late quotes) at 16:00.
    close_quote_cron: str | None = "10 15 * * 1-5"
    close_quote_retry_cron: str | None = "0 16 * * 1-5"
    financial_weekly_cron: str | None = "0 9 * * 6"
    # Also runs the overdue-report scan; evening, so deadline-day reports
    # have reached baostock.
    financial_deadline_cron: str | None = "0 19 * * *"
    # Eastmoney earnings events (mrd-intake C2): gated to trading days inside
    # the disclosure-season months (January / April / July / October).
    earnings_event_cron: str | None = "0 19 * * 1-5"
    # Dual-source check of the next unreviewed calendar year (ops "年度 /
    # 不定期维护"). Weekly; the job only enqueues inside the calendar's
    # 60-day expiry window, so it is a no-op for most of the year.
    calendar_probe_cron: str | None = "0 8 * * 1"

    @model_validator(mode="after")
    def _known_universes(self) -> Self:
        from investment_agent.providers.baostock_reference import INDEX_CODES

        unknown = sorted(set(self.universes) - set(INDEX_CODES))
        if unknown:
            raise ValueError(f"unsupported snapshot universes {unknown}; use {INDEX_CODES}")
        return self

    def crons(self) -> dict[str, str | None]:
        return {
            "universe_sync": self.universe_sync_cron,
            "industry_sync": self.industry_sync_cron,
            "market_snapshot": self.market_snapshot_cron,
            "close_quote": self.close_quote_cron,
            "close_quote_retry": self.close_quote_retry_cron,
            "financial_weekly": self.financial_weekly_cron,
            "financial_deadline": self.financial_deadline_cron,
            "earnings_event": self.earnings_event_cron,
        }


class ScreenScheduleConfig(BaseModel):
    """Weekly template screen (P1-C3): one ``screen_run`` per universe.

    ``as_of`` is the local date the job fires on; a ``None`` cron disables it.
    """

    universes: list[str] = Field(default_factory=lambda: ["CSI300"], min_length=1)
    timezone: str = "Asia/Shanghai"
    # Day-of-week by name: APScheduler 3 numbers weekdays from Monday = 0.
    cron: str | None = "0 20 * * sun"
    hit_cap: int = Field(default=20, ge=1, le=500)

    @model_validator(mode="after")
    def _known_universes(self) -> Self:
        from investment_agent.providers.baostock_reference import INDEX_CODES

        unknown = sorted(set(self.universes) - set(INDEX_CODES))
        if unknown:
            raise ValueError(f"unsupported screen universes {unknown}; use {INDEX_CODES}")
        return self


class CandidateScheduleConfig(BaseModel):
    """Daily candidate expiry sweep (P1-D1); a ``None`` cron disables it.

    The sweep also runs at the start of every ``screen_run``.
    """

    timezone: str = "Asia/Shanghai"
    expiry_sweep_cron: str | None = "0 7 * * *"


class VerificationScheduleConfig(BaseModel):
    """iFinD verification schedule (P1-A13); standard crontab fields in ``timezone``.

    ``dispatch_cron`` enqueues one ``ifind_verify`` run per slot when requests
    are due; ``calibration_cron`` queues ``calibration`` requests for
    ``calibration_count`` watchlist stocks sampled per ISO week. ``None``
    disables a job.
    """

    timezone: str = "Asia/Shanghai"
    dispatch_cron: str | None = "*/30 * * * *"
    calibration_cron: str | None = "0 21 * * 0"
    calibration_count: int = Field(default=10, ge=0, le=200)


class AppYamlConfig(BaseModel):
    scheduler: SchedulerConfig
    snapshot: SnapshotScheduleConfig | None = None
    screen: ScreenScheduleConfig | None = None
    candidates: CandidateScheduleConfig | None = None
    verification: VerificationScheduleConfig | None = None


def load_app_yaml(path: Path) -> AppYamlConfig:
    with path.open() as f:
        raw = yaml.safe_load(f)
    return AppYamlConfig.model_validate(raw)
