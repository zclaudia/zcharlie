"""UTC timestamp-normalization helper tests (``domain.time``)."""

from datetime import UTC, datetime, timedelta, timezone

from investment_agent.domain.time import as_utc, optional_utc


def test_as_utc_assumes_naive_input_is_utc_wall_time():
    naive = datetime(2026, 10, 8, 1, 30, 15, 123456)
    normalized = as_utc(naive)
    assert normalized == naive.replace(tzinfo=UTC)
    assert normalized.tzinfo is UTC
    assert normalized.utcoffset() == timedelta(0)


def test_as_utc_passes_aware_utc_through():
    aware = datetime(2026, 10, 8, 1, 30, tzinfo=UTC)
    assert as_utc(aware) == aware
    assert as_utc(aware).tzinfo is UTC


def test_as_utc_converts_non_utc_offset():
    plus_eight = timezone(timedelta(hours=8))
    converted = as_utc(datetime(2026, 10, 8, 9, 30, tzinfo=plus_eight))
    assert converted == datetime(2026, 10, 8, 1, 30, tzinfo=UTC)
    assert converted.tzinfo is UTC


def test_as_utc_does_not_mutate_its_input():
    naive = datetime(2026, 10, 8, 1, 30)
    aware = datetime(2026, 10, 8, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    before = (naive, naive.tzinfo), (aware, aware.tzinfo, aware.utcoffset())
    as_utc(naive)
    as_utc(aware)
    assert ((naive, naive.tzinfo), (aware, aware.tzinfo, aware.utcoffset())) == before
    assert naive.tzinfo is None
    assert aware.hour == 9


def test_optional_utc_none_passes_through():
    assert optional_utc(None) is None


def test_optional_utc_delegates_to_as_utc():
    naive = datetime(2026, 10, 8, 1, 30)
    aware = datetime(2026, 10, 8, 9, 30, tzinfo=timezone(timedelta(hours=8)))
    assert optional_utc(naive) == as_utc(naive)
    assert optional_utc(aware) == as_utc(aware) == datetime(2026, 10, 8, 1, 30, tzinfo=UTC)


def test_optional_utc_does_not_mutate_its_input():
    naive = datetime(2026, 10, 8, 1, 30)
    optional_utc(naive)
    assert naive.tzinfo is None
