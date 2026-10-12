"""Research material repositories: version chains, interval filters and the
immutability guards on both tables (plan 02 T2.5)."""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest

from investment_agent.storage.models.research_material import (
    ImmutableResearchMaterialError,
    ResearchDataset,
    ResearchSourceBinding,
)
from investment_agent.storage.repositories.research_material import (
    ResearchDatasetRepository,
    ResearchSourceBindingRepository,
)

CAPTURED_AT = datetime(2026, 10, 12, 3, 30, tzinfo=UTC)
PERIOD_END = date(2024, 9, 29)


def make_dataset(**overrides: Any) -> ResearchDataset:
    fields: dict[str, Any] = {
        "kind": "annual_facts",
        "subject_kind": "company",
        "subject_id": 7,
        "source": "eastmoney_hk",
        "schema_version": 1,
        "adapter_version": "hk-native-1",
        "period_end": PERIOD_END,
        "period_basis": "annual",
        "content_hash": "a" * 64,
        "captured_at": CAPTURED_AT,
        "payload": {"facts": []},
    }
    fields.update(overrides)
    return ResearchDataset(**fields)


def make_binding(**overrides: Any) -> ResearchSourceBinding:
    fields: dict[str, Any] = {
        "subject_kind": "company",
        "subject_id": 7,
        "source": "eastmoney_hk",
        "source_symbol": "00700.HK",
        "valid_from": date(2025, 1, 1),
        "valid_to": None,
        "verified_at": CAPTURED_AT,
        "verified_by": "pilot_list@2026-10-12",
        "payload": {},
    }
    fields.update(overrides)
    return ResearchSourceBinding(**fields)


# --- dataset versions ------------------------------------------------------


async def test_insert_version_numbers_revisions_and_links_previous(db_session) -> None:
    repo = ResearchDatasetRepository(db_session)
    first = await repo.insert_version(make_dataset(content_hash="a" * 64))
    assert (first.revision, first.previous_id) == (1, None)
    second = await repo.insert_version(
        make_dataset(content_hash="b" * 64, captured_at=CAPTURED_AT + timedelta(days=1))
    )
    assert (second.revision, second.previous_id) == (2, first.id)
    third = await repo.insert_version(
        make_dataset(content_hash="c" * 64, captured_at=CAPTURED_AT + timedelta(days=2))
    )
    assert (third.revision, third.previous_id) == (3, second.id)


async def test_revision_counter_is_scoped_to_the_dataset_key(db_session) -> None:
    repo = ResearchDatasetRepository(db_session)
    await repo.insert_version(make_dataset(content_hash="a" * 64))
    other_period = await repo.insert_version(
        make_dataset(content_hash="d" * 64, period_end=date(2023, 9, 30))
    )
    assert (other_period.revision, other_period.previous_id) == (1, None)
    other_source = await repo.insert_version(make_dataset(content_hash="e" * 64, source="sec"))
    assert (other_source.revision, other_source.previous_id) == (1, None)
    other_subject = await repo.insert_version(make_dataset(content_hash="f" * 64, subject_id=8))
    assert (other_subject.revision, other_subject.previous_id) == (1, None)


async def test_null_period_filing_index_rows_chain_versions(db_session) -> None:
    repo = ResearchDatasetRepository(db_session)
    first = await repo.insert_version(
        make_dataset(kind="filing_index", period_end=None, period_basis=None, content_hash="1" * 64)
    )
    second = await repo.insert_version(
        make_dataset(kind="filing_index", period_end=None, period_basis=None, content_hash="2" * 64)
    )
    assert (first.revision, second.revision) == (1, 2)
    assert second.previous_id == first.id


async def test_latest_returns_the_highest_revision_of_the_key(db_session) -> None:
    repo = ResearchDatasetRepository(db_session)
    await repo.insert_version(make_dataset(content_hash="a" * 64))
    middle = await repo.insert_version(make_dataset(content_hash="b" * 64))
    newest = await repo.insert_version(make_dataset(content_hash="c" * 64))
    await repo.insert_version(make_dataset(content_hash="d" * 64, period_end=date(2023, 9, 30)))
    latest = await repo.latest(
        kind="annual_facts", subject_kind="company", subject_id=7, period_end=PERIOD_END
    )
    assert latest is not None and latest.id == newest.id and latest.id != middle.id
    # period_end=None means the null-period (filing_index) rows, not "any period".
    assert (
        await repo.latest(
            kind="annual_facts", subject_kind="company", subject_id=7, period_end=date(2020, 1, 1)
        )
        is None
    )
    filing = await repo.insert_version(
        make_dataset(kind="filing_index", period_end=None, period_basis=None, content_hash="e" * 64)
    )
    null_period = await repo.latest(
        kind="filing_index", subject_kind="company", subject_id=7, period_end=None
    )
    assert null_period is not None and null_period.id == filing.id


async def test_history_is_ordered_by_revision(db_session) -> None:
    repo = ResearchDatasetRepository(db_session)
    for suffix in "abc":
        await repo.insert_version(make_dataset(content_hash=suffix * 64))
    await repo.insert_version(make_dataset(content_hash="d" * 64, subject_id=8))
    history = await repo.history(kind="annual_facts", subject_kind="company", subject_id=7)
    assert [row.revision for row in history] == [1, 2, 3]
    assert [row.content_hash for row in history] == ["a" * 64, "b" * 64, "c" * 64]


async def test_by_content_hash_finds_the_version(db_session) -> None:
    repo = ResearchDatasetRepository(db_session)
    first = await repo.insert_version(make_dataset(content_hash="a" * 64))
    second = await repo.insert_version(make_dataset(content_hash="b" * 64))
    found = await repo.by_content_hash(content_hash="b" * 64)
    assert found is not None and found.id == second.id and found.id != first.id
    assert await repo.by_content_hash(content_hash="0" * 64) is None


async def test_dataset_rows_are_immutable(db_session) -> None:
    repo = ResearchDatasetRepository(db_session)
    row_id = (await repo.insert_version(make_dataset())).id
    await db_session.commit()
    row = await db_session.get(ResearchDataset, row_id)
    assert row is not None
    row.payload = {"facts": [], "tampered": True}
    with pytest.raises(ImmutableResearchMaterialError):
        await db_session.flush()
    await db_session.rollback()
    reloaded = await db_session.get(ResearchDataset, row_id)
    assert reloaded is not None
    await db_session.refresh(reloaded)
    assert reloaded.payload == {"facts": []}

    await db_session.delete(reloaded)
    with pytest.raises(ImmutableResearchMaterialError):
        await db_session.flush()
    await db_session.rollback()
    assert await db_session.get(ResearchDataset, row_id) is not None


async def test_binding_rows_are_immutable(db_session) -> None:
    binding = make_binding()
    db_session.add(binding)
    await db_session.flush()
    binding_id = binding.id
    await db_session.commit()
    row = await db_session.get(ResearchSourceBinding, binding_id)
    assert row is not None
    row.source_symbol = "0007.HK"
    with pytest.raises(ImmutableResearchMaterialError):
        await db_session.flush()
    await db_session.rollback()
    reloaded = await db_session.get(ResearchSourceBinding, binding_id)
    assert reloaded is not None
    await db_session.refresh(reloaded)
    assert reloaded.source_symbol == "00700.HK"

    await db_session.delete(reloaded)
    with pytest.raises(ImmutableResearchMaterialError):
        await db_session.flush()
    await db_session.rollback()
    assert await db_session.get(ResearchSourceBinding, binding_id) is not None


# --- source bindings -------------------------------------------------------


async def test_list_for_orders_intervals_and_filters_by_date(db_session) -> None:
    db_session.add_all(
        [
            make_binding(
                source_symbol="0700.HK",
                valid_from=date(2024, 1, 1),
                valid_to=date(2024, 12, 31),
            ),
            make_binding(
                source_symbol="00700.HK",
                valid_from=date(2025, 1, 1),
                valid_to=None,
            ),
        ]
    )
    await db_session.flush()
    repo = ResearchSourceBindingRepository(db_session)
    assert [row.source_symbol for row in await repo.list_for("company", 7)] == [
        "0700.HK",
        "00700.HK",
    ]
    covering_2024 = await repo.list_for("company", 7, at=date(2024, 6, 1))
    assert [row.source_symbol for row in covering_2024] == ["0700.HK"]
    assert [
        row.source_symbol for row in await repo.list_for("company", 7, at=date(2025, 6, 1))
    ] == ["00700.HK"]
    assert await repo.list_for("company", 7, at=date(2023, 12, 31)) == []
    # Start is inclusive; the end is exclusive.
    assert [
        row.source_symbol for row in await repo.list_for("company", 7, at=date(2024, 12, 31))
    ] == []
    assert [
        row.source_symbol for row in await repo.list_for("company", 7, at=date(2025, 1, 1))
    ] == ["00700.HK"]
    # The same id in another subject space is a different subject.
    assert await repo.list_for("listing", 7) == []


async def test_symbol_for_picks_the_covering_interval(db_session) -> None:
    db_session.add_all(
        [
            make_binding(
                source_symbol="0700.HK",
                valid_from=date(2024, 1, 1),
                valid_to=date(2024, 12, 31),
            ),
            make_binding(
                source_symbol="00700.HK",
                valid_from=date(2025, 1, 1),
                valid_to=None,
            ),
        ]
    )
    await db_session.flush()
    repo = ResearchSourceBindingRepository(db_session)
    assert await repo.symbol_for("company", 7, "eastmoney_hk") == "00700.HK"
    assert await repo.symbol_for("company", 7, "eastmoney_hk", at=date(2024, 6, 1)) == "0700.HK"
    assert await repo.symbol_for("company", 7, "yahoo") is None
    assert await repo.symbol_for("listing", 7, "eastmoney_hk") is None


async def test_repositories_reject_unknown_vocabulary(db_session) -> None:
    dataset_repo = ResearchDatasetRepository(db_session)
    binding_repo = ResearchSourceBindingRepository(db_session)
    with pytest.raises(ValueError, match="subject_kind"):
        await binding_repo.list_for("fund", 7)
    with pytest.raises(ValueError, match="subject_kind"):
        await dataset_repo.latest(kind="annual_facts", subject_kind="fund", subject_id=7)
    with pytest.raises(ValueError, match="kind"):
        await dataset_repo.latest(kind="annual", subject_kind="company", subject_id=7)
    with pytest.raises(ValueError, match="content_hash"):
        await dataset_repo.insert_version(make_dataset(content_hash=""))


async def test_source_binding_expiry_and_handover_are_exclusive(db_session) -> None:
    handover = date(2025, 1, 1)
    db_session.add_all([
        make_binding(source_symbol="old", valid_from=date(2024, 1, 1), valid_to=handover),
        make_binding(source_symbol="new", valid_from=handover),
    ])
    await db_session.flush()
    repo = ResearchSourceBindingRepository(db_session)
    assert [row.source_symbol for row in await repo.list_for("company", 7, at=handover)] == ["new"]
    assert await repo.symbol_for("company", 7, "eastmoney_hk", at=handover) == "new"
    assert await repo.symbol_for("company", 7, "eastmoney_hk", at=date(2024, 12, 31)) == "old"


@pytest.mark.parametrize("model_name", ["dataset", "binding"])
@pytest.mark.parametrize("operation", ["update", "delete", "replace"])
async def test_bulk_sql_cannot_mutate_saved_material(db_session, model_name, operation) -> None:
    from sqlalchemy import delete, text, update
    from sqlalchemy.exc import IntegrityError

    model = ResearchDataset if model_name == "dataset" else ResearchSourceBinding
    row = make_dataset(revision=1) if model_name == "dataset" else make_binding()
    db_session.add(row)
    await db_session.commit()
    row_id = row.id
    statement = (
        update(model).where(model.id == row_id).values(payload={"tampered": True})
        if operation == "update" else (
            delete(model).where(model.id == row_id) if operation == "delete"
            else text(f"INSERT OR REPLACE INTO {model.__tablename__} "
                      f"SELECT * FROM {model.__tablename__} WHERE id = :id").bindparams(id=row_id)
        )
    )
    with pytest.raises(IntegrityError, match="immutable"):
        await db_session.execute(statement)
    await db_session.rollback()
    saved = await db_session.get(model, row_id)
    assert saved is not None and "tampered" not in saved.payload
