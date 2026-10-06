"""AkShare Shenwan 2021 adapter (P1-A9): fixture replay, mapping, guards, TLS scope."""

from __future__ import annotations

import ssl
import sys
from datetime import UTC, date, datetime
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

from investment_agent.providers import akshare_shenwan
from investment_agent.providers.akshare_shenwan import (
    INTERMEDIATE_CERT_PATH,
    RawTable,
    ShenwanError,
    ShenwanSchemaError,
    ShenwanTransientError,
    combined_ca_bundle,
    is_swsresearch_url,
    parse_class_code_table,
    parse_history,
    parse_index_info,
    swsresearch_tls,
    to_canonical_ticker,
    vote_code,
)
from investment_agent.providers.errors import LookaheadDataError, ProviderTransientError
from investment_agent.providers.shenwan_directory import (
    SW2021_CUTOVER,
    ShenwanIndustry,
    build_directory,
    default_shenwan_directory,
    dump_shenwan_directory,
    is_sw2021_interval,
    level_codes,
    parse_shenwan_directory,
)
from tests.unit._shenwan_fake import (
    CAPTURED_AT,
    FakeShenwanBackend,
    class_code_table,
    constituents_table,
    fake_shenwan_client,
    history_table,
    info_table,
)

# --------------------------------------------------------------------------- #
# History replay
# --------------------------------------------------------------------------- #


async def test_history_fixture_replay_moutai_changes_on_2021_07_30():
    history = await fake_shenwan_client().fetch_industry_history()
    moutai = history.changes["600519.SH"]
    assert [(change.start_date, change.code) for change in moutai] == [
        (date(2001, 7, 31), "340301"),
        (date(2021, 7, 30), "340501"),
    ]
    assert history.code_as_of("600519.SH", date(2021, 7, 29)) == "340301"
    assert history.code_as_of("600519.SH", date(2021, 7, 30)) == "340501"
    assert history.code_as_of("600519.SH", date(2000, 1, 1)) is None
    assert history.system == "sw2021" and history.source == "akshare"
    assert history.skipped_symbols == ()
    # every CSI 300 fixture member (+ edge cases) is in the replayed answer
    assert len(history.changes) >= 300


async def test_changes_are_strictly_ordered_and_timestamps_become_dates():
    history = await fake_shenwan_client().fetch_industry_history()
    for ticker, changes in history.changes.items():
        days = [change.start_date for change in changes]
        assert days == sorted(set(days)), ticker
        assert all(isinstance(day, date) and not isinstance(day, datetime) for day in days)


@pytest.mark.parametrize(
    ("symbol", "ticker"),
    [
        ("600519", "600519.SH"),
        ("601318", "601318.SH"),
        ("688981", "688981.SH"),
        ("900901", "900901.SH"),
        ("000001", "000001.SZ"),
        ("002594", "002594.SZ"),
        ("300750", "300750.SZ"),
        ("301718", "301718.SZ"),
        ("200002", "200002.SZ"),
        ("920002", "920002.BJ"),
        ("832317", "832317.BJ"),
        ("430047", "430047.BJ"),
        ("874090", "874090.BJ"),
        ("123456", None),
        ("60051", None),
        ("abcdef", None),
    ],
)
def test_exchange_inferred_from_code_prefix(symbol: str, ticker: str | None):
    assert to_canonical_ticker(symbol) == ticker


async def test_fixture_symbols_all_map_including_beijing():
    history = await fake_shenwan_client().fetch_industry_history()
    assert "920002.BJ" in history.changes
    assert "832317.BJ" in history.changes
    assert history.changes["000001.SZ"][-1].code == "480301"


def test_constituent_tickers_agree_with_the_prefix_rule():
    """legulegu returns suffixed tickers; the inferred suffix must match."""

    for index_code in ("850111.SI", "850339.SI"):
        for row in constituents_table(index_code).rows:
            ticker = str(row["股票代码"])
            assert to_canonical_ticker(ticker.split(".")[0]) == ticker


def _table(rows: list[dict[str, Any]], columns: tuple[str, ...] | None = None) -> RawTable:
    return RawTable(columns or ("symbol", "start_date", "industry_code", "update_time"), tuple(rows))


def test_start_date_after_capture_date_is_lookahead():
    table = _table(
        [{"symbol": "600519", "start_date": "2026-10-07", "industry_code": "340501",
          "update_time": "2026-10-07"}]
    )
    # 2026-10-06 23:30 UTC is already 2026-10-07 in Shanghai -> allowed
    parse_history(table, captured_at=datetime(2026, 10, 6, 23, 30, tzinfo=UTC))
    with pytest.raises(LookaheadDataError, match="after the capture date 2026-10-06"):
        parse_history(table, captured_at=CAPTURED_AT)


async def test_lookahead_rejected_when_replayed_before_the_last_change():
    client = fake_shenwan_client(captured_at=datetime(2021, 1, 1, tzinfo=UTC))
    with pytest.raises(LookaheadDataError):
        await client.fetch_industry_history()


def test_renamed_column_is_a_terminal_schema_error():
    renamed = RawTable(
        ("symbol", "计入日期", "industry_code", "update_time"),
        ({"symbol": "600519", "计入日期": "2021-07-30", "industry_code": "340501"},),
    )
    with pytest.raises(ShenwanSchemaError, match="start_date"):
        parse_history(renamed, captured_at=CAPTURED_AT)
    assert not issubclass(ShenwanSchemaError, ProviderTransientError)


def test_bad_values_and_empty_answers_fail_loudly():
    with pytest.raises(ShenwanSchemaError, match="invalid industry code"):
        parse_history(
            _table([{"symbol": "600519", "start_date": "2021-07-30", "industry_code": "白酒"}]),
            captured_at=CAPTURED_AT,
        )
    with pytest.raises(ShenwanSchemaError, match="invalid start_date"):
        parse_history(
            _table([{"symbol": "600519", "start_date": "soon", "industry_code": "340501"}]),
            captured_at=CAPTURED_AT,
        )
    with pytest.raises(ShenwanSchemaError, match="no rows"):
        parse_history(_table([]), captured_at=CAPTURED_AT)
    with pytest.raises(ShenwanSchemaError, match="上级行业"):
        parse_index_info(RawTable(("行业代码", "行业名称", "成份个数"), ()), 3)


def test_unknown_prefix_is_skipped_and_reported():
    history = parse_history(
        _table(
            [
                {"symbol": "600519", "start_date": "2021-07-30", "industry_code": "340501"},
                {"symbol": "123456", "start_date": "2021-07-30", "industry_code": "340501"},
            ]
        ),
        captured_at=CAPTURED_AT,
    )
    assert list(history.changes) == ["600519.SH"]
    assert history.skipped_symbols == ("123456",)


async def test_transient_errors_are_retried_terminal_ones_are_not():
    flaky = FakeShenwanBackend(
        errors=[ShenwanTransientError("HTTP 503", provider="akshare")]
    )
    history = await fake_shenwan_client(flaky).fetch_industry_history()
    assert flaky.calls == ["history", "history"]
    assert "600519.SH" in history.changes

    broken = FakeShenwanBackend(errors=[ShenwanError("HTTP 404", provider="akshare")])
    with pytest.raises(ShenwanError):
        await fake_shenwan_client(broken).fetch_industry_history()
    assert broken.calls == ["history"]


# --------------------------------------------------------------------------- #
# Directory
# --------------------------------------------------------------------------- #


def test_level_codes_and_era_rule():
    assert level_codes("340501") == ("340000", "340500", "340501")
    assert is_sw2021_interval(date(2001, 7, 31), None)
    assert is_sw2021_interval(date(2001, 7, 31), date(2021, 7, 31))
    assert not is_sw2021_interval(date(2001, 7, 31), SW2021_CUTOVER)


async def test_official_code_table_builds_the_directory():
    directory = await fake_shenwan_client().fetch_code_directory(version="t")
    assert (len(directory.codes(1)), len(directory.codes(2)), len(directory.codes(3))) == (
        31,
        134,
        346,
    )
    assert [item.name for item in directory.chain("340501")] == ["食品饮料", "白酒Ⅱ", "白酒Ⅲ"]
    assert directory.get("490201").parent == "490200"
    assert directory.captured_on == date(2026, 10, 6)
    # the committed file is exactly what the tool generates from this table
    assert directory.industries == default_shenwan_directory().industries


def test_code_table_schema_change_is_terminal():
    table = class_code_table()
    renamed = RawTable(
        tuple("一级行业" if column == "一级行业名称" else column for column in table.columns),
        table.rows,
    )
    with pytest.raises(ShenwanSchemaError, match="一级行业名称"):
        parse_class_code_table(renamed, version="t")
    nameless = RawTable(table.columns, ({**table.rows[0], "一级行业名称": None},))
    with pytest.raises(ShenwanSchemaError, match="has no name"):
        parse_class_code_table(nameless, version="t")


async def test_reverse_lookup_cross_check_agrees_with_the_official_table():
    client = fake_shenwan_client()
    history = await client.fetch_industry_history()
    tickers = [str(row["股票代码"]) for row in constituents_table("850339.SI").rows]
    lookup = vote_code("850339.SI", tickers, history.current_codes())
    assert lookup.code == "220309"
    assert lookup.agreement == 1.0

    reverse, lookups, constituents = await client.reverse_lookup_directory(history)
    assert {code for code, tickers in constituents.items() if tickers} == {"850111.SI", "850339.SI"}
    assert len(lookups) == len(info_table(3).rows)
    assert reverse.get("220309").index_code == "850339.SI"
    assert [item.name for item in reverse.chain("110101")] == ["农林牧渔", "种植业", "种子"]
    official = default_shenwan_directory()
    diff = official.diff(reverse)
    assert diff.added == () and diff.renamed == ()
    # indexes without fixture constituents stay unresolved, never silently dropped
    assert len(reverse.unresolved_index_codes) == len(info_table(3).rows) - 2


async def test_reverse_lookup_subset_only_queries_requested_indexes():
    backend = FakeShenwanBackend()
    client = fake_shenwan_client(backend)
    history = await client.fetch_industry_history()
    _, lookups, _ = await client.reverse_lookup_directory(history, index_codes=["850111.SI"])
    assert [lookup.index_code for lookup in lookups] == ["850111.SI"]
    assert [call for call in backend.calls if call.endswith(".SI")] == ["850111.SI"]


async def test_reverse_lookup_tolerates_a_few_unavailable_pages():
    class Flaky(FakeShenwanBackend):
        def third_constituents(self, index_code: str) -> RawTable:
            if index_code == "850111.SI":
                raise ShenwanTransientError("HTTP 504", provider="akshare")
            return super().third_constituents(index_code)

    client = fake_shenwan_client(Flaky())
    history = await client.fetch_industry_history()
    reverse, _, _ = await client.reverse_lookup_directory(
        history, index_codes=["850111.SI", "850339.SI"]
    )
    assert "850111.SI" in reverse.unresolved_index_codes
    assert reverse.get("220309") is not None


def test_directory_yaml_round_trip_and_diff():
    industries = [
        ShenwanIndustry("340000", "食品饮料", 1, None, "801120.SI"),
        ShenwanIndustry("340500", "白酒Ⅱ", 2, "340000", "801125.SI"),
        ShenwanIndustry("340501", "白酒Ⅲ", 3, "340500", "851251.SI"),
    ]
    directory = build_directory(industries, version="v1", captured_on=date(2026, 10, 6))
    parsed = parse_shenwan_directory(__import__("yaml").safe_load(dump_shenwan_directory(directory)))
    assert parsed.industries == directory.industries
    renamed = build_directory(
        [industries[0], industries[1], ShenwanIndustry("340501", "白酒", 3, "340500")],
        version="v2",
    )
    diff = directory.diff(renamed)
    assert diff.renamed == (("340501", "白酒Ⅲ", "白酒"),)
    assert diff.changed
    with pytest.raises(ValueError, match="parent"):
        build_directory([ShenwanIndustry("340501", "白酒Ⅲ", 3, "340000")], version="x")


def test_committed_directory_names_every_current_csi300_code():
    directory = default_shenwan_directory()
    assert len(directory.codes(1)) == 31
    history = parse_history(history_table(), captured_at=CAPTURED_AT)
    import json

    members = json.loads(
        (Path(__file__).parents[1] / "fixtures" / "factors" / "csi300_industry_2026-10-05.json")
        .read_text(encoding="utf-8")
    )["members"]
    tickers = [member["code"][3:] + "." + member["code"][:2].upper() for member in members]
    missing = {
        ticker: history.changes[ticker][-1].code
        for ticker in tickers
        if directory.get(history.changes[ticker][-1].code) is None
    }
    assert not missing


# --------------------------------------------------------------------------- #
# TLS scope (no network)
# --------------------------------------------------------------------------- #


def test_intermediate_certificate_is_a_valid_pem():
    text = INTERMEDIATE_CERT_PATH.read_text(encoding="utf-8")
    block = text[text.index("-----BEGIN CERTIFICATE-----") :]
    der = ssl.PEM_cert_to_DER_cert(block)
    assert b"GeoTrust G2 TLS CN RSA4096 SHA256 2022 CA1" in der
    context = ssl.create_default_context(cafile=str(combined_ca_bundle()))
    assert context.cert_store_stats()["x509_ca"] > 100


def test_combined_bundle_is_certifi_plus_intermediate():
    import certifi

    bundle = combined_ca_bundle().read_text(encoding="utf-8")
    certifi_text = Path(certifi.where()).read_text(encoding="utf-8")
    assert bundle.startswith(certifi_text[:200])
    assert "GeoTrust G2 TLS CN RSA4096 SHA256 2022 CA1" in bundle
    assert combined_ca_bundle() == combined_ca_bundle()  # written once


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://www.swsresearch.com/swindex/pdf/x.xls", True),
        ("https://swsresearch.com/", True),
        ("https://legulegu.com/stockdata", False),
        ("https://swsresearch.com.evil.example/", False),
        ("https://notswsresearch.com/", False),
    ],
)
def test_only_swsresearch_hosts_get_the_bundle(url: str, expected: bool):
    assert is_swsresearch_url(url) is expected


class _Exceptions:
    """Mirrors the ``requests.exceptions`` hierarchy (SSLError is a ConnectionError)."""

    class RequestError(Exception):
        pass

    class ConnectionError(RequestError):  # noqa: A001 - mirrors requests
        pass

    class SSLError(ConnectionError):
        pass

    class Timeout(RequestError):  # noqa: N818 - mirrors requests
        pass


def _fake_requests(status: int = 200, error: Exception | None = None) -> Any:
    calls: list[tuple[str, dict[str, Any]]] = []

    def get(url: str, **kwargs: Any) -> Any:
        calls.append((url, kwargs))
        if error is not None:
            raise error
        return SimpleNamespace(status_code=status, content=b"ok")

    return SimpleNamespace(get=get, exceptions=_Exceptions, calls=calls, Session=object)


def _module(requests: Any) -> ModuleType:
    module = ModuleType("fake_akshare_module")
    module.requests = requests  # type: ignore[attr-defined]
    return module


def test_scoped_requests_verifies_swsresearch_with_bundle_only(tmp_path):
    bundle = tmp_path / "bundle.pem"
    real = _fake_requests()
    module = _module(real)
    with swsresearch_tls([module], timeout=7, bundle=bundle):
        assert module.requests is not real
        module.requests.get("https://www.swsresearch.com/a.xls", headers={})
        module.requests.get("https://legulegu.com/b")
        assert module.requests.Session is object  # attribute passthrough
    assert module.requests is real  # restored
    (sws_url, sws_kwargs), (other_url, other_kwargs) = real.calls
    assert sws_kwargs == {"headers": {}, "timeout": 7, "verify": str(bundle)}
    assert other_kwargs == {"timeout": 7}  # default verification for every other host


def test_scoped_requests_restores_on_error(tmp_path):
    real = _fake_requests(status=503)
    module = _module(real)
    with (
        pytest.raises(ShenwanTransientError, match="HTTP 503"),
        swsresearch_tls([module], bundle=tmp_path / "b.pem"),
    ):
        module.requests.get("https://www.swsresearch.com/a.xls")
    assert module.requests is real


@pytest.mark.parametrize(
    ("status", "error", "expected", "code"),
    [
        (429, None, ShenwanTransientError, "429"),
        (502, None, ShenwanTransientError, "502"),
        (404, None, ShenwanError, "404"),
        (200, _Exceptions.SSLError("bad chain"), ShenwanError, "tls"),
        (200, _Exceptions.ConnectionError("reset"), ShenwanTransientError, None),
        (200, _Exceptions.Timeout("slow"), ShenwanTransientError, None),
    ],
)
def test_scoped_requests_classifies_failures(tmp_path, status, error, expected, code):
    module = _module(_fake_requests(status=status, error=error))
    with (
        swsresearch_tls([module], bundle=tmp_path / "b.pem"),
        pytest.raises(expected) as caught,
    ):
        module.requests.get("https://www.swsresearch.com/a.xls")
    assert caught.value.error_code == code
    if expected is ShenwanError:
        assert not isinstance(caught.value, ProviderTransientError)


def test_akshare_is_not_imported_by_offline_tests():
    assert "akshare" not in sys.modules
    assert akshare_shenwan.PROVIDER == "akshare"


# --------------------------------------------------------------------------- #
# Network smoke (deselected by default)
# --------------------------------------------------------------------------- #


@pytest.mark.network
async def test_live_history_download_over_scoped_tls():
    from investment_agent.providers.akshare_shenwan import AkshareShenwanClient

    history = await AkshareShenwanClient().fetch_industry_history()
    assert len(history.changes) > 5000
    assert history.code_as_of("600519.SH", date(2026, 1, 1)) == "340501"
