"""Controlled HK/US identities and the market registry (plan W1 task 01).

Covers the acceptance table of ``docs/product/plans/hk-us-expansion/
01-identity-markets.md``: leading zeros, per-source symbols that never replace
each other, same-name companies that never merge, ``company_key`` reuse,
refusal on ambiguity, refusal to re-point an existing listing, the eight
checked identities, and idempotent market rows.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import func, select

from investment_agent.domain.security import (
    MARKETS,
    SOURCE_EASTMONEY_HK,
    SOURCE_SEC_COMPANYFACTS,
    SOURCE_YAHOO_CHART,
    ControlledIdentity,
    PilotListError,
    SecurityType,
    SourceBinding,
    find_identity,
    load_pilot_identities,
    normalize_ticker,
    source_symbol,
    validate_pilot_list,
)
from investment_agent.storage.models.reference import Company, Listing, Market
from investment_agent.storage.repositories.listing import ListingRepository
from tests.unit._hk_us_support import (
    PILOT_LIST_PATH,
    pilot_identities,
    seed_hk_us_markets,
    seed_pilot_listings,
)

CHECKED_ON = date(2026, 10, 11)


def _identity(**overrides: Any) -> ControlledIdentity:
    """A minimal valid HKEX identity; every registry rule is opt-out-able."""

    base: dict[str, Any] = {
        "market_code": "HKEX",
        "ticker": "00002",
        "company_key": "clp-holdings",
        "company_name": "CLP Holdings Limited",
        "company_name_zh": "中电控股",
        "security_type": SecurityType.ORDINARY,
        "quote_currency": "HKD",
        "report_currency": "HKD",
        "fiscal_year_end_note": "12-31",
        "sources": (SourceBinding(source=SOURCE_EASTMONEY_HK, symbol="00002.HK"),),
        "valid_from": CHECKED_ON,
    }
    base.update(overrides)
    return ControlledIdentity(**base)


# --- registry and ticker normalisation -------------------------------------------------


def test_markets_hold_only_the_pilot_exchanges_with_their_quote_currency():
    assert set(MARKETS) == {"HKEX", "NASDAQ", "NYSE"}
    assert MARKETS["HKEX"].timezone == "Asia/Hong_Kong"
    assert MARKETS["HKEX"].quote_currency == "HKD"
    for code in ("NASDAQ", "NYSE"):
        assert MARKETS[code].timezone == "America/New_York"
        assert MARKETS[code].quote_currency == "USD"
    # A-share markets stay in domain.a_share; this registry never claims them.
    assert "SSE" not in MARKETS and "SZSE" not in MARKETS and "BSE" not in MARKETS


def test_normalize_ticker_stores_each_market_canonical_form():
    assert normalize_ticker("HKEX", "00002.HK") == "00002"
    assert normalize_ticker("HKEX", "00002.hk") == "00002"
    assert normalize_ticker("HKEX", "2") == "00002"
    assert normalize_ticker("HKEX", "0002") == "00002"
    assert normalize_ticker("HKEX", "700") == "00700"
    assert normalize_ticker("HKEX", "00700") == "00700"
    assert normalize_ticker("HKEX", "00002") == "00002"  # idempotent
    assert normalize_ticker("NASDAQ", "aapl") == "AAPL"
    assert normalize_ticker("NASDAQ", "aapl.US") == "AAPL"
    assert normalize_ticker("NASDAQ", "AAPL") == "AAPL"
    assert normalize_ticker("NYSE", "tsm") == "TSM"


@pytest.mark.parametrize(
    ("market_code", "raw"),
    [
        ("SSE", "600519"),
        ("SZSE", "000001"),
        ("BSE", "920001"),
        ("LSE", "HSBA"),
        ("", "AAPL"),
        ("HKEX", "12a"),
        ("HKEX", "123456"),
        ("HKEX", "  "),
        ("HKEX", "00002.SS"),
        ("NASDAQ", "123"),
        ("NYSE", "a b"),
    ],
)
def test_normalize_ticker_rejects_unknown_market_and_bad_codes(market_code, raw):
    with pytest.raises(ValueError):
        normalize_ticker(market_code, raw)


# --- the checked list file -------------------------------------------------------------


def test_pilot_list_has_the_eight_checked_identities():
    identities = load_pilot_identities(PILOT_LIST_PATH)
    assert len(identities) == 8
    keys = {(identity.market_code, identity.ticker) for identity in identities}
    assert keys == {
        ("HKEX", "00002"),
        ("HKEX", "00005"),
        ("HKEX", "00700"),
        ("NASDAQ", "AAPL"),
        ("NASDAQ", "COST"),
        ("NYSE", "JPM"),
        ("NYSE", "XOM"),
        ("NYSE", "TSM"),
    }
    expected = {
        ("HKEX", "00002"): ("HKD", "HKD", SecurityType.ORDINARY),
        ("HKEX", "00005"): ("HKD", "USD", SecurityType.ORDINARY),
        ("HKEX", "00700"): ("HKD", "CNY", SecurityType.ORDINARY),
        ("NASDAQ", "AAPL"): ("USD", "USD", SecurityType.ORDINARY),
        ("NASDAQ", "COST"): ("USD", "USD", SecurityType.ORDINARY),
        ("NYSE", "JPM"): ("USD", "USD", SecurityType.ORDINARY),
        ("NYSE", "XOM"): ("USD", "USD", SecurityType.ORDINARY),
        ("NYSE", "TSM"): ("USD", "TWD", SecurityType.ADS),
    }
    for identity in identities:
        key = (identity.market_code, identity.ticker)
        quote, report, security_type = expected[key]
        assert identity.quote_currency == quote, key
        assert identity.report_currency == report, key
        assert identity.security_type is security_type, key
        assert identity.valid_from == CHECKED_ON, key
        assert identity.valid_to is None, key
        assert identity.fiscal_year_end_note, key
    # Eight distinct company keys: the list shares no company yet.
    assert len({identity.company_key for identity in identities}) == 8
    by_key = {(identity.market_code, identity.ticker): identity for identity in identities}
    tsm = by_key[("NYSE", "TSM")]
    assert tsm.ads_ratio == "5"
    assert "20-F" in tsm.notes
    assert by_key[("NASDAQ", "AAPL")].ads_ratio is None
    # 52/53-week fiscal years are noted for the two US retailers, 12-31 for the rest.
    assert "52/53" in by_key[("NASDAQ", "AAPL")].fiscal_year_end_note
    assert "52/53" in by_key[("NASDAQ", "COST")].fiscal_year_end_note
    assert by_key[("HKEX", "00005")].fiscal_year_end_note == "12-31"


def test_pilot_list_binds_source_codes_per_source():
    by_key = {
        (identity.market_code, identity.ticker): identity
        for identity in load_pilot_identities(PILOT_LIST_PATH)
    }
    hsbc = by_key[("HKEX", "00005")]
    eastmoney_code = source_symbol(hsbc, SOURCE_EASTMONEY_HK)
    yahoo_code = source_symbol(hsbc, SOURCE_YAHOO_CHART)
    assert eastmoney_code == "00005.HK"
    assert yahoo_code == "0005.HK"
    assert source_symbol(hsbc, SOURCE_SEC_COMPANYFACTS) == "1089113"
    # The two HK symbols share the leading-zero-stripped prefix but are separate
    # codes: neither may be derived from the other.
    assert eastmoney_code != yahoo_code
    assert yahoo_code is not None and not yahoo_code.startswith("0000")
    assert source_symbol(hsbc, "fmp.v1") is None
    assert source_symbol(by_key[("HKEX", "00700")], SOURCE_YAHOO_CHART) == "0700.HK"
    assert source_symbol(by_key[("HKEX", "00002")], SOURCE_EASTMONEY_HK) == "00002.HK"
    # Every US issuer binds a plain decimal CIK that is documented as an issuer
    # identifier; the HK trio binds no SEC source except HSBC's cross-check.
    for key, identity in by_key.items():
        sec = [b for b in identity.sources if b.source == SOURCE_SEC_COMPANYFACTS]
        if key[0] == "HKEX":
            assert {b.symbol for b in sec} <= {"1089113"}, key
        else:
            assert len(sec) == 1, key
            assert sec[0].symbol.isdigit(), key
            assert sec[0].symbol == str(int(sec[0].symbol)), key  # decimal, not padded
            assert "发行人标识" in sec[0].note, key
        if key[0] == "HKEX":
            assert {b.source for b in identity.sources} & {SOURCE_EASTMONEY_HK, SOURCE_YAHOO_CHART}, key


def test_find_identity_matches_exactly_and_normalises_nothing():
    found = find_identity(market_code="HKEX", ticker="00005", path=PILOT_LIST_PATH)
    assert found is not None
    assert found.company_name == "HSBC Holdings plc"
    assert find_identity(market_code="HKEX", ticker="5", path=PILOT_LIST_PATH) is None
    assert find_identity(market_code="NASDAQ", ticker="aapl", path=PILOT_LIST_PATH) is None
    assert find_identity(market_code="SSE", ticker="600519", path=PILOT_LIST_PATH) is None


# --- list validation -------------------------------------------------------------------


def test_validate_rejects_duplicate_listing_and_duplicate_source_symbol():
    first = _identity()
    with pytest.raises(PilotListError, match="duplicate"):
        validate_pilot_list([first, _identity(company_key="other-issuer")])
    with pytest.raises(PilotListError, match="already bound"):
        validate_pilot_list(
            [
                first,
                _identity(
                    ticker="00999",
                    company_key="other-issuer",
                    company_name="Other Issuer",
                ),
            ]
        )


def test_validate_rejects_quote_currency_disagreeing_with_registry():
    with pytest.raises(PilotListError, match="quote_currency"):
        validate_pilot_list([_identity(quote_currency="USD")])
    # Report currency is independent of the market default; USD on a HKEX
    # listing is exactly the HSBC case.
    validate_pilot_list([_identity(report_currency="USD")])
    with pytest.raises(PilotListError, match="three-letter"):
        validate_pilot_list([_identity(report_currency="usd")])


def test_validate_requires_ads_ratio_and_a_source_of_the_market():
    with pytest.raises(PilotListError, match="ADS"):
        validate_pilot_list([_identity(security_type=SecurityType.ADS)])
    with pytest.raises(PilotListError, match="ads_ratio"):
        validate_pilot_list([_identity(ads_ratio="5")])
    with pytest.raises(PilotListError, match="source binding"):
        validate_pilot_list(
            [
                _identity(
                    market_code="NASDAQ",
                    ticker="AAPL",
                    quote_currency="USD",
                    sources=(SourceBinding(source=SOURCE_EASTMONEY_HK, symbol="00002.HK"),),
                )
            ]
        )


def test_validate_rejects_empty_list_and_bad_validity_window():
    with pytest.raises(PilotListError, match="no identities"):
        validate_pilot_list([])
    with pytest.raises(PilotListError, match="valid_to precedes"):
        validate_pilot_list([_identity(valid_to=date(2026, 10, 10))])


MINIMAL_LIST = """\
identities:
  - market_code: HKEX
    ticker: "00002"
    company_key: clp-holdings
    company_name: CLP Holdings Limited
    company_name_zh: 中电控股
    security_type: ordinary
    quote_currency: HKD
    report_currency: HKD
    fiscal_year_end_note: "12-31"
    valid_from: 2026-10-11
    sources:
      - source: eastmoney.hk.native.v1
        symbol: "00002.HK"
"""


@pytest.mark.parametrize(
    ("old", "new", "match"),
    [
        ("    report_currency: HKD\n", "", "report_currency"),
        ("    company_key: clp-holdings\n", "", "company_key"),
        ('ticker: "00002"', 'ticker: "2"', "canonical"),
        ('ticker: "00002"', "ticker: 00002", "quote it"),
        ("market_code: HKEX", "market_code: SSE", "not in the HK/US registry"),
        ("    valid_from: 2026-10-11\n", "", "valid_from"),
    ],
)
def test_load_rejects_a_malformed_list_file(tmp_path: Path, old, new, match):
    path = tmp_path / "pilot_list.yaml"
    path.write_text(MINIMAL_LIST.replace(old, new), encoding="utf-8")
    with pytest.raises(PilotListError, match=match):
        load_pilot_identities(path)


# --- market rows -----------------------------------------------------------------------


async def test_ensure_market_is_idempotent_and_re_stamps_registry_values(db_session):
    repository = ListingRepository(db_session)
    first = await repository.ensure_market(MARKETS["HKEX"])
    second = await repository.ensure_market(MARKETS["HKEX"])
    assert first is second
    assert (first.code, first.name, first.timezone, first.currency) == (
        "HKEX",
        "Hong Kong Stock Exchange",
        "Asia/Hong_Kong",
        "HKD",
    )
    assert await db_session.scalar(select(func.count()).select_from(Market)) == 1
    # A stale row is corrected from the registry instead of kept.
    first.currency = "CNY"
    await db_session.flush()
    await repository.ensure_market(MARKETS["HKEX"])
    assert first.currency == "HKD"
    assert await db_session.scalar(select(func.count()).select_from(Market)) == 1


async def test_ensure_controlled_listings_seeds_the_eight_identities(db_session):
    listings = await seed_pilot_listings(db_session)
    assert len(listings) == 8
    assert set(listings) == {
        (identity.market_code, identity.ticker) for identity in pilot_identities()
    }
    assert await db_session.scalar(select(func.count()).select_from(Company)) == 8
    assert await db_session.scalar(select(func.count()).select_from(Market)) == 3
    again = await seed_pilot_listings(db_session)
    assert {key: row.id for key, row in again.items()} == {
        key: row.id for key, row in listings.items()
    }
    assert await db_session.scalar(select(func.count()).select_from(Listing)) == 8
    assert await db_session.scalar(select(func.count()).select_from(Company)) == 8
    tsm = listings[("NYSE", "TSM")]
    tsm_company = await db_session.get(Company, tsm.company_id)
    assert tsm_company is not None
    assert tsm_company.canonical_name == (
        "Taiwan Semiconductor Manufacturing Company Limited"
    )


async def test_controlled_listings_need_the_market_row_first(db_session):
    with pytest.raises(ValueError, match="Unknown market: HKEX"):
        await ListingRepository(db_session).ensure_controlled_listings([_identity()])


# --- company resolution -----------------------------------------------------------------


async def test_same_company_key_shares_one_company_across_markets(db_session):
    await seed_hk_us_markets(db_session)
    repository = ListingRepository(db_session)
    listings = await repository.ensure_controlled_listings(
        [
            _identity(),
            _identity(
                market_code="NASDAQ",
                ticker="CLPX",
                quote_currency="USD",
                sources=(SourceBinding(source=SOURCE_SEC_COMPANYFACTS, symbol="999001"),),
            ),
        ]
    )
    assert set(listings) == {("HKEX", "00002"), ("NASDAQ", "CLPX")}
    assert len({listing.company_id for listing in listings.values()}) == 1
    assert await db_session.scalar(select(func.count()).select_from(Company)) == 1
    assert await db_session.scalar(select(func.count()).select_from(Listing)) == 2


async def test_same_name_with_different_company_keys_never_merges(db_session):
    await seed_hk_us_markets(db_session)
    repository = ListingRepository(db_session)
    await repository.ensure_controlled_listings(
        [
            _identity(company_key="key-a"),
            _identity(
                market_code="NASDAQ",
                ticker="CLPX",
                quote_currency="USD",
                company_key="key-b",
                sources=(SourceBinding(source=SOURCE_SEC_COMPANYFACTS, symbol="999002"),),
            ),
        ]
    )
    named = select(Company.id).where(Company.canonical_name == "CLP Holdings Limited")
    company_ids = list((await db_session.execute(named)).scalars())
    assert len(company_ids) == 2
    hk = await repository.get_by_ticker("HKEX", "00002")
    us = await repository.get_by_ticker("NASDAQ", "CLPX")
    assert hk is not None and us is not None
    assert hk.company_id != us.company_id
    assert {hk.company_id, us.company_id} == set(company_ids)


async def test_ambiguous_same_name_companies_are_refused(db_session):
    await seed_hk_us_markets(db_session)
    db_session.add_all(
        [
            Company(canonical_name="CLP Holdings Limited"),
            Company(canonical_name="CLP Holdings Limited"),
        ]
    )
    await db_session.flush()
    repository = ListingRepository(db_session)
    with pytest.raises(ValueError, match="refusing to merge by name") as excinfo:
        await repository.ensure_controlled_listings([_identity()])
    message = str(excinfo.value)
    assert "HKEX/00002" in message
    assert "CLP Holdings Limited" in message
    assert await db_session.scalar(select(func.count()).select_from(Company)) == 2
    assert await db_session.scalar(select(func.count()).select_from(Listing)) == 0


async def test_existing_listing_pointing_elsewhere_is_refused_and_kept(db_session):
    await seed_hk_us_markets(db_session)
    alpha = Company(canonical_name="Alpha Limited")
    beta = Company(canonical_name="Beta Limited")
    db_session.add_all([alpha, beta])
    await db_session.flush()
    db_session.add_all(
        [
            Listing(company_id=alpha.id, market_code="HKEX", ticker="00002"),
            Listing(company_id=beta.id, market_code="NASDAQ", ticker="ALFA"),
        ]
    )
    await db_session.flush()
    repository = ListingRepository(db_session)
    with pytest.raises(ValueError, match="refusing to re-point") as excinfo:
        await repository.ensure_controlled_listings(
            [
                _identity(company_name="Alpha Limited", company_key="key-a"),
                _identity(
                    market_code="NASDAQ",
                    ticker="ALFA",
                    quote_currency="USD",
                    company_name="Alpha Limited",
                    company_key="key-a",
                    sources=(
                        SourceBinding(source=SOURCE_SEC_COMPANYFACTS, symbol="999003"),
                    ),
                ),
            ]
        )
    message = str(excinfo.value)
    assert "NASDAQ/ALFA" in message
    assert "Alpha Limited" in message
    # Neither the companies nor the existing listings were touched.
    assert await db_session.scalar(select(func.count()).select_from(Company)) == 2
    hk = await repository.get_by_ticker("HKEX", "00002")
    us = await repository.get_by_ticker("NASDAQ", "ALFA")
    assert hk is not None and us is not None
    assert hk.company_id == alpha.id
    assert us.company_id == beta.id


async def test_one_listing_cannot_be_claimed_by_two_company_keys(db_session):
    await seed_hk_us_markets(db_session)
    repository = ListingRepository(db_session)
    with pytest.raises(ValueError, match="claimed by company keys"):
        await repository.ensure_controlled_listings(
            [
                _identity(company_key="key-a"),
                _identity(company_key="key-b", company_name="Another Limited"),
            ]
        )
    assert await db_session.scalar(select(func.count()).select_from(Company)) == 0
    assert await db_session.scalar(select(func.count()).select_from(Listing)) == 0
