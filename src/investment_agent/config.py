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
    model_config = SettingsConfigDict(env_file=".env", env_nested_delimiter="__", extra="ignore")

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

    # A-share research MVP data gateway.
    ifind_proxy_url: str = "http://127.0.0.1:7100"
    ifind_proxy_api_key: str = ""

    # Browser web session for the same-origin /app UI.
    web_session_ttl_minutes: int = 720

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
    watchlist_refresh_cron: str | None = None
    watchlist_refresh_timezone: str = "Asia/Shanghai"
    a_share_calendar_path: str = "config/calendars/a_share.yaml"


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
