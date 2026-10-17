"""Eastmoney annual main-indicator adapter for banks.

This narrowly scoped adapter requests one explicitly selected ``SECUCODE`` and
December 31 period from ``RPT_F10_FINANCE_MAINFINADATA``. It reuses the shared
Eastmoney transport pipeline for caching, throttling, retries, and request logs.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from investment_agent.providers.akshare_eastmoney import to_eastmoney_symbol
from investment_agent.providers.eastmoney_annual import (
    DATACENTER_BASE_URL,
    MAIN_FINADATA_PATH,
    MAIN_FINADATA_REPORT,
    PERCENT,
    AnnualField,
    CompanyAnnualCollection,
    EastmoneyAnnualCollector,
    EastmoneyAnnualDataError,
    EastmoneyAnnualSchemaError,
    ParsedStatement,
    SourceRowMeta,
    _decimal_string,
    _decimal_value,
    _sha256,
    parse_json_body,
)

BANK_ADAPTER_VERSION = "bank-annual-v1"
BANK_ANNUAL_METRICS = (
    "roe_weighted_annual",
    "net_interest_margin",
    "npl_ratio",
    "provision_coverage",
    "equity_parent",
)

_BANK_SOURCE_FIELDS = {
    "roe_weighted_annual": "ROEJQ",
    "net_interest_margin": "NET_INTEREST_MARGIN",
    "npl_ratio": "NONPERLOAN",
    "provision_coverage": "BLDKBBL",
}
_BANK_LABELS = frozenset({"银行", "银行业"})


def _strict_date(raw: Any, *, field_name: str) -> date:
    """Accept only an ISO date or an ISO timestamp, without prefix truncation."""
    try:
        if isinstance(raw, str):
            parsed = datetime.fromisoformat(raw.strip())
            return parsed.date()
        if isinstance(raw, date) and not isinstance(raw, datetime):
            return raw
    except ValueError:
        pass
    raise EastmoneyAnnualSchemaError(
        f"eastmoney bank_main_fina: invalid {field_name} {raw!r}", provider="eastmoney"
    )


def _strict_optional_date(raw: Any, *, field_name: str) -> date | None:
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        return None
    return _strict_date(raw, field_name=field_name)


def parse_bank_main_indicators(
    body: bytes, *, secucode: str, wanted: date, response_hash: str
) -> tuple[ParsedStatement, str]:
    """Parse a complete, single-row bank annual response without inference."""
    payload = parse_json_body(body, operation="bank_main_fina")
    if not isinstance(payload, dict) or payload.get("success") is not True:
        raise EastmoneyAnnualSchemaError(
            "eastmoney bank_main_fina: response is not a successful JSON object",
            provider="eastmoney",
        )
    source_version = payload.get("version")
    if source_version is not None and not isinstance(source_version, str):
        raise EastmoneyAnnualSchemaError(
            f"bank response version is not a string: {source_version!r}", provider="eastmoney"
        )
    result = payload.get("result")
    if result is None:
        return ParsedStatement(row=None), ""
    if not isinstance(result, dict):
        raise EastmoneyAnnualSchemaError("bank result is not an object", provider="eastmoney")
    pages = result.get("pages")
    if not isinstance(pages, int) or isinstance(pages, bool) or pages not in (0, 1):
        raise EastmoneyAnnualSchemaError(
            f"bank result pagination must be zero or one page, got {pages!r}",
            provider="eastmoney",
        )
    rows = result.get("data")
    if not isinstance(rows, list):
        raise EastmoneyAnnualSchemaError("bank result.data is not a list", provider="eastmoney")
    if not rows:
        return ParsedStatement(row=None), ""
    if pages != 1:
        raise EastmoneyAnnualSchemaError(
            "bank response has rows but pagination reports no page", provider="eastmoney"
        )
    if len(rows) != 1 or not isinstance(rows[0], dict):
        raise EastmoneyAnnualDataError(
            "bank annual request returned duplicate or malformed rows", provider="eastmoney"
        )
    row: Mapping[str, Any] = rows[0]
    required = (
        "SECUCODE",
        "REPORT_DATE",
        "REPORT_TYPE",
        "NOTICE_DATE",
        "UPDATE_DATE",
        "CURRENCY",
        "ORG_TYPE",
        *_BANK_SOURCE_FIELDS.values(),
    )
    missing = [field for field in required if field not in row]
    if missing:
        raise EastmoneyAnnualSchemaError(
            f"bank annual row misses required columns: {missing}", provider="eastmoney"
        )
    if str(row["SECUCODE"]).strip().upper() != secucode.upper():
        raise EastmoneyAnnualSchemaError(
            "bank response issuer does not match request", provider="eastmoney"
        )
    report_date = _strict_date(row["REPORT_DATE"], field_name="REPORT_DATE")
    if report_date != wanted or (wanted.month, wanted.day) != (12, 31):
        raise EastmoneyAnnualSchemaError(
            "bank response period does not match annual request", provider="eastmoney"
        )
    report_type = str(row["REPORT_TYPE"] or "").strip()
    if report_type not in {"年报", f"{wanted.year}年年报"}:
        raise EastmoneyAnnualSchemaError(
            f"bank row is not an annual report: {report_type!r}", provider="eastmoney"
        )
    if row["CURRENCY"] != "CNY":
        raise EastmoneyAnnualDataError(
            f"bank annual currency is not CNY: {row['CURRENCY']!r}", provider="eastmoney"
        )
    org_type = str(row["ORG_TYPE"] or "").strip()
    if org_type not in _BANK_LABELS:
        raise EastmoneyAnnualSchemaError(
            f"issuer is not classified as a bank: {org_type!r}", provider="eastmoney"
        )
    # Required dates may be null upstream; preserve that absence rather than inventing a date.
    notice = _strict_optional_date(row["NOTICE_DATE"], field_name="NOTICE_DATE")
    update = _strict_optional_date(row["UPDATE_DATE"], field_name="UPDATE_DATE")
    if notice is None:
        raise EastmoneyAnnualSchemaError("bank annual row has no notice date", provider="eastmoney")
    values = {
        source: _decimal_value(row[source], field_name=source, operation="bank_main_fina")
        for source in _BANK_SOURCE_FIELDS.values()
    }
    meta = SourceRowMeta(
        source_table="main",
        report_period=report_date,
        notice_date=notice,
        update_date=update,
        currency="CNY",
        response_hash=response_hash,
        source_version=source_version,
    )
    return ParsedStatement(row=meta, values=values), org_type


class EastmoneyBankAnnualCollector(EastmoneyAnnualCollector):
    """Collect five canonical bank fields for one explicitly named company-year."""

    async def collect_company_annual(
        self, ticker: str, report_period: date, *, company_type: str | None = None
    ) -> CompanyAnnualCollection:
        del company_type  # Bank classification comes from the validated response row.
        to_eastmoney_symbol(ticker)  # Validate the canonical exchange-qualified symbol.
        secucode = ticker.strip().upper()
        if (report_period.month, report_period.day) != (12, 31):
            raise ValueError(f"not an annual report period: {report_period.isoformat()}")
        captured_at = self._now()
        log_start = len(self.request_log)
        params = {
            "reportName": MAIN_FINADATA_REPORT,
            "columns": "ALL",
            "filter": f"(SECUCODE=\"{secucode}\")(REPORT_DATE in ('{report_period.isoformat()}'))",
            "pageSize": "100",
            "pageNumber": "1",
            "sortTypes": "-1",
            "sortColumns": "REPORT_DATE",
            "source": "HSF10",
            "client": "PC",
        }

        def parse(response: Any) -> tuple[ParsedStatement, str]:
            return parse_bank_main_indicators(
                response.body,
                secucode=secucode,
                wanted=report_period,
                response_hash=_sha256(response.body),
            )

        parsed, org_type = await self._logical_step(
            "bank_main_fina", f"{DATACENTER_BASE_URL}{MAIN_FINADATA_PATH}", params, parse
        )
        if parsed.row is not None:
            local_capture_date = captured_at.astimezone(ZoneInfo("Asia/Shanghai")).date()
            if parsed.row.notice_date is not None and parsed.row.notice_date > local_capture_date:
                raise EastmoneyAnnualSchemaError(
                    "bank notice date is later than the capture date", provider="eastmoney"
                )
            if parsed.row.update_date is not None and parsed.row.update_date > local_capture_date:
                raise EastmoneyAnnualSchemaError(
                    "bank update date is later than the capture date", provider="eastmoney"
                )
        fields: dict[str, AnnualField] = {}
        for metric in BANK_ANNUAL_METRICS:
            source = _BANK_SOURCE_FIELDS.get(metric)
            if source is None:
                fields[metric] = AnnualField(
                    metric=metric,
                    source_table="main",
                    source_field="",
                    value=None,
                    unit="CNY",
                    currency="CNY",
                    missing_reason="unverified_mapping_reconcile_annual_report",
                )
                continue
            raw = parsed.values[source] if parsed.row is not None else None
            percent = True
            unit = "fraction"
            if parsed.row is None:
                fields[metric] = AnnualField(
                    metric=metric,
                    source_table="main",
                    source_field=source,
                    value=None,
                    unit=unit,
                    currency=None,
                    missing_reason="no_annual_row",
                    raw_unit="percent",
                )
            elif raw is None:
                fields[metric] = AnnualField(
                    metric=metric,
                    source_table="main",
                    source_field=source,
                    value=None,
                    unit=unit,
                    currency=None,
                    missing_reason="upstream_null",
                    raw_unit="percent",
                    response_hash=parsed.row.response_hash,
                )
            else:
                assert isinstance(raw, Decimal)
                fields[metric] = AnnualField(
                    metric=metric,
                    source_table="main",
                    source_field=source,
                    value=raw / PERCENT if percent else raw,
                    unit=unit,
                    currency=None,
                    raw_value=_decimal_string(raw),
                    raw_unit="percent",
                    response_hash=parsed.row.response_hash,
                )
        return CompanyAnnualCollection(
            ticker=secucode,
            secucode=secucode,
            report_period=report_period,
            company_type=org_type,
            company_type_source="bank_main_metadata",
            captured_at=captured_at,
            adapter_version=BANK_ADAPTER_VERSION,
            fields=fields,
            source_rows={"main": parsed.row} if parsed.row is not None else {},
            requests=tuple(self.request_log[log_start:]),
        )


__all__ = [
    "BANK_ADAPTER_VERSION",
    "BANK_ANNUAL_METRICS",
    "EastmoneyBankAnnualCollector",
    "parse_bank_main_indicators",
]
