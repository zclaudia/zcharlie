from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from investment_agent.domain.security import (
    DEFAULT_PILOT_LIST_PATH,
    SOURCE_EASTMONEY_HK,
    SOURCE_SEC_COMPANYFACTS,
    load_pilot_identities,
)
from investment_agent.materials import source_probe
from investment_agent.materials.archive import verify_capture
from investment_agent.materials.contracts import (
    FactPeriod,
    PeriodBasis,
    RawPayload,
    UpstreamRequest,
)
from investment_agent.materials.handlers import (
    HttpxMaterialFetcher,
    _MaterialDataError,
    _MaterialTransientError,
)
from investment_agent.materials.source_policy import evaluate_source_policy
from tests.support.material_source_policy import synthetic_approved_source_policy

GOOD_UA = "ZCharlie research operations (contact probe@zcharlie.org)"


def _one_hk_identity(tmp_path: Path) -> tuple[Path, Any]:
    value = yaml.safe_load(DEFAULT_PILOT_LIST_PATH.read_text(encoding="utf-8"))
    row = next(
        item
        for item in value["identities"]
        if any(binding["source"] == SOURCE_EASTMONEY_HK for binding in item["sources"])
    )
    value["identities"] = [row]
    pilot_path = tmp_path / "pilot.yaml"
    pilot_path.write_text(yaml.safe_dump(value, allow_unicode=True, sort_keys=False), encoding="utf-8")
    identity = next(
        item
        for item in load_pilot_identities(pilot_path)
        if any(binding.source == SOURCE_EASTMONEY_HK for binding in item.sources)
    )
    return pilot_path, identity


def _approval(identity: Any) -> dict[str, object]:
    from investment_agent.materials.hk_native import ADAPTER_VERSION

    approval = synthetic_approved_source_policy(SOURCE_EASTMONEY_HK, identity, ADAPTER_VERSION)
    record = approval[SOURCE_EASTMONEY_HK]
    assert isinstance(record, dict)
    return {
        SOURCE_EASTMONEY_HK: {
            "licensing": record["licensing"],
            "technical_acceptance": {"status": "not_accepted", "company_keys": [], "reports": []},
        }
    }


def _all_hk_approvals(identities: list[Any]) -> dict[str, object]:
    approval = _approval(identities[0])
    record = approval[SOURCE_EASTMONEY_HK]
    assert isinstance(record, dict)
    licensing = record["licensing"]
    assert isinstance(licensing, dict)
    licensing["company_keys"] = [identity.company_key for identity in identities]
    return approval


def _hk_periods() -> tuple[FactPeriod, ...]:
    from datetime import date

    return tuple(
        FactPeriod(
            start=date(year, 1, 1),
            end=date(year, 12, 31),
            basis=PeriodBasis.ANNUAL,
        )
        for year in range(2021, 2025)
    )


def _annual_rows(identity: Any, name: str) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for year in range(2021, 2025):
        end = f"{year}-12-31"
        common: dict[str, object] = {
            "STD_REPORT_DATE": end,
            "START_DATE": f"{year}-01-01",
            "CURRENCY_CODE": identity.report_currency,
            "CURRENCY_ABBR": identity.report_currency,
            "ACCOUNT_STANDARD": "国际会计准则",
            "REPORT_TYPE": "年报",
            "FISCAL_YEAR": str(year),
        }
        if name == "income":
            rows.append(
                {
                    **common,
                    "STD_ITEM_CODE": "004001999",
                    "ITEM_NAME": "营运收入",
                    "AMOUNT": year * 100,
                }
            )
        elif name == "balance":
            rows.append(
                {
                    **common,
                    "STD_ITEM_CODE": "004009999",
                    "ITEM_NAME": "资产总计",
                    "AMOUNT": year * 1000,
                }
            )
        else:
            rows.append(
                {
                    "REPORT_DATE": end,
                    "VALUE_LIST": [
                        {
                            **common,
                            "STD_ITEM_CODE": "003999",
                            "ITEM_NAME": "经营活动现金流量净额",
                            "AMOUNT": year * 10,
                        }
                    ],
                }
            )
    return rows


def _response(identity: Any, request: UpstreamRequest, *, include_unmapped: bool = False) -> bytes:
    data = _annual_rows(identity, request.name)
    if include_unmapped and request.name == "income":
        data.append(
            {
                "STD_REPORT_DATE": "2024-12-31",
                "START_DATE": "2024-01-01",
                "CURRENCY_CODE": identity.report_currency,
                "ACCOUNT_STANDARD": "国际会计准则",
                "REPORT_TYPE": "年报",
                "FISCAL_YEAR": "2024",
                "STD_ITEM_CODE": "999999999",
                "ITEM_NAME": "unmapped test line",
                "AMOUNT": 123,
            }
        )
    body = {
        "success": True,
        "version": "synthetic-response-1",
        "result": {"data": data, "count": len(data), "pages": 1},
    }
    return json.dumps(body, ensure_ascii=False).encode("utf-8")


class _FakeFetcher:
    def __init__(
        self,
        user_agent: str,
        identity: Any,
        statuses: dict[str, list[int]] | None = None,
        *,
        after_wait: Callable[[], None] | None = None,
        include_unmapped: bool = False,
    ) -> None:
        self.user_agent = user_agent
        self.identity = identity
        self.before_send = None
        self.statuses = statuses or {}
        self.calls: list[str] = []
        self.after_wait = after_wait
        self.include_unmapped = include_unmapped

    async def fetch(self, request: UpstreamRequest, *, identity: Any) -> RawPayload:
        assert self.before_send is not None
        await asyncio.sleep(0)
        if self.after_wait is not None:
            self.after_wait()
        await self.before_send(request)
        self.calls.append(request.name)
        status_script = self.statuses.get(request.name, [])
        status = status_script.pop(0) if status_script else 200
        raw = RawPayload(
            name=request.name,
            content=_response(identity, request, include_unmapped=self.include_unmapped),
            media_type="application/json",
            fetched_at=datetime.now(UTC),
            source_url=request.url,
            http_status=status,
        )
        if status in {408, 429} or status >= 500:
            raise _MaterialTransientError(
                f"{request.name}: upstream HTTP {status}",
                provider=SOURCE_EASTMONEY_HK,
                error_code=f"http_{status}",
                raw_payload=raw,
                parse_eligible=False,
            )
        if status >= 400:
            raise _MaterialDataError(
                f"{request.name}: upstream HTTP {status}",
                provider=SOURCE_EASTMONEY_HK,
                error_code=f"http_{status}",
                raw_payload=raw,
                parse_eligible=False,
            )
        return raw


def _dependencies(tmp_path: Path, identity: Any, fetcher: _FakeFetcher) -> source_probe.ProbeDependencies:
    return source_probe.ProbeDependencies(
        approvals=_approval(identity),
        pilot_list_path=tmp_path / "pilot.yaml",
        fetcher_factory=lambda ua: fetcher,
    )


def test_default_is_zero_network_dry_run_with_controlled_scope(tmp_path: Path) -> None:
    work_dir = tmp_path / "dry-run"
    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=work_dir,
    )

    assert report["mode"] == "dry_run"
    assert report["status"] == "dry_run"
    assert report["requests_total"] == 0
    assert report["requests"] == []
    assert report["company_keys"] == []
    assert report["planned_company_keys"] == ["clp-holdings", "hsbc-holdings", "tencent-holdings"]
    assert report["adapter_version"] == "1"
    assert report["single_process_serial_exclusive_required"] is True
    assert report["periods"] == []
    assert "hk_annual_spans_required" in " ".join(report["issues"])
    assert any("user_agent_invalid" in issue for issue in report["issues"])
    assert any("probe_blocked:licensing_unknown" in issue for issue in report["issues"])
    assert not any(
        routine_reason in issue
        for issue in report["issues"]
        for routine_reason in (
            "operationally_disabled",
            "global_refresh_disabled",
            "technical_acceptance_missing_or_mismatched",
        )
    )
    for decision in report["source_decisions"].values():
        assert decision["operational_enabled"] is False
        assert "global_refresh_disabled" in decision["reason_codes"]
        assert "technical_acceptance_missing_or_mismatched" in decision["reason_codes"]
    assert json.loads((work_dir / "report.json").read_text(encoding="utf-8"))["mode"] == "dry_run"
    assert not list(work_dir.rglob("*.bin"))


def test_sec_preflight_uses_the_six_bound_identities_and_current_version(tmp_path: Path) -> None:
    report: Any = source_probe.run_source_probe(
        source=SOURCE_SEC_COMPANYFACTS,
        work_dir=tmp_path / "sec-dry-run",
        user_agent=GOOD_UA,
    )
    assert report["adapter_version"] == "1"
    assert report["status"] == "dry_run"
    assert report["requests_total"] == 0
    assert set(report["planned_company_keys"]) == {
        "apple",
        "costco",
        "jpmorgan-chase",
        "exxon-mobil",
        "tsmc",
        "hsbc-holdings",
    }
    assert all(item["bound"] is True for item in report["source_decisions"].values())
    assert report["periods"] == []


def test_hk_network_mode_without_explicit_window_is_blocked(tmp_path: Path) -> None:
    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=tmp_path / "hk-no-window",
        user_agent=GOOD_UA,
        allow_network=True,
    )
    assert report["mode"] == "real"
    assert report["status"] == "blocked"
    assert report["requests_total"] == 0
    assert any("hk_annual_spans_required" in item for item in report["issues"])


def test_hk_periods_require_four_complete_consecutive_years(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="exactly four"):
        source_probe.run_source_probe(
            source=SOURCE_EASTMONEY_HK,
            work_dir=tmp_path / "bad-count",
            user_agent=GOOD_UA,
            periods=_hk_periods()[:3],
        )
    malformed = list(_hk_periods())
    malformed[1] = FactPeriod(
        start=malformed[1].start,
        end=malformed[1].end,
        basis=PeriodBasis.INTERIM,
    )
    with pytest.raises(ValueError, match="four consecutive complete"):
        source_probe.run_source_probe(
            source=SOURCE_EASTMONEY_HK,
            work_dir=tmp_path / "bad-span",
            user_agent=GOOD_UA,
            periods=tuple(malformed),
        )


def test_unknown_licence_blocks_network_and_placeholder_ua_is_rejected(tmp_path: Path) -> None:
    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=tmp_path / "blocked",
        user_agent=GOOD_UA,
        allow_network=True,
        periods=_hk_periods(),
    )
    assert report["mode"] == "real"
    assert report["status"] == "blocked"
    assert report["requests_total"] == 0
    assert report["archives"] == []
    assert "licensing_unknown" in " ".join(report["issues"])

    with pytest.raises(ValueError, match="placeholder"):
        source_probe.validate_user_agent("ZCharlie research (contact in config)")
    with pytest.raises(ValueError, match="valid contact email"):
        source_probe.validate_user_agent("ZCharlie research team")
    with pytest.raises(ValueError, match="placeholder"):
        source_probe.validate_user_agent("ZCharlie research (probe@example.test)")
    with pytest.raises(ValueError, match="control characters"):
        source_probe.validate_user_agent("ZCharlie\nresearch (probe@zcharlie.org)")


def test_invalid_user_agent_blocks_even_with_simulation_injection(tmp_path: Path, monkeypatch: Any) -> None:
    _pilot_path, identity = _one_hk_identity(tmp_path)
    fetcher = _FakeFetcher(GOOD_UA, identity)

    async def forbidden_request(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network must not be called")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden_request)
    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=tmp_path / "bad-ua-real",
        user_agent="ZCharlie research team",
        allow_network=True,
        periods=_hk_periods(),
        dependencies=_dependencies(tmp_path, identity, fetcher),
    )
    assert report["status"] == "blocked"
    assert report["requests_total"] == 0
    assert fetcher.calls == []


def test_injected_dependencies_without_fetcher_never_fall_back_to_network(
    tmp_path: Path, monkeypatch: Any
) -> None:
    _pilot_path, identity = _one_hk_identity(tmp_path)

    async def forbidden_request(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError("network must not be called")

    monkeypatch.setattr(httpx.AsyncClient, "request", forbidden_request)
    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=tmp_path / "no-fetcher",
        user_agent=GOOD_UA,
        allow_network=True,
        periods=_hk_periods(),
        dependencies=source_probe.ProbeDependencies(
            approvals=_approval(identity),
            pilot_list_path=tmp_path / "pilot.yaml",
        ),
    )
    assert report["status"] == "blocked"
    assert report["mode"] == "simulation"
    assert report["requests_total"] == 0


def test_work_dir_parent_symlink_is_rejected(tmp_path: Path) -> None:
    real_parent = tmp_path / "real-parent"
    real_parent.mkdir()
    alias = tmp_path / "parent-alias"
    alias.symlink_to(real_parent, target_is_directory=True)
    with pytest.raises(ValueError, match="must not contain symlinks"):
        source_probe.run_source_probe(
            source=SOURCE_EASTMONEY_HK,
            work_dir=alias / "output",
            user_agent=GOOD_UA,
        )


def test_work_dir_must_be_new_and_report_does_not_overwrite(tmp_path: Path) -> None:
    existing = tmp_path / "already-there"
    existing.mkdir()
    with pytest.raises(FileExistsError):
        source_probe.run_source_probe(
            source=SOURCE_EASTMONEY_HK,
            work_dir=existing,
            user_agent=GOOD_UA,
        )

    output = tmp_path / "fresh"
    source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=output,
        user_agent=GOOD_UA,
    )
    with pytest.raises(FileExistsError):
        source_probe.run_source_probe(
            source=SOURCE_EASTMONEY_HK,
            work_dir=output,
            user_agent=GOOD_UA,
        )


def test_simulation_archives_every_response_and_keeps_canonical_facts(tmp_path: Path) -> None:
    pilot_path, identity = _one_hk_identity(tmp_path)
    fetcher = _FakeFetcher(GOOD_UA, identity)
    dependencies = _dependencies(tmp_path, identity, fetcher)
    output = tmp_path / "simulated"
    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=output,
        user_agent=GOOD_UA,
        allow_network=True,
        periods=_hk_periods(),
        dependencies=dependencies,
    )

    assert pilot_path.exists()
    assert report["mode"] == "simulation"
    assert report["status"] == "passed"
    assert report["company_keys"] == [identity.company_key]
    assert report["subject_namespace"] == "isolated_probe_memory"
    assert report["passed_scope"] == "single_probe_transport_and_parse_only"
    assert report["passed_implies_technical_or_licensing_acceptance"] is False
    assert report["source_decisions"][identity.company_key]["technical_acceptance"] == "not_accepted"
    assert report["operational_refresh_enabled"] is False
    assert report["global_refresh_enabled"] is False
    assert report["requests_total"] == report["requests_succeeded"] == 3
    assert report["requests_failed"] == 0
    assert report["informational"] == []
    assert len(report["archives"]) == 1
    assert len(report["parsed_facts"][0]["facts"]) == 12
    assert all("__attempt_1" in row["response_archive_name"] for row in report["requests"])

    archive_ref = report["archives"][0]
    capture = verify_capture(output, archive_ref["manifest_ref"], archive_ref["manifest_sha256"])
    assert capture.source == SOURCE_EASTMONEY_HK
    assert capture.identity["company_key"] == identity.company_key
    assert len(capture.responses) == 3
    assert {response.name for response in capture.responses} == {
        "income__attempt_1",
        "balance__attempt_1",
        "cashflow__attempt_1",
    }
    manifest_responses = {response.name: response for response in capture.responses}
    for row in report["requests"]:
        archived_response = manifest_responses[row["response_archive_name"]]
        assert archived_response.http_status == row["http_status"]
        assert hashlib.sha256(archived_response.content).hexdigest() == row["response_sha256"]
    assert all(row["status"] == "http_2xx" for row in report["requests"])


def test_one_fetcher_and_one_host_limiter_are_reused_across_hk_companies(tmp_path: Path) -> None:
    identities = [
        item
        for item in load_pilot_identities(DEFAULT_PILOT_LIST_PATH)
        if any(binding.source == SOURCE_EASTMONEY_HK for binding in item.sources)
    ]
    fetcher = _FakeFetcher(GOOD_UA, identities[0])
    factory_calls = 0

    def factory(user_agent: str) -> _FakeFetcher:
        nonlocal factory_calls
        factory_calls += 1
        assert user_agent == GOOD_UA
        return fetcher

    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=tmp_path / "shared-fetcher",
        user_agent=GOOD_UA,
        allow_network=True,
        periods=_hk_periods(),
        dependencies=source_probe.ProbeDependencies(
            approvals=_all_hk_approvals(identities),
            fetcher_factory=factory,
        ),
    )
    assert report["status"] == "passed"
    assert factory_calls == 1
    assert report["requests_total"] == 9
    assert len(report["archives"]) == 3

    from investment_agent.materials.hk_native import HkNativeAdapter

    requests = [HkNativeAdapter().requests(identity, periods=_hk_periods())[0] for identity in identities]
    limiter_fetcher = HttpxMaterialFetcher(user_agent=GOOD_UA)
    assert len({id(limiter_fetcher.limiter_for(request)) for request in requests}) == 1


def test_unmapped_lines_are_informational_not_probe_failures(tmp_path: Path) -> None:
    _pilot_path, identity = _one_hk_identity(tmp_path)
    fetcher = _FakeFetcher(GOOD_UA, identity, include_unmapped=True)
    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=tmp_path / "informational",
        user_agent=GOOD_UA,
        allow_network=True,
        periods=_hk_periods(),
        dependencies=_dependencies(tmp_path, identity, fetcher),
    )
    assert report["status"] == "passed"
    assert report["issues"] == []
    assert any("unmapped_items" in item for item in report["informational"])


@pytest.mark.parametrize("status", [401, 403, 429])
def test_auth_and_rate_limit_responses_stop_the_source(tmp_path: Path, status: int) -> None:
    _pilot_path, identity = _one_hk_identity(tmp_path)
    fetcher = _FakeFetcher(GOOD_UA, identity, {"income": [status]})
    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=tmp_path / f"stop-{status}",
        user_agent=GOOD_UA,
        allow_network=True,
        periods=_hk_periods(),
        dependencies=_dependencies(tmp_path, identity, fetcher),
    )

    assert report["mode"] == "simulation"
    assert report["status"] == "failed"
    assert fetcher.calls == ["income"]
    assert report["requests_total"] == 1
    assert report["requests"][0]["http_status"] == status
    assert report["requests"][0]["status"] == f"terminal_http_{status}_stop_source"
    archived = verify_capture(
        tmp_path / f"stop-{status}",
        report["archives"][0]["manifest_ref"],
        report["archives"][0]["manifest_sha256"],
    )
    assert len(archived.responses) == 1
    assert archived.responses[0].http_status == status


def test_transient_503_is_retried_at_most_three_attempts_then_fails(tmp_path: Path) -> None:
    _pilot_path, identity = _one_hk_identity(tmp_path)
    fetcher = _FakeFetcher(GOOD_UA, identity, {"income": [503, 503, 503]})
    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=tmp_path / "retry-503",
        user_agent=GOOD_UA,
        allow_network=True,
        periods=_hk_periods(),
        dependencies=_dependencies(tmp_path, identity, fetcher),
    )

    assert report["status"] == "failed"
    assert [row["retry_attempt"] for row in report["requests"] if row["name"] == "income"] == [1, 2, 3]
    assert [row["status"] for row in report["requests"] if row["name"] == "income"] == [
        "retry_http_503",
        "retry_http_503",
        "http_503",
    ]
    assert report["requests_total"] == len(report["requests"]) == 5
    archived = verify_capture(
        tmp_path / "retry-503",
        report["archives"][0]["manifest_ref"],
        report["archives"][0]["manifest_sha256"],
    )
    assert len(archived.responses) == 5
    assert sum(response.http_status == 503 for response in archived.responses) == 3


def test_policy_closing_after_limiter_wait_prevents_send(tmp_path: Path) -> None:
    _pilot_path, identity = _one_hk_identity(tmp_path)
    approvals = _approval(identity)
    calls = 0
    base = evaluate_source_policy

    def closes_after_wait(*args: Any, **kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        decision = base(*args, **kwargs)
        if calls >= 5:
            return decision.__class__(
                **{
                    **decision.__dict__,
                    "licensing": "not_approved",
                    "bound": decision.bound,
                    "reason": "licensing_closed",
                    "reason_codes": ("licensing_unknown",),
                }
            )
        return decision

    fetcher = _FakeFetcher(GOOD_UA, identity)
    dependencies = source_probe.ProbeDependencies(
        approvals=approvals,
        fetcher_factory=lambda ua: fetcher,
        policy_evaluator=closes_after_wait,
        pilot_list_path=tmp_path / "pilot.yaml",
    )
    _one_hk_identity(tmp_path)
    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=tmp_path / "policy-race",
        user_agent=GOOD_UA,
        allow_network=True,
        periods=_hk_periods(),
        dependencies=dependencies,
    )
    assert fetcher.calls == []
    assert report["requests_total"] == 0
    assert report["status"] == "blocked"
    assert any("source_policy_closed_before_send" in issue for issue in report["issues"])


def test_stop_signal_created_during_limiter_wait_prevents_send(tmp_path: Path) -> None:
    _pilot_path, identity = _one_hk_identity(tmp_path)
    work_dir = tmp_path / "stop-during-wait"
    fetcher = _FakeFetcher(GOOD_UA, identity, after_wait=lambda: (work_dir / "STOP").touch())
    report: Any = source_probe.run_source_probe(
        source=SOURCE_EASTMONEY_HK,
        work_dir=work_dir,
        user_agent=GOOD_UA,
        allow_network=True,
        periods=_hk_periods(),
        dependencies=_dependencies(tmp_path, identity, fetcher),
    )

    assert (work_dir / "STOP").is_file()
    assert fetcher.calls == []
    assert report["mode"] == "simulation"
    assert report["status"] == "blocked"
    assert report["requests_total"] == 0
    assert len(report["archives"]) == 1
    assert any("stop_signal_after_limiter_wait" in item for item in report["issues"])
