"""Candidate mapping tests; fixture success never enables production DCF."""
import importlib.util
import json
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from investment_agent.valuation.fcfe import AccountingChecks, assess_year

NOW = datetime(2026, 10, 10, tzinfo=UTC)
ROW = dict(SECURITY_CODE='600519', REPORT_DATE='2025-12-31',
           NOTICE_DATE='2026-04-17', UPDATE_DATE='2026-04-17', CURRENCY='CNY',
           NETCASH_OPERATE='100', CONSTRUCT_LONG_ASSET='10',
           RECEIVE_LOAN_CASH='20', ISSUE_BOND='5', PAY_DEBT_CASH='7')
CHECKS = AccountingChecks(Decimal('3'), False, True, True, True, True, True, True)


def assess(row=ROW, checks=CHECKS, **kwargs):
    return assess_year(row, as_of=NOW, captured_at=NOW, expected_code='600519',
                       checks=checks, **kwargs)


def test_bridge_and_interest_not_deducted_twice():
    assert assess()['fcfe_candidate'] == '105'
    assert assess(checks=replace(CHECKS, interest_in_cfo=True))['fcfe_candidate'] == '108'


@pytest.mark.parametrize('field,reason', [('RECEIVE_LOAN_CASH','missing_borrowings'),
                                         ('ISSUE_BOND','missing_bonds'),
                                         ('PAY_DEBT_CASH','missing_repayments')])
def test_missing_not_zero(field, reason):
    result = assess({**ROW, field: None})
    assert result['fcfe_candidate'] is None
    assert reason in result['reasons']


def test_combined_distribution_and_accrual_never_cash_interest():
    result = assess({**ROW, 'ASSIGN_DIVIDEND_PORFIT': '3',
                     'PAY_INTEREST_COMMISSION': '3', 'INTEREST_EXPENSE': '3'},
                    replace(CHECKS, cash_interest=None))
    assert 'cash_interest_unverified' in result['reasons']
    assert result['fcfe_candidate'] is None


@pytest.mark.parametrize('checks,reason', [
    (replace(CHECKS, financing_disjoint=False),'financing_disjoint_unverified'),
    (replace(CHECKS, ordinary_equity=False),'ordinary_equity_unverified'),
    (replace(CHECKS, non_operating_consistent=False),'non_operating_consistent_unverified')])
def test_accounting_gate(checks, reason):
    assert reason in assess(checks=checks)['reasons']


@pytest.mark.parametrize('changes,reason', [
    ({'SECURITY_CODE':'000333'},'subject_mismatch'),
    ({'REPORT_DATE':'2025-99-31'},'invalid_report_period'),
    ({'REPORT_DATE':'2027-12-31'},'future_report_period'),
    ({'UPDATE_DATE':'2027-01-01'},'future_update_date'),
    ({'PAY_DEBT_CASH':'-1'},'invalid_sign_repayments'),
    ({'CURRENCY':'USD'},'currency_unverified')])
def test_source_boundaries(changes, reason):
    assert reason in assess({**ROW, **changes})['reasons']


def test_financial_excluded_and_naive_rejected():
    assert assess(financial=True)['primary_reason'] == 'financial_sector_excluded'
    with pytest.raises(ValueError):
        assess_year(ROW, as_of=NOW.replace(tzinfo=None), captured_at=NOW, expected_code='600519')


def test_real_capture_replay_and_tampering():
    path = Path('tools/probe_fcfe_sources.py')
    spec = importlib.util.spec_from_file_location('fcfe_probe', path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = json.loads(Path('docs/product/probes/ag11-fcfe-capture-2026-10-10.json').read_text())
    saved = json.loads(Path('docs/product/probes/ag11-fcfe-coverage-2026-10-10.json').read_text())
    assert module.replay(source, datetime.fromisoformat(saved['as_of'])) == saved
    assert saved['summary'] == dict(raw_fields_available=0, accounting_usable=0,
                                    method_applicable=0, numerical_success=0)
    assert all(stock['complete_annual_rows'] for stock in saved['stocks'])
    source['captures'][0]['body'] += ' '
    with pytest.raises(ValueError, match='hash mismatch'):
        module.replay(source, NOW)


def test_backend_preserves_unmapped_values_and_nulls(monkeypatch):
    from investment_agent.providers.akshare_eastmoney import AkshareEastmoneyBackend
    from investment_agent.providers.akshare_shenwan import RawTable
    backend = AkshareEastmoneyBackend()
    raw = RawTable(columns=('NETCASH_OPERATE', 'ISSUE_BOND', 'UNMAPPED'),
                   rows=({'NETCASH_OPERATE': 1, 'ISSUE_BOND': None, 'UNMAPPED': 'audit'},))
    monkeypatch.setattr(backend, '_call', lambda *args, **kwargs: raw)
    assert backend.cash_flow_table('SH600519') is raw


def test_shanghai_date_visibility():
    # UTC Oct 9 17:00 is Shanghai Oct 10: a same-day notice is visible.
    when = datetime(2026, 10, 9, 17, tzinfo=UTC)
    result = assess_year({**ROW, 'NOTICE_DATE':'2026-10-10', 'UPDATE_DATE':'2026-10-10'},
                         as_of=when, captured_at=when, expected_code='600519', checks=CHECKS)
    assert result['accounting_usable']


def test_three_rows_with_wrong_years_are_not_complete():
    spec = importlib.util.spec_from_file_location('fcfe_probe_periods', Path('tools/probe_fcfe_sources.py'))
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    source = json.loads(Path('docs/product/probes/ag11-fcfe-capture-2026-10-10.json').read_text())
    import hashlib
    first = source['captures'][0]
    body = json.loads(first['body'])
    body['data'][0]['REPORT_DATE'] = '2022-12-31'
    first['body'] = json.dumps(body)
    first['sha256'] = hashlib.sha256(first['body'].encode()).hexdigest()
    report = module.replay(source, datetime(2026, 10, 10, 16, tzinfo=UTC))
    assert not report['stocks'][0]['complete_annual_rows']
    assert 'annual_period_set_mismatch' in report['stocks'][0]['reasons']
