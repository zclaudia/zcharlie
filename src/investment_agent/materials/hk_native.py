"""Eastmoney native-currency HK statements as a controlled material source.

Task 03 (``docs/product/plans/hk-us-expansion/03-hk-native-adapter.md``): turn
the three hand-verified native-currency endpoints into
:class:`~investment_agent.materials.contracts.DatasetDraft` values of kind
``ANNUAL_FACTS``. Requests mirror the checked probe
(``research/market-data-validation/scripts/native_hk_probe.py``): ``columns=ALL``,
the controlled ``SECUCODE``, a bounded ``REPORT_DATE`` window and
``REPORT_TYPE="年报"``, ``pageNumber=1``, ``pageSize=500``.

Evidence and reconciliation authority (read-only, retained outside the
repository): ``research/market-data-validation/evidence/remaining-final-03/raw/
native/{00005,00002,00700}/*.json`` and ``remaining-validation.md`` §1–2. The 18
amounts used below were reconciled field by field against the issuers' official
documents (see :data:`OFFICIAL_DOCUMENTS`), and the two HSBC definition
differences (revenue after expected credit losses; net purchases/sales instead
of gross capex) are recorded in ``derived/native-hk/result.json``.

Data discipline carried by this module:

* Amounts stay in the endpoint's currency and unit: ``raw_value`` is the
  provider's integer amount as a decimal string, ``raw_unit="元"``, ``scale=1``,
  ``unit``/``currency`` = ``identity.report_currency``. The observed CNY display
  multiplier 7.1884 is a presentation artifact and is never applied.
* Missing is never zero and never reconstructed. HSBC's ``revenue_gross``,
  ``credit_loss_expense`` and ``equity_ordinary`` are absent from this issuer's
  line set, so every HSBC income/balance draft carries them as
  ``raw_value=None`` + ``missing_reason`` (see :data:`EXPLICIT_MISSING`); no
  7.1884 conversion and no subtraction of provider lines is performed.
* The three ``capex_ppe`` lines are *not* the same measure; the per-issuer
  meanings live in :data:`CAPEX_SIGN_CONVENTIONS` and are never merged.

Structure checks (each failure is reported in ``ParseResult.problems`` and the
affected table yields no drafts):

1. body must be a JSON object (``not_json``; an HTML body counts as not JSON);
2. ``success is True`` (``upstream_success_false``; HTTP 200 + error object is
   a failure);
3. ``result.data`` must be a list (``missing_result_data``);
4. pagination complete: ``pages == 1`` and ``len(data) == result.count``
   (``pagination_truncated``);
5. one currency per table, equal to ``identity.report_currency`` — income and
   balance from ``CURRENCY_CODE``, cashflow from the nested ``CURRENCY_ABBR``
   (``currency_mismatch``);
6. reported periods equal the requested periods (``unexpected_period``;
   cashflow outer ``REPORT_DATE`` included);
7. ``STD_ITEM_CODE`` unique within a period (``duplicate_item_code``);
8. cashflow must be outer report rows carrying ``VALUE_LIST``
   (``row_shape_invalid``); any ``RPT_HKF10_FN_INCOME_PC`` / ``MAININDICATOR`` /
   ``_PC`` marker rejects the whole batch (``display_currency_source_rejected``,
   hard red line — display-currency values are not an amount source);
9. detail rows outside :data:`ITEM_MAP` yield no fact and are counted once per
   table as ``unmapped_items: <table>/<n>``.

Defensive extras beyond the nine (same reporting channel): ``missing_raw`` /
``duplicate_raw`` for the raw payload set, ``invalid_amount`` for a mapped row
whose ``AMOUNT`` is not numeric, and ``unsupported_period`` for a requested
period that is not an annual one. A mapped row with a null ``AMOUNT`` is not an
error: it becomes an explicit missing fact (``missing_reason`` set), never 0.

Drafts: one per company x table x report period
(``kind=ANNUAL_FACTS``, ``subject_kind=COMPANY``, ``subject_id`` given by the
caller, ``period`` = the requested annual period, ``captured_at`` = real now).
Fact payload entries are produced only through
``contracts.fact_to_payload`` (plan 02, frozen). ``parse`` needs the target
company id as ``subject_id`` because a :class:`ControlledIdentity` carries a
``company_key`` but no row id: callers resolve ``company_key -> Company.id``
(tasks 05/06 own that lookup). ``parse`` raises ``ValueError`` when no id is
given — producing a draft with a fabricated id is not an option.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from investment_agent.domain.security import ControlledIdentity, source_symbol
from investment_agent.materials.contracts import (
    SCHEMA_VERSION,
    AccountingStandard,
    DatasetDraft,
    DatasetKind,
    DatePrecision,
    EquityScope,
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactSemantics,
    FactValue,
    ParseResult,
    PeriodBasis,
    PerShareBasis,
    RawPayload,
    ReportingScope,
    SubjectKind,
    UpstreamRequest,
    Verification,
    fact_to_payload,
)

SOURCE = "eastmoney.hk.native.v1"
ADAPTER_VERSION = "1"

BASE = "https://datacenter.eastmoney.com/securities/api/data/v1/get"

#: Probe-verified report names; the guessed ``RPT_HKF10_FN_CASHFLOW*`` names
#: never worked and stay unmodelled (see the saved discovery evidence).
REPORTS: dict[str, str] = {
    "income": "RPT_HKF10_FN_INCOME",
    "balance": "RPT_HKF10_FN_BALANCE",
    "cashflow": "RPT_CUSTOM_HKSK_APPFN_CASHFLOW",
}

REPORT_TYPE = "年报"

#: (report, STD_ITEM_CODE) -> canonical metric. Only fields reconciled against
#: official documents are mapped; everything else stays unmapped and counted.
ITEM_MAP: dict[tuple[str, str], str] = {
    ("income", "004001999"): "revenue_operating",  # 营运收入 (CLP / Tencent)
    ("income", "001003999"): "revenue_net_credit",  # 汇丰 经营收入总额（信用损失后）
    ("income", "004025002"): "profit_ordinary_shareholders",  # 股东应占溢利
    ("income", "001025002"): "profit_ordinary_shareholders",  # 汇丰 股东应占溢利
    ("balance", "004009999"): "total_assets",
    ("balance", "001001999"): "total_assets",
    ("balance", "004030999"): "equity_parent",
    ("balance", "001009999"): "equity_parent",
    ("cashflow", "003999"): "operating_cash_flow",
    ("cashflow", "005005"): "capex_ppe",  # 三家均为正数支出规模，定义并不相同
}

#: The four-year window covered by the retained evidence
#: (``native/<code>-four-years.json``, 2021-01-01..2024-12-31). Callers asking
#: for other windows pass explicit ``periods``.
DEFAULT_PERIODS: tuple[FactPeriod, ...] = tuple(
    FactPeriod(start=date(year, 1, 1), end=date(year, 12, 31), basis=PeriodBasis.ANNUAL)
    for year in range(2021, 2025)
)

#: Display sources that are never an amount source (hard red line). The saved
#: native responses contain none of these markers; the check is a pre-pass over
#: every raw body before any table is read.
DISPLAY_MARKERS: tuple[str, ...] = ("RPT_HKF10_FN_INCOME_PC", "MAININDICATOR", "_PC")

_ACCOUNTING_STANDARDS: dict[str, AccountingStandard] = {
    "国际会计准则": AccountingStandard.IFRS,
    "香港会计准则": AccountingStandard.HKFRS,
}

#: Per-issuer capex meaning. The three lines are different measures and must not
#: be merged; plan 08's FCF whitelist keys on this text ("capital expenditure"
#: is the only currently eligible one). HSBC's line is a bank's net
#: purchases/sales figure, hence also not applicable as a capex measure.
CAPEX_SIGN_CONVENTIONS: dict[str, str] = {
    "hsbc-holdings": (
        "net purchases and sales of fixed assets, positive spend magnitude; "
        "not applicable as a capex measure for a bank (the line is net of disposals)"
    ),
    "clp-holdings": "capital expenditure, positive spend magnitude",
    "tencent-holdings": (
        "purchases and prepayments incl. property/construction in progress/"
        "investment property, positive spend magnitude"
    ),
}

#: Default sign convention for amounts taken as reported (the endpoint returns
#: the statement value's sign; no reinterpretation happens here).
_AS_REPORTED = "as reported by the provider (no sign reinterpretation)"

_MISSING_SIGN = "n/a (value missing from this source)"

#: Issuers whose capex line is not an applicable measure for the company profile.
CAPEX_APPLICABILITY: dict[str, str] = {"hsbc-holdings": "not_applicable"}

_MISSING_REVENUE_GROSS = (
    "not carried by this endpoint: the provider income detail lists only 经营收入总额 "
    "(net operating income, after expected credit losses); revenue before expected credit "
    "losses is absent (FY2024 official 65,854m USD) and is not reconstructed here"
)
_MISSING_CREDIT_LOSS = (
    "not carried by this endpoint: expected credit losses are not a line in the provider "
    "income detail (FY2024 official 3,414m USD); the value is not reconstructed by "
    "subtracting provider lines"
)
_MISSING_EQUITY_ORDINARY = (
    "not obtainable from this issuer's balance detail: 归属于母公司股东权益 (all parent "
    "equity holders) includes other equity instruments (perpetual capital securities); "
    "ordinary shareholders' equity is not separately reported by this endpoint"
)


@dataclass(frozen=True)
class _MissingMetric:
    metric: str
    reason: str


#: Hand-checked provider-layout gaps: metrics that apply to the issuer but are
#: absent from the endpoint's line set and must still appear in the facts list.
EXPLICIT_MISSING: dict[str, dict[str, tuple[_MissingMetric, ...]]] = {
    "hsbc-holdings": {
        "income": (
            _MissingMetric("revenue_gross", _MISSING_REVENUE_GROSS),
            _MissingMetric("credit_loss_expense", _MISSING_CREDIT_LOSS),
        ),
        "balance": (_MissingMetric("equity_ordinary", _MISSING_EQUITY_ORDINARY),),
    }
}


@dataclass(frozen=True)
class _OfficialDocument:
    """The issuer document the FY2024 amounts were reconciled against.

    ``pages`` is metric -> 1-based PDF page, taken from the probe's field table;
    a locator is only attached to facts of :attr:`period_end` (the documented
    fiscal year), never to other years.
    """

    url: str
    period_end: date
    pages: Mapping[str, int]


OFFICIAL_DOCUMENTS: dict[str, _OfficialDocument] = {
    "hsbc-holdings": _OfficialDocument(
        url=(
            "https://www.hsbc.com/-/files/hsbc/investors/hsbc-results/2024/annual/pdfs/"
            "hsbc-holdings-plc/250219-annual-results-2024-media-release.pdf?download=1"
        ),
        period_end=date(2024, 12, 31),
        pages={
            "revenue_net_credit": 12,
            "profit_ordinary_shareholders": 12,
            "total_assets": 14,
            "equity_parent": 14,
            "operating_cash_flow": 18,
            "capex_ppe": 18,
        },
    ),
    "clp-holdings": _OfficialDocument(
        url=(
            "https://www.clpgroup.com/content/dam/clp-group/channels/investor/document/"
            "3-3-financial-reports/2024/e_2024E113.pdf.coredownload.pdf"
        ),
        period_end=date(2024, 12, 31),
        pages={
            "revenue_operating": 16,
            "profit_ordinary_shareholders": 16,
            "total_assets": 87,
            "equity_parent": 18,
            "operating_cash_flow": 20,
            "capex_ppe": 20,
        },
    ),
    "tencent-holdings": _OfficialDocument(
        url="https://www.hkexnews.hk/listedco/listconews/sehk/2025/0408/2025040800667.pdf",
        period_end=date(2024, 12, 31),
        pages={
            "revenue_operating": 124,
            "profit_ordinary_shareholders": 124,
            "total_assets": 126,
            "equity_parent": 127,
            "operating_cash_flow": 133,
            "capex_ppe": 133,
        },
    ),
}

#: Metric-level scope qualifiers that are not "unknown".
_EQUITY_SCOPES: dict[str, EquityScope] = {
    "equity_parent": EquityScope.ALL_PARENT,
    "equity_ordinary": EquityScope.ORDINARY,
    "profit_ordinary_shareholders": EquityScope.ORDINARY,
}


@dataclass(frozen=True)
class HkNativeAdapter:
    """Requests/parse for the three native-currency HK statement endpoints."""

    source: str = SOURCE
    adapter_version: str = ADAPTER_VERSION

    def requests(
        self, identity: ControlledIdentity, *, periods: Sequence[FactPeriod]
    ) -> tuple[UpstreamRequest, ...]:
        """One bounded request per report; ``SECUCODE`` comes from ``identity``.

        An empty ``periods`` means :data:`DEFAULT_PERIODS` (the retained
        four-year evidence window). The leading zeros of the Eastmoney code are
        the pilot list's, never padded locally: an identity without a binding
        for this source raises ``ValueError``.
        """

        secucode = source_symbol(identity, self.source)
        if secucode is None:
            raise ValueError(
                f"{identity.market_code}/{identity.ticker} has no {self.source!r} binding; "
                "add it to the checked pilot list instead of deriving a code here"
            )
        window = tuple(periods) if periods else DEFAULT_PERIODS
        start = min(period.start or period.end for period in window)
        end = max(period.end for period in window)
        filter_text = (
            f'(SECUCODE="{secucode}")'
            f"(REPORT_DATE>='{start.isoformat()}')"
            f"(REPORT_DATE<='{end.isoformat()}')"
            f'(REPORT_TYPE="{REPORT_TYPE}")'
        )
        return tuple(
            UpstreamRequest(
                name=name,
                url=BASE,
                params=(
                    ("reportName", report_name),
                    ("columns", "ALL"),
                    ("filter", filter_text),
                    ("pageNumber", "1"),
                    ("pageSize", "500"),
                ),
            )
            for name, report_name in REPORTS.items()
        )

    def parse(
        self,
        identity: ControlledIdentity,
        *,
        raw: Sequence[RawPayload],
        periods: Sequence[FactPeriod],
        subject_id: int | None = None,
    ) -> ParseResult:
        """Validate the three saved responses and build one draft set per table.

        ``subject_id`` is the target ``Company.id`` (a ``ControlledIdentity``
        has no row id); callers resolve it from ``identity.company_key``. When
        omitted, an enriched identity carrying ``company_id`` is accepted too;
        with neither, this raises ``ValueError`` rather than inventing an id.
        """

        company_id = subject_id if subject_id is not None else getattr(identity, "company_id", None)
        if not isinstance(company_id, int) or isinstance(company_id, bool) or company_id <= 0:
            raise ValueError(
                "HkNativeAdapter.parse needs the target company id: pass subject_id=<Company.id> "
                "(resolved by the caller from identity.company_key); the controlled identity "
                "itself carries no row id"
            )
        rejected = tuple(
            f"http_status_rejected: {item.name}/{item.http_status}"
            for item in raw if item.http_status != 0 and not 200 <= item.http_status < 300
        )
        if rejected:
            return ParseResult(drafts=(), problems=rejected)

        captured_at = datetime.now(UTC)
        problems: list[str] = []
        payloads: dict[str, RawPayload] = {}
        for item in raw:
            if item.name in payloads:
                problems.append(f"duplicate_raw: {item.name}")
                continue
            payloads[item.name] = item
        for item in raw:
            if _has_display_marker(item.content):
                # Hard red line: display-currency values are not an amount source.
                return ParseResult(
                    drafts=(), problems=(f"display_currency_source_rejected: {item.name}",)
                )

        requested = tuple(periods) if periods else DEFAULT_PERIODS
        annual: list[FactPeriod] = []
        for period in requested:
            if period.basis is PeriodBasis.ANNUAL and period.start is not None:
                annual.append(period)
            else:
                problems.append(
                    f"unsupported_period: {period.end.isoformat()} has basis {period.basis}; "
                    "the HK annual endpoints only serve annual reports"
                )
        if not annual:
            return ParseResult(drafts=(), problems=tuple(problems))
        expected_ends = {period.end for period in annual}
        secucode = source_symbol(identity, self.source)
        if secucode is None:
            return ParseResult(
                drafts=(),
                problems=tuple(problems + [f"source_symbol_missing: no {self.source!r} binding"]),
            )

        drafts: list[DatasetDraft] = []
        for name, report_name in REPORTS.items():
            raw_item = payloads.get(name)
            if raw_item is None:
                problems.append(f"missing_raw: {name}")
                continue
            parsed = _json_object(raw_item.content)
            if parsed is None:
                problems.append(f"not_json: {name}")
                continue
            if parsed.get("success") is not True:
                problems.append(f"upstream_success_false: {name}")
                continue
            result = parsed.get("result")
            if not isinstance(result, Mapping):
                problems.append(f"missing_result_data: {name}")
                continue
            data = result.get("data")
            if not isinstance(data, list):
                problems.append(f"missing_result_data: {name}")
                continue
            count = result.get("count")
            pages = result.get("pages")
            if (
                not isinstance(count, int)
                or isinstance(count, bool)
                or not isinstance(pages, int)
                or isinstance(pages, bool)
                or pages != 1
                or len(data) != count
            ):
                problems.append(f"pagination_truncated: {name}")
                continue
            rows, outer_ends = _detail_rows(name, data)
            if rows is None:
                problems.append(f"row_shape_invalid: {name}")
                continue
            if not rows:
                problems.append(f"missing_result_data: {name}")
                continue
            currencies = sorted({row.currency for row in rows}, key=str)
            if currencies != [identity.report_currency]:
                problems.append(
                    f"currency_mismatch: {name} provider {currencies} != report_currency "
                    f"{identity.report_currency!r}"
                )
                continue
            observed = {row.period_end for row in rows}
            if outer_ends is not None:
                observed |= outer_ends
            if observed != expected_ends:
                problems.append(
                    f"unexpected_period: {name} reported "
                    f"{sorted(_period_label(value) for value in observed)} != requested "
                    f"{sorted(end.isoformat() for end in expected_ends)}"
                )
                continue
            duplicate = _duplicate_code(rows)
            if duplicate is not None:
                problems.append(f"duplicate_item_code: {name} {duplicate}")
                continue

            unmapped_total = sum(
                1 for row in rows if row.code is None or (name, row.code) not in ITEM_MAP
            )
            if unmapped_total:
                problems.append(f"unmapped_items: {name}/{unmapped_total}")

            source_version = parsed.get("version")
            version_text = (
                source_version if isinstance(source_version, str) and source_version else None
            )
            original_hash = hashlib.sha256(raw_item.content).hexdigest()
            for period in annual:
                period_rows = [row for row in rows if row.period_end == period.end]
                facts: list[FactRecord] = []
                unmapped_period = 0
                for row in period_rows:
                    metric = ITEM_MAP.get((name, row.code)) if row.code is not None else None
                    if metric is None:
                        unmapped_period += 1
                        continue
                    fact = _fact_for_row(
                        name=name,
                        metric=metric,
                        row=row,
                        company_key=identity.company_key,
                        report_currency=identity.report_currency,
                        original_hash=original_hash,
                        source_version=version_text,
                        problems=problems,
                    )
                    if fact is not None:
                        facts.append(fact)
                facts.extend(
                    _explicit_missing_facts(
                        name=name,
                        period=period,
                        company_key=identity.company_key,
                        report_currency=identity.report_currency,
                        table_standard=_table_standard(period_rows),
                        original_hash=original_hash,
                        source_version=version_text,
                    )
                )
                drafts.append(
                    DatasetDraft(
                        kind=DatasetKind.ANNUAL_FACTS,
                        subject_kind=SubjectKind.COMPANY,
                        subject_id=company_id,
                        source=self.source,
                        schema_version=SCHEMA_VERSION,
                        adapter_version=self.adapter_version,
                        captured_at=captured_at,
                        payload=_payload(
                            name=name,
                            report_name=report_name,
                            rows=period_rows,
                            secucode=secucode,
                            unmapped_count=unmapped_period,
                            facts=facts,
                        ),
                        period=period,
                    )
                )
        return ParseResult(drafts=tuple(drafts), problems=tuple(problems))


@dataclass(frozen=True)
class _Row:
    """One detail line normalized to the fields this adapter reads."""

    code: str | None
    name: str | None
    amount: object
    period_end: date | None
    period_start: date | None
    currency: object
    account_standard: str | None
    report_type: str | None
    fiscal_year: str | None


def _detail_rows(
    name: str, data: Sequence[Any]
) -> tuple[list[_Row] | None, set[date | None] | None]:
    """Flatten one table's ``result.data``; ``None`` rows mean a shape failure.

    Cashflow must be outer report rows with a nested ``VALUE_LIST``; the outer
    report dates are returned so they can join the period check. Income and
    balance are flat detail lists.
    """

    rows: list[_Row] = []
    outer_ends: set[date | None] = set()
    for entry in data:
        if not isinstance(entry, Mapping):
            return None, None
        if name == "cashflow":
            values = entry.get("VALUE_LIST")
            if not isinstance(values, list):
                return None, None
            outer_ends.add(_iso_date(entry.get("REPORT_DATE")))
            detail: Sequence[Any] = values
        else:
            detail = [entry]
        for value in detail:
            if not isinstance(value, Mapping):
                return None, None
            rows.append(_row(value))
    return rows, (outer_ends if name == "cashflow" else None)


def _row(entry: Mapping[str, Any]) -> _Row:
    code = entry.get("STD_ITEM_CODE")
    name = entry.get("ITEM_NAME")
    standard = entry.get("ACCOUNT_STANDARD")
    report_type = entry.get("REPORT_TYPE")
    fiscal_year = entry.get("FISCAL_YEAR")
    return _Row(
        code=code if isinstance(code, str) else None,
        name=name if isinstance(name, str) else None,
        amount=entry.get("AMOUNT"),
        period_end=_iso_date(entry.get("STD_REPORT_DATE")),
        period_start=_iso_date(entry.get("START_DATE")),
        currency=entry.get("CURRENCY_CODE", entry.get("CURRENCY_ABBR")),
        account_standard=standard if isinstance(standard, str) else None,
        report_type=report_type if isinstance(report_type, str) else None,
        fiscal_year=fiscal_year if isinstance(fiscal_year, str) else None,
    )


def _duplicate_code(rows: Sequence[_Row]) -> str | None:
    seen: set[tuple[date | None, str]] = set()
    for row in rows:
        if row.code is None:
            continue
        key = (row.period_end, row.code)
        if key in seen:
            return f"{row.code} @ {_iso(row.period_end) or 'unreadable date'}"
        seen.add(key)
    return None


def _fact_for_row(
    *,
    name: str,
    metric: str,
    row: _Row,
    company_key: str,
    report_currency: str,
    original_hash: str,
    source_version: str | None,
    problems: list[str],
) -> FactRecord | None:
    period = _period_for_row(row)
    if period is None:
        problems.append(f"invalid_amount: {name}/{row.code} has no readable period end")
        return None
    raw_value = _decimal_string(row.amount)
    if raw_value is None and row.amount is not None:
        problems.append(f"invalid_amount: {name}/{row.code} is not a number")
        return None
    return _fact(
        metric=metric,
        period=period,
        company_key=company_key,
        report_currency=report_currency,
        raw_value=raw_value,
        missing_reason=(
            None
            if raw_value is not None
            else f"provider AMOUNT is null ({name}/{row.code} @ {period.end.isoformat()})"
        ),
        source_label=row.name,
        source_field=row.code,
        accounting_standard=_accounting_standard(row.account_standard),
        original_hash=original_hash,
        original_locator=_original_locator(company_key, metric, period),
        source_version=source_version,
    )


def _explicit_missing_facts(
    *,
    name: str,
    period: FactPeriod,
    company_key: str,
    report_currency: str,
    table_standard: AccountingStandard,
    original_hash: str,
    source_version: str | None,
) -> list[FactRecord]:
    facts: list[FactRecord] = []
    for missing in EXPLICIT_MISSING.get(company_key, {}).get(name, ()):
        facts.append(
            _fact(
                metric=missing.metric,
                period=_missing_period(name, period),
                company_key=company_key,
                report_currency=report_currency,
                raw_value=None,
                missing_reason=missing.reason,
                source_label=None,
                source_field=None,
                accounting_standard=table_standard,
                original_hash=original_hash,
                original_locator=None,
                source_version=source_version,
            )
        )
    return facts


def _missing_period(name: str, period: FactPeriod) -> FactPeriod:
    if name == "balance":
        # Balance items are instants; only the period end is known here.
        return FactPeriod(start=None, end=period.end, basis=PeriodBasis.INSTANT)
    return period


def _period_for_row(row: _Row) -> FactPeriod | None:
    if row.period_end is None:
        return None
    if row.period_start is None:
        return FactPeriod(start=None, end=row.period_end, basis=PeriodBasis.INSTANT)
    return FactPeriod(start=row.period_start, end=row.period_end, basis=PeriodBasis.ANNUAL)


def _fact(
    *,
    metric: str,
    period: FactPeriod,
    company_key: str,
    report_currency: str,
    raw_value: str | None,
    missing_reason: str | None,
    source_label: str | None,
    source_field: str | None,
    accounting_standard: AccountingStandard,
    original_hash: str,
    original_locator: str | None,
    source_version: str | None,
) -> FactRecord:
    sign_convention = _AS_REPORTED
    applicability = "applicable"
    if metric == "capex_ppe":
        sign_convention = CAPEX_SIGN_CONVENTIONS.get(
            company_key,
            "as reported by the provider (positive spend magnitude; definition not "
            "hand-checked for this issuer)",
        )
        applicability = CAPEX_APPLICABILITY.get(company_key, "applicable")
    if raw_value is None:
        sign_convention = _MISSING_SIGN
    return FactRecord(
        metric=metric,
        period=period,
        semantics=FactSemantics(
            metric=metric,
            source_label=source_label,
            source_field=source_field,
            accounting_standard=accounting_standard,
            reporting_scope=ReportingScope.CONSOLIDATED,
            equity_scope=_EQUITY_SCOPES.get(metric, EquityScope.UNKNOWN),
            per_share_basis=PerShareBasis.NA,
        ),
        value=FactValue(
            raw_value=raw_value,
            raw_unit="元",
            scale=1,
            unit=report_currency,
            currency=report_currency,
            sign_convention=sign_convention,
            missing_reason=missing_reason,
        ),
        provenance=FactProvenance(
            source=SOURCE,
            accession=None,
            original_hash=original_hash,
            source_published_at=None,
            source_published_date=None,
            source_published_precision=DatePrecision.UNKNOWN,
            accepted_at=None,
            source_available_at=None,
        ),
        verification=Verification.CANDIDATE,
        conflict="none",
        applicability=applicability,
        original_locator=original_locator,
        source_version=source_version,
    )


def _payload(
    *,
    name: str,
    report_name: str,
    rows: Sequence[_Row],
    secucode: str,
    unmapped_count: int,
    facts: Sequence[FactRecord],
) -> dict[str, Any]:
    return {
        "facts": [
            fact_to_payload(
                fact,
                source_label=fact.semantics.source_label,
                source_field=fact.semantics.source_field,
                original_locator=fact.original_locator,
            )
            for fact in facts
        ],
        "report": name,
        "report_name": report_name,
        "account_standard": _unique_text(row.account_standard for row in rows),
        "fiscal_year_label": _unique_text(row.fiscal_year for row in rows),
        "unmapped_item_count": unmapped_count,
        "provider": {
            "secucode": secucode,
            "report_type": _unique_text(row.report_type for row in rows),
        },
    }


def _accounting_standard(text: str | None) -> AccountingStandard:
    if text is None:
        return AccountingStandard.UNKNOWN
    return _ACCOUNTING_STANDARDS.get(text, AccountingStandard.UNKNOWN)


def _table_standard(rows: Sequence[_Row]) -> AccountingStandard:
    standards = {_accounting_standard(row.account_standard) for row in rows}
    if len(standards) == 1:
        return standards.pop()
    return AccountingStandard.UNKNOWN


def _original_locator(company_key: str, metric: str, period: FactPeriod) -> str | None:
    document = OFFICIAL_DOCUMENTS.get(company_key)
    if document is None or document.period_end != period.end:
        return None
    page = document.pages.get(metric)
    if page is None:
        return None
    return f"{document.url} p{page}"


def _unique_text(values: Iterable[object]) -> str | None:
    seen = sorted({value for value in values if isinstance(value, str) and value})
    if not seen:
        return None
    return "|".join(seen)


def _has_display_marker(content: bytes) -> bool:
    text = content.decode("utf-8", errors="replace")
    return any(marker in text for marker in DISPLAY_MARKERS)


def _json_object(content: bytes) -> Mapping[str, Any] | None:
    try:
        text = content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return None
    if text.lstrip().startswith("<"):
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, Mapping) else None


def _decimal_string(value: object) -> str | None:
    """The provider's amount as a plain decimal string; ``None`` = not numeric."""

    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return format(Decimal(str(value)), "f") if math.isfinite(value) else None
    if isinstance(value, Decimal):
        return format(value, "f") if value.is_finite() else None
    if isinstance(value, str) and value.strip():
        try:
            parsed = Decimal(value.strip())
        except InvalidOperation:
            return None
        return format(parsed, "f") if parsed.is_finite() else None
    return None


def _iso_date(value: object) -> date | None:
    if not isinstance(value, str) or len(value) < 10:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _iso(value: date | None) -> str | None:
    return None if value is None else value.isoformat()


def _period_label(value: date | None) -> str:
    """Sortable label for a possibly unreadable period date in problem text."""

    return value.isoformat() if value is not None else "unreadable"
