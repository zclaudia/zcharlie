"""AG11 bounded source diagnostics. Candidates are never SavedEvidence or qualifications.

Supplier field names are retained as candidates until annual-report/accounting
verification supplies the separate checks. No provider cash-interest mapping is
approved: ASSIGN_DIVIDEND_PORFIT and PAY_INTEREST_COMMISSION cannot substitute.
"""
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from investment_agent.clock import SHANGHAI_TZ

MAPPING_VERSION = 'eastmoney.fcfe_candidate.v1'
FIELDS = {'cfo': 'NETCASH_OPERATE', 'capex': 'CONSTRUCT_LONG_ASSET',
          'borrowings': 'RECEIVE_LOAN_CASH', 'bonds': 'ISSUE_BOND',
          'repayments': 'PAY_DEBT_CASH'}


@dataclass(frozen=True)
class AccountingChecks:
    """Trusted source review inputs, never model or HTTP request parameters.

    This candidate API does not issue a production qualification. Cash interest
    must be tied to separate verifiable evidence by any future evidence adapter.
    """
    cash_interest: Decimal | None = None
    interest_in_cfo: bool | None = None
    financing_disjoint: bool = False
    ordinary_equity: bool = False
    consolidated_scope: bool = False
    non_operating_consistent: bool = False
    material_changes_handled: bool = False
    units_verified: bool = False


def number(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        result = Decimal(str(value))
    except InvalidOperation:
        return None
    return result if result.is_finite() else None


def assess_year(row: Mapping[str, Any], *, as_of: datetime,
                captured_at: datetime, expected_code: str, financial: bool = False,
                checks: AccountingChecks | None = None) -> dict[str, Any]:
    if as_of.tzinfo is None or captured_at.tzinfo is None:
        raise ValueError("aware timestamps required")
    checks = checks or AccountingChecks()
    reasons: list[str] = []
    if row.get('SECURITY_CODE') != expected_code:
        reasons.append('subject_mismatch')
    if financial:
        reasons.append('financial_sector_excluded')
    values = {name: number(row.get(field)) for name, field in FIELDS.items()}
    for name, value in values.items():
        if value is None:
            reasons.append(f'missing_{name}')
        elif name != 'cfo' and value < 0:
            reasons.append(f'invalid_sign_{name}')
    period = str(row.get('REPORT_DATE', ''))[:10]
    try:
        report_day = date.fromisoformat(period)
    except ValueError:
        reasons.append('invalid_report_period')
    else:
        if (report_day.month, report_day.day) != (12, 31):
            reasons.append('incomplete_annual_period')
        if report_day > as_of.astimezone(SHANGHAI_TZ).date():
            reasons.append('future_report_period')
    if captured_at > as_of:
        reasons.append('capture_after_as_of')
    for field in ('NOTICE_DATE', 'UPDATE_DATE'):
        raw = row.get(field)
        try:
            day = datetime.fromisoformat(str(raw)).date()
        except ValueError:
            reasons.append(f'missing_or_invalid_{field.lower()}')
        else:
            if day > min(as_of.astimezone(SHANGHAI_TZ).date(),
                         captured_at.astimezone(SHANGHAI_TZ).date()):
                reasons.append(f'future_{field.lower()}')
    if row.get('CURRENCY') != 'CNY':
        reasons.append('currency_unverified')
    if checks.cash_interest is None:
        reasons.append('cash_interest_unverified')
    elif not checks.cash_interest.is_finite() or checks.cash_interest < 0:
        reasons.append('invalid_cash_interest')
    if checks.interest_in_cfo is None:
        reasons.append('interest_classification_unverified')
    for name in ('financing_disjoint', 'ordinary_equity', 'consolidated_scope',
                 'non_operating_consistent', 'material_changes_handled', 'units_verified'):
        if not getattr(checks, name):
            reasons.append(f'{name}_unverified')
    fcfe = None
    if not reasons:
        assert all(value is not None for value in values.values())
        assert checks.cash_interest is not None
        known = {name: value for name, value in values.items() if value is not None}
        fcfe = (known['cfo'] - known['capex'] + known['borrowings']
                + known['bonds'] - known['repayments'])
        if not checks.interest_in_cfo:
            fcfe -= checks.cash_interest
    return {'period': period, 'mapping_version': MAPPING_VERSION,
            'raw_fields_available': all(value is not None for value in values.values()),
            'values': {key: str(value) if value is not None else None
                       for key, value in values.items()},
            'fcfe_candidate': str(fcfe) if fcfe is not None else None,
            'accounting_usable': not reasons, 'reasons': reasons,
            'primary_reason': reasons[0] if reasons else None}
