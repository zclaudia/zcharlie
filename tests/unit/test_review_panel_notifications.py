"""In-app notice for noteworthy review rounds (design §10.5)."""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select

from investment_agent.config import Settings
from investment_agent.review_panel import notifications
from investment_agent.review_panel.comparison import (
    ComparisonResult,
    DivergenceLevel,
    SeatChange,
)
from investment_agent.review_panel.contracts import (
    CompanyStance,
    Dimension,
    TradeView,
)
from investment_agent.review_panel.notifications import (
    FORBIDDEN_REMINDER_WORDS,
    notify_round_completed,
    render_notification_text,
)
from investment_agent.review_panel.service import finalize_round
from investment_agent.storage.models.monitoring import InvestorNotification
from investment_agent.storage.models.review_panel import (
    ReviewRoundRecord,
    ReviewSeatRunRecord,
)
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.review_panel import RoundDetail
from tests.support.review_panel import (
    base_pack,
    create_round_with_opinions,
    divergent_opinions,
)


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "api_key": "fixture",
        "review_panel_enabled": True,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


def _comparison(*, high: bool = False, flip: bool = False) -> ComparisonResult:
    reasons: list[str] = []
    if high:
        reasons.append("high_divergence")
    if flip:
        reasons.append("stance_flip")
    return ComparisonResult(
        stance_distribution={CompanyStance.POSITIVE: 1, CompanyStance.NEGATIVE: 1},
        trade_view_distribution={TradeView.HOLD_WATCH: 2},
        dimension_matrix={},
        divergent_dimensions=[Dimension.VALUATION] if high else [],
        divergent_materials={Dimension.VALUATION: True} if high else {},
        stance_range=2 if high else 0,
        divergence_score=2 if high else 0,
        divergence_level=DivergenceLevel.HIGH if high else DivergenceLevel.LOW,
        evidence_overlap=[],
        falsifiers_by_dimension=[],
        seat_changes=[
            SeatChange(
                seat_id="bear",
                previous_stance=CompanyStance.POSITIVE,
                current_stance=CompanyStance.NEGATIVE,
                stance_flipped=True,
            )
        ]
        if flip
        else [],
        notify_reasons=reasons,
        failed_seats=[],
    )


def _detail(*, round_id: int = 7, listing_id: int = 1) -> RoundDetail:
    round_row = ReviewRoundRecord(
        id=round_id,
        listing_id=listing_id,
        trigger="manual",
        panel_id="default",
        panel_snapshot={"seats": [{"seat_id": "bear", "lens_name": "空头"}]},
        as_of=date(2026, 11, 6),
        evidence_cutoff=datetime(2026, 11, 6, tzinfo=UTC),
        base_pack=base_pack(),
        base_pack_hash="0" * 64,
        status="completed",
        reserved_tokens=0,
        reserved_requests=0,
        settled_usage={},
        completed_at=datetime(2026, 11, 6, 8, tzinfo=UTC),
    )
    seat = ReviewSeatRunRecord(
        id=1,
        round_id=round_id,
        seat_id="bear",
        lens_id="bear",
        lens_ref="bear.v1",
        model_ref="primary/reason",
        model_snapshot={},
        status="succeeded",
    )
    return RoundDetail(round=round_row, seats=[seat], comparison=None)


async def _case_for(db_session, listing_id: int):
    from investment_agent.investor.common import ObjectRef
    from investment_agent.investor.foundations import CreateCase, InvestorService

    return await InvestorService(db_session).create_case(
        "local_owner",
        CreateCase(
            listing_ref=ObjectRef(kind="listing", id=listing_id, revision=1),
            question="复核这家公司",
            idempotency_key=uuid4(),
        ),
    )


async def _listing(db_session):
    return await ListingRepository(db_session).ensure_a_share_listing(
        "600036.SH", company_name="招商银行"
    )


@pytest.mark.parametrize(
    ("high", "flip", "expected"),
    [
        (True, False, ["分歧程度：高", "分歧维度：估值水平"]),
        (False, True, ["空头席位由偏正面转为偏负面"]),
        (True, True, ["分歧程度：高", "空头席位由偏正面转为偏负面"]),
    ],
)
def test_notification_text_mentions_stock_divergence_and_flips(
    high: bool, flip: bool, expected: list[str]
) -> None:
    text = render_notification_text(_detail(), _comparison(high=high, flip=flip))
    assert text is not None
    assert "招商银行" in text
    for phrase in expected:
        assert phrase in text
    for word in FORBIDDEN_REMINDER_WORDS:
        assert word not in text


def test_no_text_when_nothing_noteworthy() -> None:
    assert render_notification_text(_detail(), _comparison()) is None


@pytest.mark.parametrize(
    ("high", "flip", "expected_count"),
    [(True, False, 1), (False, True, 1), (True, True, 1), (False, False, 0)],
)
async def test_one_notification_per_round_and_reason_set(
    db_session, high: bool, flip: bool, expected_count: int
) -> None:
    listing = await _listing(db_session)
    await _case_for(db_session, listing.id)
    detail = _detail(listing_id=listing.id)
    comparison = _comparison(high=high, flip=flip)

    created = await notify_round_completed(
        db_session, detail, comparison, settings=_settings()
    )
    # A second call (retry/re-finalisation) must not duplicate.
    again = await notify_round_completed(
        db_session, detail, comparison, settings=_settings()
    )

    rows = list(
        (await db_session.execute(select(InvestorNotification))).scalars().all()
    )
    assert len(rows) == expected_count
    assert created is (expected_count == 1)
    assert again is False
    if expected_count:
        row = rows[0]
        assert row.key == f"review_round:{detail.round.id}"
        payload = row.payload
        assert payload["listing_ref"] == {
            "kind": "listing",
            "id": listing.id,
            "revision": 1,
        }
        assert payload["round_id"] == detail.round.id
        assert payload["result_refs"] == [
            {"kind": "review", "id": str(detail.round.id), "revision": 1}
        ]
        assert payload["review"]["notify_reasons"] == comparison.notify_reasons
        body = json.dumps(payload, ensure_ascii=False)
        for word in FORBIDDEN_REMINDER_WORDS:
            assert word not in body


async def test_disabled_feature_creates_no_notification(db_session) -> None:
    listing = await _listing(db_session)
    await _case_for(db_session, listing.id)
    created = await notify_round_completed(
        db_session,
        _detail(listing_id=listing.id),
        _comparison(high=True),
        settings=_settings(review_panel_enabled=False),
    )
    assert created is False
    assert (
        await db_session.scalar(
            select(func.count()).select_from(InvestorNotification)
        )
        == 0
    )


async def test_notification_is_case_scoped(db_session) -> None:
    listing = await _listing(db_session)
    # No investor case exists: cannot write into the case-scoped inbox.
    created = await notify_round_completed(
        db_session,
        _detail(listing_id=listing.id),
        _comparison(high=True),
        settings=_settings(),
    )
    assert created is False


async def test_finalize_writes_notification_once_and_survives_insert_failure(
    db_session, monkeypatch: pytest.MonkeyPatch
) -> None:
    listing = await _listing(db_session)
    await _case_for(db_session, listing.id)
    settings = _settings()

    async def enabled(_session):  # noqa: ANN001, ANN202
        return settings

    monkeypatch.setattr(notifications, "_effective_settings", enabled)

    round_id = await create_round_with_opinions(
        db_session, listing.id, divergent_opinions()
    )
    assert await finalize_round(db_session, round_id) is True
    assert (
        await db_session.scalar(
            select(func.count()).select_from(InvestorNotification)
        )
        == 1
    )
    # Re-finalisation is a no-op and cannot duplicate the notice.
    assert await finalize_round(db_session, round_id) is False
    assert (
        await db_session.scalar(
            select(func.count()).select_from(InvestorNotification)
        )
        == 1
    )

    other_listing = await ListingRepository(db_session).ensure_a_share_listing(
        "000001.SZ", company_name="平安银行"
    )
    await _case_for(db_session, other_listing.id)

    async def broken(*_args, **_kwargs):  # noqa: ANN002, ANN003, ANN202
        raise RuntimeError("notification insert failed")

    monkeypatch.setattr(notifications, "_write_notification", broken)
    failing_round = await create_round_with_opinions(
        db_session, other_listing.id, divergent_opinions()
    )
    assert await finalize_round(db_session, failing_round) is True

    from investment_agent.storage.models.review_panel import ReviewRoundRecord

    row = await db_session.get(ReviewRoundRecord, failing_round)
    assert row is not None and row.status == "completed"
