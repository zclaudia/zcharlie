"""Bounded public read-only AG11 capture/replay; never opens a database.

Exactly four named samples, one request each, 20 second timeout, no retry or
pagination. Raw response bodies remain audit artifacts, never verified facts.
"""
import argparse
import hashlib
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx

from investment_agent.valuation.fcfe import FIELDS, assess_year

SAMPLES = (('SH600519', '4', 'consumer'), ('SZ000333', '4', 'appliances'),
           ('SH600900', '4', 'utilities'), ('SH601398', '3', 'bank_excluded'))
URL = 'https://emweb.securities.eastmoney.com/PC_HSF10/NewFinanceAnalysis/xjllbAjaxNew'


def capture() -> dict[str, Any]:
    captures = []
    for symbol, kind, _ in SAMPLES:
        params = dict(companyType=kind, reportDateType='0', reportType='1',
                      dates='2025-12-31,2024-12-31,2023-12-31', code=symbol)
        item: dict[str, Any] = dict(symbol=symbol, url=URL, params=params,
                                    captured_at=datetime.now(UTC).isoformat())
        try:
            response = httpx.get(URL, params=params, timeout=20, follow_redirects=False)
            # Store text and hash in the same reproducible UTF-8 representation.
            item.update(status_code=response.status_code, body=response.text,
                        sha256=hashlib.sha256(response.text.encode()).hexdigest())
        except httpx.HTTPError as exc:
            item['error'] = type(exc).__name__
        captures.append(item)
    return dict(schema_version='ag11.capture.v1', request_count=4, max_requests=4,
                timeout_seconds=20, retries=0, captures=captures)


def replay(source: dict[str, Any], as_of: datetime) -> dict[str, Any]:
    if [item['symbol'] for item in source['captures']] != [s[0] for s in SAMPLES]:
        raise ValueError('fixed sample denominator changed')
    stocks = []
    for item, (_, _, industry) in zip(source['captures'], SAMPLES, strict=True):
        reasons = []
        years = []
        field_diagnostics = []
        body = item.get('body')
        if body is not None and hashlib.sha256(body.encode()).hexdigest() != item['sha256']:
            raise ValueError('raw response hash mismatch')
        if item.get('status_code') != 200:
            reasons.append('source_request_failed')
        else:
            try:
                rows = json.loads(body, parse_float=Decimal)['data']
                if not isinstance(rows, list):
                    raise ValueError('not rows')
                for row in rows:
                    if row.get('SECURITY_CODE') != item['symbol'][2:]:
                        raise ValueError('wrong subject')
                    field_diagnostics.append(dict(period=str(row.get('REPORT_DATE'))[:10],
                        fields={field: ('absent' if field not in row else
                                        'null' if row[field] is None else 'value')
                                for field in (*FIELDS.values(), 'ASSIGN_DIVIDEND_PORFIT')}))
                    years.append(assess_year(row, as_of=as_of,
                                             captured_at=datetime.fromisoformat(item['captured_at']),
                                             expected_code=item['symbol'][2:],
                                             financial=industry == 'bank_excluded'))
                if sorted(y['period'] for y in years) != ['2023-12-31', '2024-12-31', '2025-12-31']:
                    reasons.append('annual_period_set_mismatch')
            except (ValueError, KeyError, TypeError):
                years = []
                field_diagnostics = []
                reasons.append('source_schema_invalid')
        if industry == 'bank_excluded':
            reasons.insert(0, 'financial_sector_excluded')
        for year in years:
            reasons.extend(year['reasons'])
        reasons.extend(['ordinary_equity_market_value_unverified',
                        'no_approved_saved_evidence_adapter'])
        reasons = list(dict.fromkeys(reasons))
        stocks.append(dict(symbol=item['symbol'], industry=industry, years=years,
                           request_succeeded=item.get('status_code') == 200,
                           complete_annual_rows=(sorted(y['period'] for y in years)
                                                 == ['2023-12-31', '2024-12-31', '2025-12-31']),
                           field_diagnostics=field_diagnostics,
                           raw_fields_available=len(years) == 3 and all(y['raw_fields_available'] for y in years),
                           accounting_usable=False, method_applicable=False,
                           numerical_success=False, reasons=reasons, primary_reason=reasons[0],
                           raw_sha256=item.get('sha256'), captured_at=item['captured_at']))
    return dict(schema_version='ag11.v3b.v1', as_of=as_of.isoformat(), denominator=4,
                scope='fixed representative sample; not CSI300 coverage', years=[2023, 2024, 2025],
                raw_fields_definition='three annual non-null CFO/capex/borrowings/bonds/repayments candidate columns; cash interest separately unverified',
                cash_interest_mapping_approved=False,
                summary=dict(raw_fields_available=sum(s['raw_fields_available'] for s in stocks),
                             accounting_usable=0, method_applicable=0, numerical_success=0),
                production_reverse_dcf_enabled=False, stocks=stocks)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--capture', type=Path)
    parser.add_argument('--replay', type=Path)
    parser.add_argument('--report', type=Path, required=True)
    parser.add_argument('--as-of', type=datetime.fromisoformat, required=True)
    args = parser.parse_args()
    if bool(args.capture) == bool(args.replay) or args.as_of.tzinfo is None:
        parser.error('choose capture OR replay; as-of requires timezone')
    if args.capture:
        if args.capture.exists() or args.report.exists():
            parser.error('capture outputs already exist; choose new paths')
        source = capture()
        args.capture.write_text(json.dumps(source, ensure_ascii=False, indent=2)+'\n')
    else:
        source = json.loads(args.replay.read_text())
    report = replay(source, args.as_of)
    args.report.write_text(json.dumps(report, ensure_ascii=False, indent=2)+'\n')
    print(json.dumps(report['summary']))


if __name__ == '__main__':
    main()
