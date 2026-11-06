"""source_policy as a config document (config-docs-plan §3.1 / §4.8, WP2).

parse-as-validation of the whole approval record (every branch of
``parse_source_policy``), the store round-trip, the DB-backed runtime read of
:class:`~investment_agent.materials.source_policy.evaluate_source_policy`, and
the audit-hash continuity: a policy edited through ``ConfigDocStore.put``
changes the ``revision@digest`` label subsequent audit records carry.
"""

from __future__ import annotations

from collections.abc import Iterator
from copy import deepcopy
from datetime import date
from typing import Any

import pytest

from investment_agent.config import Settings
from investment_agent.config_docs import loader, registry
from investment_agent.config_docs.store import ConfigDocStore, ConfigDocValidationError
from investment_agent.domain.security import ControlledIdentity, SecurityType, SourceBinding
from investment_agent.materials.source_policy import (
    _policy_metadata,
    evaluate_source_policy,
    load_source_policy,
    parse_source_policy,
    source_policy_revision_label,
)

HK_SOURCE = "eastmoney.hk.native.v1"


def _identity() -> ControlledIdentity:
    return ControlledIdentity(
        market_code="HKEX",
        ticker="00005",
        company_key="hsbc-holdings",
        company_name="HSBC Holdings plc",
        company_name_zh="汇丰控股",
        security_type=SecurityType.ORDINARY,
        quote_currency="HKD",
        report_currency="USD",
        fiscal_year_end_note="December",
        sources=(SourceBinding(source=HK_SOURCE, symbol="00005.HK"),),
        valid_from=date(2020, 1, 1),
    )


def _entry(
    *,
    licensing_status: str = "unknown",
    technical_status: str = "not_accepted",
) -> dict[str, Any]:
    """A structurally valid source approval row (statuses stay runtime gates)."""

    return {
        "licensing": {"status": licensing_status, "company_keys": [], "citation": ""},
        "technical_acceptance": {
            "status": technical_status,
            "company_keys": [],
            "adapter_version": "",
            "reports": [],
        },
    }


def _document() -> dict[str, Any]:
    return {
        "schema_version": 1,
        "revision": "unit-policy-1",
        "sources": {HK_SOURCE: _entry(), "sec.companyfacts.v1": _entry()},
    }


def _approved_licensing(document: dict[str, Any]) -> dict[str, Any]:
    """Approve the HK source's licensing for the identity's company."""

    edited = deepcopy(document)
    licensing = edited["sources"][HK_SOURCE]["licensing"]
    licensing.update(
        {
            "status": "approved",
            "company_keys": ["hsbc-holdings"],
            "citation": "https://example.com/license",
        }
    )
    return edited


@pytest.fixture(autouse=True)
def _isolated_loader_cache(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """The loader cache is process-global: keep it (and its broken flag) clean."""

    monkeypatch.setattr(loader, "_refresh_broken", False)
    loader.invalidate()
    yield
    loader.invalidate()


# --- parse: the real baseline + every rejection branch ------------------------------


def test_parse_accepts_the_repo_baseline() -> None:
    spec = registry.spec_for("source_policy")
    document = registry.baseline_document(spec)
    assert parse_source_policy(document) == document
    # The pre-DB file-reading path (cold cache) serves exactly that document.
    assert load_source_policy() == document


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda d: d.update(schema_version=2), id="schema_version"),
        pytest.param(lambda d: d.update(revision="Not A Slug"), id="revision"),
        pytest.param(lambda d: d.update(revision=7), id="revision_not_a_string"),
        pytest.param(lambda d: d.update(sources=[]), id="sources_not_a_mapping"),
    ],
)
def test_parse_rejects_a_broken_document_header(mutate: Any) -> None:
    document = _document()
    mutate(document)
    with pytest.raises(
        ValueError, match="schema_version must be 1, revision a slug, sources a mapping"
    ):
        parse_source_policy(document)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda e: e.clear(), id="entry_not_a_mapping"),
        pytest.param(lambda e: e.pop("licensing"), id="licensing_missing"),
        pytest.param(lambda e: e.update(licensing=[]), id="licensing_not_a_mapping"),
        pytest.param(lambda e: e.update(technical_acceptance="x"), id="technical_not_a_mapping"),
    ],
)
def test_parse_rejects_a_broken_entry_or_section(mutate: Any) -> None:
    document = _document()
    mutate(document["sources"][HK_SOURCE])
    with pytest.raises(ValueError, match=r"source_policy\.sources\.eastmoney\.hk\.native\.v1"):
        parse_source_policy(document)


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda e: e["licensing"].update(status=1), id="licensing_status"),
        pytest.param(
            lambda e: e["licensing"].update(company_keys="hsbc"), id="licensing_keys_not_a_list"
        ),
        pytest.param(
            lambda e: e["licensing"].update(company_keys=[1]), id="licensing_keys_not_strings"
        ),
        pytest.param(lambda e: e["licensing"].update(citation=None), id="licensing_citation"),
        pytest.param(
            lambda e: e["technical_acceptance"].update(status=True), id="technical_status"
        ),
        pytest.param(
            lambda e: e["technical_acceptance"].update(company_keys={"a": 1}), id="technical_keys"
        ),
        pytest.param(
            lambda e: e["technical_acceptance"].update(adapter_version=2), id="adapter_version"
        ),
        pytest.param(
            lambda e: e["technical_acceptance"].update(reports={}), id="reports_not_a_list"
        ),
    ],
)
def test_parse_rejects_broken_rows(mutate: Any) -> None:
    document = _document()
    mutate(document["sources"][HK_SOURCE])
    with pytest.raises(
        ValueError, match=r"eastmoney\.hk\.native\.v1\.(licensing|technical_acceptance)"
    ):
        parse_source_policy(document)


@pytest.mark.parametrize(
    "report",
    [
        pytest.param({"citation": "https://example.com"}, id="missing_observed_on"),
        pytest.param(
            {"observed_on": "not-a-date", "citation": "https://example.com"}, id="bad_date"
        ),
        pytest.param(
            {"observed_on": "2026-02-30", "citation": "https://example.com"}, id="impossible_date"
        ),
        pytest.param({"observed_on": "2026-01-02"}, id="missing_citation"),
        pytest.param("https://example.com", id="report_not_a_mapping"),
    ],
)
def test_parse_rejects_broken_report_rows(report: Any) -> None:
    document = _document()
    document["sources"][HK_SOURCE]["technical_acceptance"]["reports"] = [report]
    with pytest.raises(ValueError, match=r"reports\[0\]: needs observed_on"):
        parse_source_policy(document)


def test_parse_accepts_three_cited_reports() -> None:
    document = _document()
    document["sources"][HK_SOURCE]["technical_acceptance"]["reports"] = [
        {"observed_on": "2026-09-01", "citation": "https://example.com/r1"},
        {"observed_on": "2026-09-08", "citation": "https://example.com/r2"},
        {"observed_on": "2026-09-15", "citation": "https://example.com/r3"},
    ]
    assert parse_source_policy(document) == document


# --- store: validated writes of the whole approval record ---------------------------


async def test_put_round_trip_and_baseline_import(db_session) -> None:
    store = ConfigDocStore(db_session)
    assert await store.revision("source_policy") == 0
    document = _document()
    assert await store.put("source_policy", document, actor="ui", note="approve") == 1

    value, row = await store.get("source_policy")
    assert value == document
    assert row is not None and (row.updated_by, row.imported_at) == ("ui", None)
    (change,) = await store.history("source_policy")
    assert (change.actor, change.note, change.old_value_json) == ("ui", "approve", None)

    # A broken document never reaches the row; the store surfaces the parser path.
    broken = _document()
    broken["sources"][HK_SOURCE]["licensing"]["status"] = None
    with pytest.raises(ConfigDocValidationError, match="licensing.status: expected a string"):
        await store.put("source_policy", broken, actor="ui", note="bad")
    assert await store.revision("source_policy") == 1

    # The explicit re-import restores the baseline as one more audited write.
    baseline = registry.baseline_document(registry.spec_for("source_policy"))
    assert (
        await store.import_baseline("source_policy", actor="ui", note="回到基线", force=True)
        is True
    )
    value, row = await store.get("source_policy")
    assert value == baseline and row.imported_at is not None


# --- runtime: the evaluator serves the DB-backed document ---------------------------


def test_cold_cache_reads_the_baseline_file(monkeypatch: pytest.MonkeyPatch) -> None:
    loader.invalidate()
    assert load_source_policy() == registry.baseline_document(registry.spec_for("source_policy"))


def test_test_injection_still_overrides_the_document(monkeypatch: pytest.MonkeyPatch) -> None:
    """``approvals`` / a patched loader keep working above the DB-backed read."""

    injected = _approved_licensing(_document())
    monkeypatch.setattr(
        "investment_agent.materials.source_policy.load_source_policy", lambda: injected
    )
    identity = _identity()
    decision = evaluate_source_policy(
        HK_SOURCE,
        identity,
        settings=Settings(api_key="test"),
        adapter_version="",
        global_enabled=True,
    )
    assert decision.licensing == "approved"


async def test_evaluate_source_policy_serves_a_db_edited_document(db_session) -> None:
    """A UI edit (store.put) reaches the evaluator without any restart."""

    loader.invalidate()
    identity = _identity()
    settings = Settings(api_key="test", materials_hk_refresh_enabled=True)

    before = evaluate_source_policy(
        HK_SOURCE, identity, settings=settings, adapter_version="1", global_enabled=True
    )
    assert "licensing_unknown" in before.reason_codes
    assert before.licensing == "unknown"

    await ConfigDocStore(db_session).put(
        "source_policy", _approved_licensing(_document()), actor="ui", note="approve HK licensing"
    )
    after = evaluate_source_policy(
        HK_SOURCE, identity, settings=settings, adapter_version="1", global_enabled=True
    )
    assert "licensing_unknown" not in after.reason_codes
    assert after.licensing == "approved"
    assert after.reason == "technical_acceptance_missing_or_mismatched"
    # The read itself is the stored document (the baseline has no approvals).
    assert load_source_policy()["sources"][HK_SOURCE]["licensing"]["status"] == "approved"


# --- audit continuity (§4.8): the revision@digest label follows the DB document -----


async def test_audit_records_carry_the_db_edited_policy_digest(db_session) -> None:
    """The ``revision@digest`` label audit records persist moves with the document.

    ``SourceDecision.policy_revision`` (this label) is what the material
    refresh / probe audit records persist; ``source_policy_revision_label`` is
    the shared derivation for any other audit producer.
    """

    loader.invalidate()
    document = _document()
    label_before = source_policy_revision_label()
    assert (
        label_before
        == _policy_metadata(registry.baseline_document(registry.spec_for("source_policy")))[1]
    )

    edited = _approved_licensing(document)
    edited["revision"] = "unit-policy-2"
    await ConfigDocStore(db_session).put("source_policy", edited, actor="ui", note="approve")

    label_after = source_policy_revision_label()
    expected = _policy_metadata(edited)[1]
    assert label_after == expected
    assert label_after != label_before
    assert label_after.startswith("unit-policy-2@")

    decision = evaluate_source_policy(
        HK_SOURCE,
        _identity(),
        settings=Settings(api_key="test"),
        adapter_version="",
        global_enabled=True,
    )
    record = decision.as_dict()
    assert record["policy_revision"] == label_after
