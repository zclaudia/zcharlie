"""US SEC CompanyFacts + Submissions adapter (plan 04).

Two controlled sources, two datasets:

* ``sec.companyfacts.v1`` -> ``annual_facts``: one :class:`DatasetDraft` per
  (company, fiscal year). The fiscal year comes from the issuer's own filing
  period (the ``start``/``end`` of its ``10-K`` / ``20-F`` rows, cross-checked
  against the Submissions ``reportDate``) — never from ``frames`` and never from
  a hard-coded December 31st. 52/53-week years keep their true span.
* ``sec.submissions.v1`` -> ``filing_index``: one per company (``period=None``),
  carrying the filings, the merged historical shards, the per-period version
  state and the Item 4.02 notices. Its ``payload["facts"]`` is the empty list
  the frozen contract requires of every payload.

Decisions kept from the plan:

* **No EdgarTools** (``pyproject.toml`` untouched, offline environment): version
  selection and eligibility belong to this application, so the bounded parsing
  lives here and the version state machine in ``us_sec_periods``.
  ``us_sec_periods.select_period_state`` pins the behaviour for the LNC case; if
  a third-party parser is ever introduced, this module's tests
  (``tests/unit/test_material_us_sec.py``) must be re-run against the new
  library version — the plan requires exactly that.
* **Fact selection key** = ``CIK + taxonomy + concept + unit + start/end +
  accession + form``. ``duration`` (a ``start`` is present) and ``instant`` (no
  ``start``) rows never mix. ``frame`` is not read at all: it stays in the
  fixtures only to prove that deleting it changes nothing.
* **Missing is not zero**: a mapped metric without a standard tag produces
  ``raw_value=None`` + ``missing_reason`` (JPM does not tag
  ``PaymentsToAcquirePropertyPlantAndEquipment``; no nearby tag is substituted),
  and differing versions stay visible as separate fact entries with
  ``conflict="unresolved"`` while the period state names the current version.
* **Taxonomy whitelist** = ``us-gaap:*`` and ``ifrs-full:*``. ``dei``/``srt``/
  ``ecd``/``ffd``/``invest`` and issuer extensions produce no facts and are
  counted in ``payload["unmapped"]["taxonomies"]``. TSM's IFRS amounts stay in
  TWD; the USD convenience translations of the same concepts are skipped and
  counted in ``payload["unmapped"]["unit_skipped"]`` — no conversion exists here.
* **Account standard comes from the data**: the whitelisted taxonomy satisfying
  the most mapped metrics wins (ties keep ``us-gaap``). That is how TSM lands on
  ``ifrs-full`` without an issuer-specific branch, and why ``profit_*`` for TSM
  only ever reads the 20-F series (``6-K`` rows are not annual-report forms).

Data discipline:

* ``acceptanceDateTime`` -> ``accepted_at`` + ``source_published_at`` +
  ``precision=TIMESTAMP``; only ``filed`` -> ``precision=DATE`` with
  ``source_published_at=None``; neither -> ``UNKNOWN`` with both published
  fields null. ``source_available_at`` stays null everywhere (unknown is
  unknown).
* ``captured_at`` is the real parse time; ``source_fetched_at`` belongs to the
  import path (task 05) and is never back-filled here.
* Drafts carry no ``raw_bytes``: one CompanyFacts document covers several fiscal
  years, so the raw artifact belongs to the fetch/import stage (05/06).
* Structural problems (non-JSON body, wrong shape, missing ``facts``, CIK
  mismatch, unreadable supplied shard) reject the whole parse
  (``drafts == ()``); incompleteness (a document or shard listed but not
  supplied) is reported in ``problems`` while drafts are still produced.

Module layout: this module holds the adapter, the document parsing, the period
discovery and the draft assembly. The concept table and the static selectors
built on it live in ``us_sec_concepts.py``; the version state machine, its
payload dataclasses and the shared reference/time helpers live in
``us_sec_periods.py``. Both are imported here, one way only.

Fixture provenance (``tests/fixtures/materials/us/**``): trimmed from the saved
research responses under
``research/market-data-validation/evidence/{remaining-final-03/raw/us,validated-run-01/raw/sec}``
(AAPL/COST/JPM/XOM/TSM captured 2026-10-11; LNC from ``validated-run-01``). Only
the taxonomies/concepts/units and filings the tests exercise were kept, well
under 80 KB per company; ``accn``/``filed``/``form``/``fy``/``fp``/``start``/
``end``/``val``/``frame`` are retained. Each fixture's ``filings.files`` list is
trimmed to the shards shipped with that fixture, so no fixture claims a shard it
does not carry.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
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
    FactPeriod,
    FactProvenance,
    FactRecord,
    FactSemantics,
    FactValue,
    ParseResult,
    PeriodBasis,
    RawPayload,
    ReportingScope,
    SubjectKind,
    UpstreamRequest,
    Verification,
    fact_to_payload,
)
from investment_agent.materials.us_sec_concepts import (
    _TAXONOMY_WHITELIST,
    CONCEPT_MAP,
    ConceptBinding,
    _bindings_for,
    _choose_taxonomy,
    _expected_unit,
    _ExpectedUnit,
    _metric_concepts,
    _metrics_for,
    _unit_matches,
    _unit_skipped_counts,
)
from investment_agent.materials.us_sec_periods import (
    UNRELIABLE_ITEM_CODE,
    FactRow,
    FilingRef,
    PeriodSelection,
    UnreliableNotice,
    _reference_known_at,
    _reference_order,
    select_period_state,
)

SOURCE_FACTS = "sec.companyfacts.v1"
SOURCE_FILINGS = "sec.submissions.v1"
ADAPTER_VERSION = "1"
COMPANYFACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
SHARD_URL = "https://data.sec.gov/submissions/{name}"
USER_AGENT = "zcharlie-research/0.1 (personal research; contact in config)"
MIN_INTERVAL_SECONDS = 0.5
"""Lower bound shared with the app-wide limiter injected by task 06."""

FACTS_DOCUMENT = "companyfacts.json"
SUBMISSIONS_DOCUMENT = "submissions.json"
_ANNUAL_REPORT_FORMS: tuple[str, ...] = ("10-K", "20-F")
"""Non-amendment annual reports; only these define a fiscal-year period."""
_ANNUAL_SPAN_RANGE = (330, 400)
DEFAULT_PERIOD_WINDOW = 4
"""Most recent fiscal years produced when no ``periods`` are requested."""
MISSING_TAG_REASON = "standard tag absent in CompanyFacts for this issuer"
_SIGN_CONVENTION = "as_reported"


_STANDARD_BY_TAXONOMY: dict[str, AccountingStandard] = {
    "us-gaap": AccountingStandard.US_GAAP,
    "ifrs-full": AccountingStandard.IFRS,
}


# --- the adapter ------------------------------------------------------------


@dataclass(frozen=True)
class SecFactsAdapter:
    """CompanyFacts + Submissions adapter (the ``MaterialAdapter`` protocol).

    ``subject_id`` is ``company.id`` in this application; a
    :class:`ControlledIdentity` is id-free by design, so the caller supplies it
    (task 06 owns the listing -> company mapping). It may be given on the
    instance or per call.

    ``requests()`` is a two-stage builder: the first call returns CompanyFacts +
    Submissions, and once ``parse()`` has seen the Submissions document every
    historical shard listed in ``filings.files[].name`` is appended as one more
    request — the shards must be fetched and merged, never skipped.
    """

    subject_id: int | None = None
    source: str = SOURCE_FACTS
    adapter_version: str = ADAPTER_VERSION
    _shard_names: dict[str, tuple[str, ...]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def requests(
        self,
        identity: ControlledIdentity,
        *,
        periods: Sequence[FactPeriod] = (),
        subject_id: int | None = None,
    ) -> tuple[UpstreamRequest, ...]:
        """Documents to fetch: CompanyFacts, Submissions, then its shards."""

        del periods, subject_id
        cik10 = _identity_cik(identity)
        return (
            UpstreamRequest(name=FACTS_DOCUMENT, url=COMPANYFACTS_URL.format(cik=int(cik10))),
            UpstreamRequest(name=SUBMISSIONS_DOCUMENT, url=SUBMISSIONS_URL.format(cik=int(cik10))),
            *(
                UpstreamRequest(name=name, url=SHARD_URL.format(name=name))
                for name in self._shard_names.get(cik10, ())
            ),
        )

    def parse(
        self,
        identity: ControlledIdentity,
        *,
        raw: Sequence[RawPayload],
        periods: Sequence[FactPeriod] = (),
        subject_id: int | None = None,
    ) -> ParseResult:
        """Build the annual-facts drafts and the filing index from raw responses.

        ``periods`` restricts the fiscal years (matched by period end); empty
        means the most recent :data:`DEFAULT_PERIOD_WINDOW` fiscal years found in
        the response.
        """

        cik10 = _identity_cik(identity)
        company_id = subject_id if subject_id is not None else self.subject_id
        if company_id is None:
            raise ValueError(
                "SecFactsAdapter needs subject_id (the application's company id) on the "
                "instance or per parse() call"
            )

        rejected = tuple(
            f"http_status_rejected: {item.name}/{item.http_status}"
            for item in raw if item.http_status != 0 and not 200 <= item.http_status < 300
        )
        if rejected:
            return ParseResult(drafts=(), problems=rejected)

        fatal: list[str] = []
        soft: list[str] = []
        facts_doc = _document(raw, FACTS_DOCUMENT)
        submissions_doc = _document(raw, SUBMISSIONS_DOCUMENT)
        if facts_doc is None:
            soft.append(f"missing_document: {FACTS_DOCUMENT}")
        if submissions_doc is None:
            soft.append(f"missing_document: {SUBMISSIONS_DOCUMENT}")
            soft.extend(
                f"unexpected_document: {payload.name}"
                for payload in raw
                if payload.name not in (FACTS_DOCUMENT, SUBMISSIONS_DOCUMENT)
            )

        facts = None if facts_doc is None else _read_facts_document(facts_doc, cik10, fatal, soft)
        submissions = None
        if submissions_doc is not None:
            submissions = _read_submissions_document(
                cik10,
                submissions_doc,
                _shard_payloads(raw, submissions_doc),
                fatal,
                soft,
            )
        if fatal:
            return ParseResult(drafts=(), problems=tuple(fatal + soft))
        if submissions is not None:
            self._shard_names[cik10] = submissions.shard_names

        captured_at = datetime.now(UTC)
        annual = _annual_facts_drafts(
            identity=identity,
            facts=facts,
            submissions=submissions,
            periods=periods,
            subject_id=company_id,
            captured_at=captured_at,
            soft=soft,
        )
        index = _filing_index_draft(
            identity=identity,
            facts=facts,
            submissions=submissions,
            subject_id=company_id,
            captured_at=captured_at,
        )
        return ParseResult(
            drafts=(*annual, *((index,) if index is not None else ())), problems=tuple(soft)
        )


# --- CompanyFacts parsing ---------------------------------------------------


@dataclass(frozen=True)
class _FactsDocument:
    rows: tuple[FactRow, ...]
    entity_name: str | None
    taxonomy_counts: dict[str, int]
    unmapped_concepts: dict[str, int]


def _read_facts_document(
    payload: RawPayload, cik10: str, fatal: list[str], soft: list[str]
) -> _FactsDocument | None:
    body = _json_object(payload, fatal)
    if body is None:
        return None
    if not _cik_matches(body.get("cik"), cik10):
        fatal.append(
            f"cik_mismatch: {payload.name} expected={cik10} got={_cik_text(body.get('cik'))}"
        )
        return None
    facts_block = body.get("facts")
    if not isinstance(facts_block, Mapping):
        fatal.append(f"missing_facts: {payload.name}")
        return None

    rows: list[FactRow] = []
    taxonomy_counts: dict[str, int] = {}
    unmapped_concepts: dict[str, int] = {}
    for taxonomy, concepts in facts_block.items():
        if not isinstance(taxonomy, str) or not isinstance(concepts, Mapping):
            fatal.append(f"unexpected_shape: {payload.name} facts[{taxonomy!r}]")
            return None
        for concept, node in concepts.items():
            units = node.get("units") if isinstance(node, Mapping) else None
            if not isinstance(concept, str) or not isinstance(units, Mapping):
                fatal.append(f"unexpected_shape: {payload.name} facts[{taxonomy}][{concept!r}]")
                return None
            mapped = bool(_bindings_for(taxonomy, concept))
            for unit, unit_rows in units.items():
                if not isinstance(unit, str) or not isinstance(unit_rows, list):
                    fatal.append(
                        f"unexpected_shape: {payload.name} facts[{taxonomy}][{concept}][{unit!r}]"
                    )
                    return None
                for raw_row in unit_rows:
                    row = _fact_row(taxonomy, concept, unit, raw_row, payload.name, soft)
                    if row is None:
                        continue
                    rows.append(row)
                    taxonomy_counts[taxonomy] = taxonomy_counts.get(taxonomy, 0) + 1
                    if taxonomy in _TAXONOMY_WHITELIST and not mapped:
                        unmapped_concepts[taxonomy] = unmapped_concepts.get(taxonomy, 0) + 1

    entity_name = body.get("entityName")
    return _FactsDocument(
        rows=tuple(rows),
        entity_name=entity_name if isinstance(entity_name, str) else None,
        taxonomy_counts=taxonomy_counts,
        unmapped_concepts=unmapped_concepts,
    )


def _fact_row(
    taxonomy: str,
    concept: str,
    unit: str,
    raw_row: object,
    document: str,
    soft: list[str],
) -> FactRow | None:
    if not isinstance(raw_row, Mapping):
        soft.append(f"skipped_row: {document} {taxonomy}:{concept} is not an object")
        return None
    end_text = raw_row.get("end")
    accession = raw_row.get("accn")
    form = raw_row.get("form")
    value = raw_row.get("val")
    if (
        not isinstance(end_text, str)
        or not isinstance(accession, str)
        or not isinstance(form, str)
        or value is None
    ):
        soft.append(f"skipped_row: {document} {taxonomy}:{concept} lacks end/accn/form/val")
        return None
    end = _parse_date(end_text)
    start_text = raw_row.get("start")
    start = _parse_date(start_text) if isinstance(start_text, str) else None
    if end is None or (isinstance(start_text, str) and start is None):
        soft.append(f"skipped_row: {document} {taxonomy}:{concept} accn {accession} has a bad date")
        return None
    value_text = _decimal_text(value)
    if value_text is None:
        soft.append(
            f"skipped_row: {document} {taxonomy}:{concept} accn {accession} has a bad value"
        )
        return None
    return FactRow(
        taxonomy=taxonomy,
        concept=concept,
        unit=unit,
        start=start,
        end=end,
        value=value_text,
        accession=accession,
        form=form,
        filed=_parse_date(raw_row.get("filed")),
        fy=_parse_int(raw_row.get("fy")),
        fp=raw_row.get("fp") if isinstance(raw_row.get("fp"), str) else None,
    )


# --- Submissions parsing ----------------------------------------------------


@dataclass(frozen=True)
class _SubmissionsDocument:
    filings: dict[str, FilingRef]
    notices: tuple[UnreliableNotice, ...]
    filing_count: int
    shard_names: tuple[str, ...]
    merged_shards: tuple[str, ...]
    missing_shards: tuple[str, ...]


def _read_submissions_document(
    cik10: str,
    payload: RawPayload,
    shards: tuple[RawPayload, ...],
    fatal: list[str],
    soft: list[str],
) -> _SubmissionsDocument | None:
    body = _json_object(payload, fatal)
    if body is None:
        return None
    if not _cik_matches(body.get("cik"), cik10):
        fatal.append(
            f"cik_mismatch: {payload.name} expected={cik10} got={_cik_text(body.get('cik'))}"
        )
        return None
    filings_block = body.get("filings")
    if not isinstance(filings_block, Mapping):
        fatal.append(f"unexpected_shape: {payload.name} filings")
        return None
    recent = filings_block.get("recent")
    if not isinstance(recent, Mapping):
        fatal.append(f"missing_recent: {payload.name}")
        return None
    filings = _filing_rows(recent, payload.name, fatal, soft)
    if filings is None:
        return None

    shard_names = _shard_names(filings_block)
    merged: list[str] = []
    for shard in shards:
        if shard.name not in shard_names:
            soft.append(f"unexpected_document: {shard.name}")
            continue
        shard_body = _json_object(shard, fatal)
        if shard_body is None:
            return None
        table = _shard_table(shard_body, shard.name, fatal)
        if table is None:
            return None
        rows = _filing_rows(table, shard.name, fatal, soft)
        if rows is None:
            return None
        for accession, ref in rows.items():
            if accession in filings:
                soft.append(f"duplicate_accession: {accession} in {shard.name}")
                continue
            filings[accession] = ref
        merged.append(shard.name)
    missing = tuple(name for name in shard_names if name not in merged)
    soft.extend(f"missing_shard: {name}" for name in missing)

    notices = tuple(
        UnreliableNotice(
            accession=ref.accession,
            form=ref.form,
            accepted_at=ref.accepted_at,
            items=ref.items,
            known_at=_reference_known_at(ref),
        )
        for ref in sorted(filings.values(), key=_reference_order)
        if ref.form == "8-K" and UNRELIABLE_ITEM_CODE in ref.items
    )
    return _SubmissionsDocument(
        filings=filings,
        notices=notices,
        filing_count=len(filings),
        shard_names=shard_names,
        merged_shards=tuple(merged),
        missing_shards=missing,
    )


def _filing_rows(
    table: Mapping[str, Any], document: str, fatal: list[str], soft: list[str]
) -> dict[str, FilingRef] | None:
    accessions = table.get("accessionNumber")
    if not isinstance(accessions, list):
        fatal.append(f"unexpected_shape: {document} accessionNumber")
        return None
    columns = {
        name: table.get(name)
        for name in (
            "filingDate",
            "reportDate",
            "acceptanceDateTime",
            "form",
            "items",
            "primaryDocument",
        )
    }
    for name, values in columns.items():
        if values is not None and not isinstance(values, list):
            fatal.append(f"unexpected_shape: {document} {name}")
            return None
    filings: dict[str, FilingRef] = {}
    for index, accession in enumerate(accessions):
        if not isinstance(accession, str):
            soft.append(f"skipped_filing: {document} row {index} accession is not a string")
            continue
        form = _cell(columns, "form", index)
        filings[accession] = FilingRef(
            accession=accession,
            form=form if isinstance(form, str) else "",
            filing_date=_parse_date(_cell(columns, "filingDate", index)),
            accepted_at=_parse_datetime(_cell(columns, "acceptanceDateTime", index)),
            report_period=_parse_date(_cell(columns, "reportDate", index)),
            primary_document=_text_or_none(_cell(columns, "primaryDocument", index)),
            items=_items(_cell(columns, "items", index)),
        )
    return filings


def _cell(columns: Mapping[str, Any], name: str, index: int) -> object:
    values = columns.get(name)
    if not isinstance(values, list) or index >= len(values):
        return None
    return values[index]


def _shard_table(body: Mapping[str, Any], name: str, fatal: list[str]) -> Mapping[str, Any] | None:
    """Saved shard bodies are the bare column table; accept the wrapped shape too."""

    if "accessionNumber" in body:
        return body
    filings_block = body.get("filings")
    if isinstance(filings_block, Mapping):
        if "accessionNumber" in filings_block:
            return filings_block
        recent = filings_block.get("recent")
        if isinstance(recent, Mapping):
            return recent
    fatal.append(f"unexpected_shape: {name}")
    return None


def _shard_names(filings_block: Mapping[str, Any]) -> tuple[str, ...]:
    files = filings_block.get("files")
    if not isinstance(files, list):
        return ()
    names: list[str] = []
    for entry in files:
        if isinstance(entry, Mapping):
            name = entry.get("name")
            if isinstance(name, str):
                names.append(name)
    return tuple(names)


# --- annual facts -----------------------------------------------------------


@dataclass(frozen=True)
class _AnnualPeriod:
    start: date
    end: date

    @property
    def days(self) -> int:
        return (self.end - self.start).days


def _annual_facts_drafts(
    *,
    identity: ControlledIdentity,
    facts: _FactsDocument | None,
    submissions: _SubmissionsDocument | None,
    periods: Sequence[FactPeriod],
    subject_id: int,
    captured_at: datetime,
    soft: list[str] | None = None,
) -> tuple[DatasetDraft, ...]:
    if facts is None or not facts.rows:
        if periods and soft is not None:
            soft.extend(f"missing_period: {period.end.isoformat()}" for period in periods)
        return ()
    report_currency = identity.report_currency
    taxonomy = _choose_taxonomy(facts.rows, report_currency)
    standard = _STANDARD_BY_TAXONOMY[taxonomy]
    metrics = _metrics_for(taxonomy)
    unit_skipped = _unit_skipped_counts(facts.rows, taxonomy, report_currency)
    filings_by_accession = {} if submissions is None else submissions.filings
    notices = () if submissions is None else submissions.notices

    targets = _target_periods(facts.rows, periods)
    if periods and soft is not None:
        found = {period.end for period in targets}
        soft.extend(
            f"missing_period: {period.end.isoformat()}"
            for period in periods
            if period.end not in found
        )
    drafts: list[DatasetDraft] = []
    for period in targets:
        references = _period_references(facts.rows, filings_by_accession, period, taxonomy, metrics)
        selection = select_period_state(references, notices)
        entries: list[dict[str, Any]] = []
        missing: list[str] = []
        for metric in metrics:
            metric_entries, metric_missing = _metric_entries(
                rows=facts.rows,
                filings_by_accession=filings_by_accession,
                taxonomy=taxonomy,
                standard=standard,
                metric=metric,
                period=period,
                report_currency=report_currency,
                selection=selection,
            )
            entries.extend(metric_entries)
            if metric_missing:
                missing.append(metric)
        label = _fiscal_label(facts.rows, period)
        payload: dict[str, Any] = {
            "facts": entries,
            "cik": _identity_cik(identity),
            "entity_name": facts.entity_name,
            "report_currency": report_currency,
            "accounting_standard": standard.value,
            "fiscal_year_label": label,
            "period_state": selection.payload(),
            "missing_metrics": missing,
            "unmapped": {
                "taxonomies": dict(sorted(facts.taxonomy_counts.items())),
                "concepts": dict(sorted(facts.unmapped_concepts.items())),
                "unit_skipped": dict(sorted(unit_skipped.items())),
            },
            "filings_with_known_acceptance": sum(
                1 for ref in references if ref.accepted_at is not None
            ),
            "documents": [
                FACTS_DOCUMENT,
                *(() if submissions is None else submissions.merged_shards),
            ],
        }
        drafts.append(
            DatasetDraft(
                kind=DatasetKind.ANNUAL_FACTS,
                subject_kind=SubjectKind.COMPANY,
                subject_id=subject_id,
                source=SOURCE_FACTS,
                schema_version=SCHEMA_VERSION,
                adapter_version=ADAPTER_VERSION,
                captured_at=captured_at,
                payload=payload,
                period=FactPeriod(
                    start=period.start,
                    end=period.end,
                    basis=PeriodBasis.ANNUAL,
                    label=label,
                    days=period.days,
                ),
                raw_source_url=COMPANYFACTS_URL.format(cik=int(_identity_cik(identity))),
            )
        )
    drafts.sort(key=lambda draft: draft.period.end if draft.period is not None else date.min)
    return tuple(drafts)


def _metric_entries(
    *,
    rows: Sequence[FactRow],
    filings_by_accession: Mapping[str, FilingRef],
    taxonomy: str,
    standard: AccountingStandard,
    metric: str,
    period: _AnnualPeriod,
    report_currency: str,
    selection: PeriodSelection,
) -> tuple[list[dict[str, Any]], bool]:
    bindings = [
        binding
        for binding in CONCEPT_MAP
        if binding.metric == metric and binding.taxonomy == taxonomy
    ]
    binding = min(bindings, key=lambda item: item.priority)
    concepts = {item.concept for item in bindings}
    expected_unit = _expected_unit(binding, report_currency)
    candidates = [
        row
        for row in rows
        if row.taxonomy == taxonomy
        and row.concept in concepts
        and _unit_matches(row.unit, binding, report_currency)
        and _row_in_period(row, period)
    ]
    fact_period = _fact_period(binding, _fiscal_label(candidates, period), period)
    if not candidates:
        return [
            fact_to_payload(
                _fact_record(
                    metric=metric,
                    period=fact_period,
                    binding=binding,
                    standard=standard,
                    raw_value=None,
                    raw_unit=expected_unit.raw_unit,
                    value_unit=expected_unit,
                    provenance=_provenance(accession=None, filed=None, accepted=None),
                    conflict="none",
                    conflict_note=None,
                    applicability="unknown",
                    source_version=None,
                    missing_reason=MISSING_TAG_REASON,
                )
            )
        ], True

    ordered = _candidate_order(candidates, selection.selected_accession)
    values: list[tuple[str, FactRow]] = []
    for row in ordered:
        if all(row.value != value for value, _ in values):
            values.append((row.value, row))
    conflict = "none" if len(values) == 1 else "unresolved"
    note = (
        None
        if conflict == "none"
        else "candidates kept: " + "; ".join(f"{row.accession}={value}" for value, row in values)
    )
    entries: list[dict[str, Any]] = []
    for value, row in values:
        reference = filings_by_accession.get(row.accession)
        row_binding = next((item for item in bindings if item.concept == row.concept), binding)
        entries.append(
            fact_to_payload(
                _fact_record(
                    metric=metric,
                    period=fact_period,
                    binding=row_binding,
                    standard=standard,
                    raw_value=value,
                    raw_unit=row.unit,
                    value_unit=expected_unit,
                    provenance=_provenance(
                        accession=row.accession,
                        filed=row.filed,
                        accepted=None if reference is None else reference.accepted_at,
                    ),
                    conflict=conflict,
                    conflict_note=note,
                    applicability="applicable",
                    source_version=row.accession,
                    missing_reason=None,
                )
            )
        )
    return entries, False


def _candidate_order(
    candidates: Sequence[FactRow], selected_accession: str | None
) -> list[FactRow]:
    """The selected filing's rows first, then by filing time and accession."""

    def key(row: FactRow) -> tuple[int, str, str]:
        return (
            0 if row.accession == selected_accession else 1,
            row.filed.isoformat() if row.filed is not None else "",
            row.accession,
        )

    return sorted(candidates, key=key)


def _fact_period(binding: ConceptBinding, label: str | None, period: _AnnualPeriod) -> FactPeriod:
    if binding.basis == PeriodBasis.INSTANT:
        return FactPeriod(start=None, end=period.end, basis=PeriodBasis.INSTANT, label=label)
    return FactPeriod(
        start=period.start,
        end=period.end,
        basis=PeriodBasis.ANNUAL,
        label=label,
        days=period.days,
    )


def _fiscal_label(rows: Sequence[FactRow], period: _AnnualPeriod) -> str | None:
    """``FY<fy>`` from the source rows; never derived from the period's year."""

    years = [row.fy for row in rows if row.fy is not None and _row_in_period(row, period)]
    if not years:
        return None
    return f"FY{min(years)}"


def _row_in_period(row: FactRow, period: _AnnualPeriod) -> bool:
    if row.is_duration:
        return row.start == period.start and row.end == period.end
    return row.end == period.end


def _fact_record(
    *,
    metric: str,
    period: FactPeriod,
    binding: ConceptBinding,
    standard: AccountingStandard,
    raw_value: str | None,
    raw_unit: str,
    value_unit: _ExpectedUnit,
    provenance: FactProvenance,
    conflict: str,
    conflict_note: str | None,
    applicability: str,
    source_version: str | None,
    missing_reason: str | None,
) -> FactRecord:
    has_value = raw_value is not None
    semantics = FactSemantics(
        metric=metric,
        source_label=binding.concept if has_value else None,
        source_field=f"{binding.taxonomy}:{binding.concept}" if has_value else None,
        accounting_standard=standard,
        reporting_scope=ReportingScope.CONSOLIDATED,
        equity_scope=binding.equity_scope,
        per_share_basis=binding.per_share_basis,
    )
    locator = (
        None
        if not has_value or provenance.accession is None
        else f"CompanyFacts {binding.taxonomy}:{binding.concept} accn {provenance.accession}"
    )
    return FactRecord(
        metric=metric,
        period=period,
        semantics=semantics,
        value=FactValue(
            raw_value=raw_value,
            raw_unit=raw_unit,
            scale=1,
            unit=value_unit.unit,
            currency=value_unit.currency,
            sign_convention=_SIGN_CONVENTION,
            missing_reason=missing_reason,
        ),
        provenance=provenance,
        verification=Verification.CANDIDATE,
        conflict=conflict,
        applicability=applicability,
        conflict_note=conflict_note,
        original_locator=locator,
        source_version=source_version,
    )


def _provenance(
    *,
    accession: str | None,
    filed: date | None,
    accepted: datetime | None,
) -> FactProvenance:
    if accepted is not None:
        precision = DatePrecision.TIMESTAMP
        published_date = filed if filed is not None else accepted.date()
    elif filed is not None:
        precision = DatePrecision.DATE
        published_date = filed
    else:
        precision = DatePrecision.UNKNOWN
        published_date = None
    return FactProvenance(
        source=SOURCE_FACTS,
        accession=accession,
        original_hash=None,
        source_published_at=accepted,
        source_published_date=published_date,
        source_published_precision=precision,
        accepted_at=accepted,
        source_available_at=None,
    )


# --- period discovery and filing references ---------------------------------


def _discover_periods(rows: Sequence[FactRow]) -> tuple[_AnnualPeriod, ...]:
    """Every fiscal year found in the issuer's own annual-report rows.

    A period exists only where a non-amendment ``10-K``/``20-F`` row states a
    fiscal-year-long duration — so the boundary is the issuer's, never a
    calendar quarter or a ``frame`` label.
    """

    low, high = _ANNUAL_SPAN_RANGE
    discovered: dict[date, _AnnualPeriod] = {}
    for row in rows:
        if row.form not in _ANNUAL_REPORT_FORMS or not row.is_duration:
            continue
        span = row.span_days
        if span is None or not low <= span <= high or row.start is None:
            continue
        discovered[row.end] = _AnnualPeriod(row.start, row.end)
    return tuple(sorted(discovered.values(), key=lambda period: period.end))


def _target_periods(
    rows: Sequence[FactRow], periods: Sequence[FactPeriod]
) -> tuple[_AnnualPeriod, ...]:
    """The fiscal years this parse should publish.

    ``periods`` filters the discovered years by period end; empty keeps the most
    recent :data:`DEFAULT_PERIOD_WINDOW` of them.
    """

    discovered = _discover_periods(rows)
    if periods:
        wanted = {period.end for period in periods}
        return tuple(period for period in discovered if period.end in wanted)
    return discovered[-DEFAULT_PERIOD_WINDOW:]


def _period_references(
    rows: Sequence[FactRow],
    filings_by_accession: Mapping[str, FilingRef],
    period: _AnnualPeriod,
    taxonomy: str,
    metrics: Sequence[str],
) -> tuple[FilingRef, ...]:
    """`FilingRef`s for one period: own annual reports plus every re-citation.

    An own annual report appears even when it carries no observation for the
    period (its facts may be absent from the saved CompanyFacts) — that is what
    lets the state machine mark the period ``superseded`` instead of silently
    presenting the older value as current.
    """

    concepts_by_metric = {metric: _metric_concepts(taxonomy, metric) for metric in metrics}
    observations: dict[str, list[tuple[str, str]]] = {}
    forms: dict[str, tuple[str, date | None]] = {}
    for row in rows:
        if not _row_in_period(row, period):
            continue
        forms.setdefault(row.accession, (row.form, row.filed))
        for metric, concepts in concepts_by_metric.items():
            values = observations.setdefault(row.accession, [])
            if row.concept in concepts and all(name != metric for name, _ in values):
                values.append((metric, row.value))

    references: dict[str, FilingRef] = {}
    for ref in filings_by_accession.values():
        if ref.is_annual and ref.report_period == period.end:
            references[ref.accession] = replace(
                ref,
                period_end=period.end,
                observations=tuple(observations.get(ref.accession, ())),
            )
    for accession, observed in observations.items():
        if accession in references:
            continue
        base = filings_by_accession.get(accession)
        form, filed = forms.get(accession, ("", None))
        references[accession] = (
            replace(base, period_end=period.end, observations=tuple(observed))
            if base is not None
            else FilingRef(
                accession=accession,
                form=form,
                filing_date=filed,
                accepted_at=None,
                report_period=None,
                primary_document=None,
                period_end=period.end,
                observations=tuple(observed),
            )
        )
    return tuple(references.values())


# --- filing index -----------------------------------------------------------


def _filing_index_draft(
    *,
    identity: ControlledIdentity,
    facts: _FactsDocument | None,
    submissions: _SubmissionsDocument | None,
    subject_id: int,
    captured_at: datetime,
) -> DatasetDraft | None:
    if submissions is None:
        return None
    report_currency = identity.report_currency
    taxonomy = (
        _choose_taxonomy(facts.rows, report_currency) if facts is not None and facts.rows else None
    )
    metrics = () if taxonomy is None else _metrics_for(taxonomy)
    listed = [ref for ref in submissions.filings.values() if ref.is_annual or ref.form == "8-K"]
    periods: dict[str, Any] = {}
    if facts is not None and facts.rows and taxonomy is not None:
        for period in _discover_periods(facts.rows):
            references = _period_references(
                facts.rows, submissions.filings, period, taxonomy, metrics
            )
            periods[period.end.isoformat()] = select_period_state(
                references, submissions.notices
            ).payload()
    payload: dict[str, Any] = {
        "facts": [],
        "cik": _identity_cik(identity),
        "entity_name": None if facts is None else facts.entity_name,
        "report_currency": report_currency,
        "filings": [
            ref.payload()
            for ref in sorted(listed, key=lambda ref: (ref.filing_date or date.min, ref.accession))
        ],
        "shards": list(submissions.merged_shards),
        "shards_missing": list(submissions.missing_shards),
        "periods": periods,
        "unreliable_notices": [notice.payload() for notice in submissions.notices],
        "filing_count": submissions.filing_count,
        "annual_filing_count": sum(1 for ref in listed if ref.is_annual),
    }
    return DatasetDraft(
        kind=DatasetKind.FILING_INDEX,
        subject_kind=SubjectKind.COMPANY,
        subject_id=subject_id,
        source=SOURCE_FILINGS,
        schema_version=SCHEMA_VERSION,
        adapter_version=ADAPTER_VERSION,
        captured_at=captured_at,
        payload=payload,
        period=None,
        raw_source_url=SUBMISSIONS_URL.format(cik=int(_identity_cik(identity))),
    )


# --- small helpers ----------------------------------------------------------


def _identity_cik(identity: ControlledIdentity) -> str:
    symbol = source_symbol(identity, SOURCE_FACTS)
    if symbol is None:
        raise ValueError(f"identity {identity.company_key!r} binds no {SOURCE_FACTS} symbol (CIK)")
    text = symbol.strip()
    if not text.isdigit():
        raise ValueError(f"SEC CIK must be decimal digits, got {symbol!r}")
    return f"{int(text):010d}"


def _document(raw: Sequence[RawPayload], name: str) -> RawPayload | None:
    for payload in raw:
        if payload.name == name or payload.name.endswith(f"/{name}"):
            return payload
    return None


def _shard_payloads(raw: Sequence[RawPayload], submissions: RawPayload) -> tuple[RawPayload, ...]:
    return tuple(
        payload
        for payload in raw
        if payload is not submissions and payload.name not in (FACTS_DOCUMENT, SUBMISSIONS_DOCUMENT)
    )


def _json_object(payload: RawPayload, fatal: list[str]) -> Mapping[str, Any] | None:
    text = payload.content.decode("utf-8", errors="replace").strip()
    if not text or text.startswith("<"):
        fatal.append(f"not_json: {payload.name}")
        return None
    try:
        body = json.loads(text)
    except json.JSONDecodeError:
        fatal.append(f"not_json: {payload.name}")
        return None
    if not isinstance(body, Mapping):
        fatal.append(f"unexpected_shape: {payload.name}")
        return None
    return body


def _cik_text(value: object) -> str | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return f"{value:010d}"
    if isinstance(value, str) and value.strip().isdigit():
        return f"{int(value.strip()):010d}"
    return None


def _cik_matches(value: object, cik10: str) -> bool:
    return _cik_text(value) == cik10


def _parse_date(value: object) -> date | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _parse_datetime(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        moment = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.astimezone(UTC)


def _parse_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _decimal_text(value: object) -> str | None:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = Decimal(str(value).strip())
    except InvalidOperation:
        return None
    if not number.is_finite():
        return None
    return format(number.normalize(), "f")


def _items(value: object) -> tuple[str, ...]:
    if not isinstance(value, str) or not value.strip():
        return ()
    return tuple(part.strip() for part in value.split(",") if part.strip())


def _text_or_none(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value
    return None


