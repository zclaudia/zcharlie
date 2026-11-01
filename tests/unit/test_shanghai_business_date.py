"""Business dates follow Asia/Shanghai, including while the host clock is UTC."""

import asyncio
from datetime import date
from pathlib import Path
from types import SimpleNamespace

from freezegun import freeze_time
from typer.testing import CliRunner

from investment_agent.clock import shanghai_today
from investment_agent.config import Settings
from investment_agent.domain.protocols import RenderedContent
from investment_agent.domain.security import ControlledIdentity, SecurityType, SourceBinding
from investment_agent.materials.refresh import _refresh_batch
from investment_agent.materials.source_policy import evaluate_source_policy
from investment_agent.notifiers.file_notifier import FileNotifier
from investment_agent.scheduler.calendar_refresh import refresh_years
from investment_agent.scheduler.trading_calendar import TradingCalendar, TradingCalendarSource

FROZEN = "2026-11-01 16:30:00+00:00"
SHANGHAI_DAY = date(2026, 11, 2)
SOURCE = "eastmoney.hk.native.v1"


def _identity() -> ControlledIdentity:
    return ControlledIdentity(
        market_code="HKEX",
        ticker="00005",
        company_key="hsbc-holdings",
        company_name="HSBC Holdings plc",
        company_name_zh="汇丰控股",
        security_type=SecurityType.ORDINARY,
        quote_currency="HKD",
        report_currency="USD",
        fiscal_year_end_note="December",
        sources=(SourceBinding(source=SOURCE, symbol="00005.HK"),),
        valid_from=date(2020, 1, 1),
    )


def _approvals(*observed: date) -> dict[str, object]:
    return {
        SOURCE: {
            "licensing": {
                "status": "approved",
                "company_keys": ["hsbc-holdings"],
                "citation": "synthetic:policy/licensing",
            },
            "technical_acceptance": {
                "status": "accepted",
                "company_keys": ["hsbc-holdings"],
                "adapter_version": "adapter-1",
                "reports": [
                    {"observed_on": day.isoformat(), "citation": f"synthetic:report/{index}"}
                    for index, day in enumerate(observed)
                ],
            },
        }
    }


def _evaluate(approvals: dict[str, object]):
    return evaluate_source_policy(
        SOURCE,
        _identity(),
        settings=Settings(
            _env_file=None, api_key="ci-inert-api-key", materials_hk_refresh_enabled=True
        ),
        adapter_version="adapter-1",
        global_enabled=True,
        approvals=approvals,
    )


def test_utc_evening_is_the_next_shanghai_business_date(tmp_path: Path, monkeypatch) -> None:
    # Import before freezing: freezegun's FakeDatetime breaks Pydantic models.
    from investment_agent.cli.commands import research as research_cmd
    from investment_agent.research import ifind_chain
    from investment_agent.settings import loader as settings_loader
    from investment_agent.storage import database

    with freeze_time(FROZEN):
        assert shanghai_today() == SHANGHAI_DAY

        accepted = _evaluate(_approvals(date(2026, 10, 31), date(2026, 11, 1), SHANGHAI_DAY))
        future = _evaluate(_approvals(date(2026, 10, 31), date(2026, 11, 1), date(2026, 11, 3)))
        assert "technical_report_dates_invalid_or_insufficient" not in accepted.reason_codes
        assert "technical_report_dates_invalid_or_insufficient" in future.reason_codes

        assert _refresh_batch((), {}, {}).startswith(f"{SHANGHAI_DAY.isoformat()}|")

        notifier = FileNotifier(tmp_path)
        result = asyncio.run(
            notifier.send(
                RenderedContent(format="markdown", body="body", metadata={}),
                {"kind": "research_change", "run_id": 1},
            )
        )
        assert result.target == str(tmp_path / SHANGHAI_DAY.isoformat() / "research_change_1.md")

        captured: dict[str, date] = {}

        async def chain(**kwargs):
            captured["as_of"] = kwargs["as_of"]
            return SimpleNamespace(
                run_id=1,
                score={"business_score": 1, "suggestion": "watch"},
                artifact_dir=tmp_path,
            )

        async def load_settings(session=None, base=None):
            return base

        def run_with_engine(_url, main):
            return asyncio.run(main())

        monkeypatch.setattr(
            research_cmd,
            "bootstrap_settings",
            lambda: Settings(
                _env_file=None, api_key="ci-inert-api-key", ifind_proxy_api_key="proxy"
            ),
        )
        monkeypatch.setattr(settings_loader, "load_settings", load_settings)
        monkeypatch.setattr(database, "run_with_engine", run_with_engine)
        monkeypatch.setattr(ifind_chain, "run_ifind_research_chain", chain)
        runner = CliRunner()
        snapshot = runner.invoke(research_cmd.app, ["snapshot", "600519.SH"])
        assert snapshot.exit_code == 0, snapshot.output
        assert captured["as_of"] == SHANGHAI_DAY
        ifind = runner.invoke(
            research_cmd.app,
            ["ifind", "600519.SH", "--industry", "白酒", "--report-year", "2025"],
        )
        assert ifind.exit_code == 0, ifind.output
        assert captured["as_of"] == SHANGHAI_DAY

    empty = TradingCalendar(
        calendar_id="a_share",
        timezone="Asia/Shanghai",
        source=TradingCalendarSource(
            title="fixture", url="https://example.invalid", published_on=date(2026, 1, 1)
        ),
        years=[],
    )
    with freeze_time("2026-12-31 16:30:00+00:00"):
        assert refresh_years(empty) == [2027]
