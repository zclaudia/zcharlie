"""Round creation for multi-perspective review (design §4.1).

``create_round`` is the single entry point other layers (API, scheduler,
change-trigger) call. It validates the watchlist entry, resolves the panel and
the explicit model references (no substitution), reserves the whole round's
quota, freezes the data boundary and the public base pack, and enqueues one
``review_seat`` pipeline run per seat — all in one transaction. Any rejection
raises :class:`RoundRejected` with a stable ``code`` and a Chinese message; a
rejected round leaves no round, seat or queue rows behind.

Quota reservation is atomic in the practical SQLite sense: the daily totals are
checked before the insert and re-checked inside the same write transaction
after the round row is flushed, so a concurrent writer that committed in
between cannot make this round exceed the cap. ``review_panel_daily_rounds = 0``
means unlimited (the settings page documents it that way).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Sequence
from datetime import UTC, datetime, time

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ, shanghai_today
from investment_agent.config import Settings
from investment_agent.domain.a_share import format_listing_ticker, parse_a_share_ticker
from investment_agent.review_panel.comparison import (
    ALGORITHM_VERSION,
    SeatInput,
    compare_round,
)
from investment_agent.review_panel.contracts import (
    MAX_SEATS_PER_ROUND,
    REQUIRED_SEAT_CAPABILITIES,
    RoundStatus,
    RoundTrigger,
    SeatOpinion,
    SeatStatus,
)
from investment_agent.review_panel.lenses import Lens, LensSet, load_review_lenses
from investment_agent.review_panel.panels import (
    PanelSpec,
    ReviewPanels,
    SeatSpec,
    parse_review_panels,
)
from investment_agent.settings.resolution import (
    ModelUnavailableError,
    ResolvedModel,
    resolve_model_ref,
)
from investment_agent.storage.models.analysis import ResearchChangeSet, ResearchSnapshot
from investment_agent.storage.models.review_panel import (
    ReviewRoundRecord,
    ReviewSeatRunRecord,
)
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.storage.repositories.pipeline_run import PipelineRunRepository
from investment_agent.storage.repositories.research_change import ResearchChangeRepository
from investment_agent.storage.repositories.research_snapshot import (
    ResearchSnapshotRepository,
    score_item_from_payload,
)
from investment_agent.storage.repositories.review_panel import (
    ReviewPanelRepository,
    SeatPlan,
)
from investment_agent.storage.repositories.watchlist import WatchlistRepository

REVIEW_SEAT_KIND = "review_seat"
BASE_PACK_SCHEMA_VERSION = "review-base-pack.v1"
BASE_SCORE_REF = "base:score"
BASE_SNAPSHOT_REF = "base:snapshot"
BASE_CHANGES_REF = "base:changes"
BASE_AGENT_VIEW_REF = "base:agent_view"
BASE_PACK_REFS: tuple[str, ...] = (
    BASE_SCORE_REF,
    BASE_SNAPSHOT_REF,
    BASE_CHANGES_REF,
    BASE_AGENT_VIEW_REF,
)
"""Stable refs a seat may cite for the public base pack (design §4.3 item 4)."""

_SCORE_FIELDS = (
    "investment_score",
    "research_confidence_score",
    "strategy_label",
    "needs_agent_review",
    "scoring_version",
    "source_mode",
    "freshness",
)
_MAX_CHANGES_IN_PACK = 20
_MAX_AGENT_GAPS = 8


class RoundRejected(Exception):  # noqa: N818 - the code contract names it this way
    """A round cannot be created. ``code`` is stable; ``message`` is Chinese."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def canonical_json(value: object) -> str:
    """Canonical JSON used for the base-pack hash (stable across runs)."""

    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )


def base_pack_hash(pack: dict[str, object]) -> str:
    """SHA-256 of the canonical base pack."""

    return hashlib.sha256(canonical_json(pack).encode("utf-8")).hexdigest()


def model_snapshot(resolved: ResolvedModel) -> dict[str, object]:
    """Credential-free model identity frozen on a seat and in the round snapshot."""

    model = resolved.model
    invocation = canonical_json(resolved.agent_values())
    return {
        "ref": resolved.ref,
        "provider_id": resolved.provider.id,
        "provider_kind": resolved.provider.kind,
        "model_id": model.id,
        "model": model.model,
        "profile": model.profile,
        "output_mode": model.output_mode,
        "thinking": model.thinking,
        "capabilities": list(model.capabilities),
        "selection": resolved.selection.model_dump(mode="json"),
        "invocation_hash": hashlib.sha256(invocation.encode("utf-8")).hexdigest(),
    }


def build_base_pack(
    *,
    listing_id: int,
    ticker: str,
    snapshot: ResearchSnapshot,
    change_set: ResearchChangeSet | None,
    agent_view: dict[str, object] | None,
) -> dict[str, object]:
    """The public base pack every seat receives (design §4.3 item 4).

    Items carry stable refs (``base:*``) so opinions can cite them. Values are
    deliberately compact summaries, never the full stored payloads.
    """

    score = score_item_from_payload(snapshot.score_json)
    score_summary = {
        key: score[key] for key in _SCORE_FIELDS if key in score
    }
    items: list[dict[str, object]] = [
        {
            "ref": BASE_SCORE_REF,
            "label": "确定性评分摘要",
            "data": score_summary,
        },
        {
            "ref": BASE_SNAPSHOT_REF,
            "label": "研究快照摘要",
            "data": {
                "listing_id": listing_id,
                "ticker": ticker,
                "symbol": snapshot.symbol,
                "market": snapshot.market,
                "company_name": snapshot.company_name,
                "industry": snapshot.industry,
                "as_of": snapshot.as_of.isoformat(),
                "report_year": snapshot.report_year,
                "canonical_schema_version": snapshot.canonical_schema_version,
                "scoring_version": snapshot.scoring_version,
                "score_schema_version": snapshot.score_schema_version,
            },
        },
    ]
    if change_set is None:
        changes: dict[str, object] = {"available": False}
    else:
        diff = change_set.diff_json
        raw_changes = diff.get("changes")
        changes = {
            "available": True,
            "baseline": change_set.baseline,
            "has_meaningful_changes": change_set.has_meaningful_changes,
            "change_count": change_set.change_count,
            "meaningful_change_count": change_set.meaningful_change_count,
            "category_counts": diff.get("category_counts", {}),
            "source_changed": diff.get("source_changed", False),
            "changes": (
                list(raw_changes)[:_MAX_CHANGES_IN_PACK]
                if isinstance(raw_changes, list)
                else []
            ),
        }
    items.append(
        {"ref": BASE_CHANGES_REF, "label": "最近变化摘要", "data": changes}
    )
    if agent_view is not None:
        items.append(
            {"ref": BASE_AGENT_VIEW_REF, "label": "当前正式 Agent 观点摘要", "data": agent_view}
        )
    return {
        "schema_version": BASE_PACK_SCHEMA_VERSION,
        "listing_id": listing_id,
        "ticker": ticker,
        "as_of": snapshot.as_of.isoformat(),
        "items": items,
    }


async def _agent_view_summary(
    session: AsyncSession, *, listing_id: int, cutoff: datetime
) -> dict[str, object] | None:
    """The current formal Agent company view, condensed; ``None`` when absent.

    Best effort and read-only: a missing case, no current recommendation or an
    unreadable/inconsistent stored view simply omits the base-pack item.
    """

    try:
        from investment_agent.investor.contracts import Case
        from investment_agent.investor.publication import PublicationService
        from investment_agent.storage.models.investor import InvestorCase

        row = (
            await session.execute(
                select(InvestorCase).where(
                    InvestorCase.owner_id == "local_owner",
                    InvestorCase.listing_id == listing_id,
                )
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        case = Case.model_validate(row.payload)
        ref = case.current_recommendation_ref
        if ref is None or case.status == "archived":
            return None
        view = await PublicationService(session).get_recommendation(ref, at=cutoff)
        recommendation = view.recommendation
        status = getattr(view.status, "value", str(view.status))
        return {
            "recommendation_ref": f"{ref.kind}:{ref.id}",
            "status": status,
            "as_of": recommendation.as_of.isoformat(),
            "valid_until": recommendation.valid_until.isoformat(),
            "question_scope": recommendation.question_scope,
            "business_view": getattr(
                recommendation.business_view, "value", str(recommendation.business_view)
            ),
            "price_view": getattr(
                recommendation.price_view, "value", str(recommendation.price_view)
            ),
            "sufficiency": getattr(
                recommendation.sufficiency, "value", str(recommendation.sufficiency)
            ),
            "gaps": list(recommendation.gaps)[:_MAX_AGENT_GAPS],
            "limitation_reasons": list(view.limitation_reasons),
        }
    except Exception:  # noqa: BLE001 - the base pack must never block a round
        return None


def _shanghai_day_start() -> datetime:
    return datetime.combine(shanghai_today(), time.min, tzinfo=SHANGHAI_TZ).astimezone(UTC)


def _resolve_panel(
    settings: Settings, panel_id: str | None
) -> tuple[str, ReviewPanels, PanelSpec]:
    try:
        panels = parse_review_panels(settings.review_panels)
    except Exception as exc:  # ReviewPanelError is a ValueError
        raise RoundRejected("panel_not_configured", "评审组配置无效，请先在设置页检查") from exc
    if not panels.panels:
        raise RoundRejected("panel_not_configured", "尚未配置评审组，请先在设置页配置")
    chosen = panel_id or panels.default_panel
    panel = panels.panels.get(chosen)
    if panel is None:
        raise RoundRejected("unknown_panel", "所选评审组不存在，请刷新后重试")
    return chosen, panels, panel


def _select_seats(
    panel: PanelSpec, seat_ids: Sequence[str] | None
) -> list[SeatSpec]:
    seats: list[SeatSpec] = list(panel.seats)
    if seat_ids is None:
        return seats
    requested = list(seat_ids)
    if not requested or len(set(requested)) != len(requested):
        raise RoundRejected("unknown_seat", "未选择有效席位")
    by_id = {seat.seat_id: seat for seat in seats}
    selected: list[SeatSpec] = []
    for seat_id in requested:
        seat = by_id.get(seat_id)
        if seat is None:
            raise RoundRejected("unknown_seat", f"席位 {seat_id} 不存在，请刷新后重试")
        selected.append(seat)
    return selected


def _resolve_models(
    settings: Settings,
    seats: Sequence[SeatSpec],
    lenses: LensSet,
) -> list[tuple[SeatSpec, Lens, ResolvedModel]]:
    resolved: list[tuple[SeatSpec, Lens, ResolvedModel]] = []
    for seat in seats:
        lens = lenses.get(seat.lens_id)
        if lens is None:
            raise RoundRejected(
                "unknown_lens", f"视角 {seat.lens_id} 未配置，请先在设置页检查"
            )
        try:
            model = resolve_model_ref(
                settings,
                seat.model_ref,
                required=REQUIRED_SEAT_CAPABILITIES,
                label=f"评审席位 {seat.seat_id}",
            )
        except ModelUnavailableError as exc:
            raise RoundRejected("model_unavailable", str(exc)) from None
        resolved.append((seat, lens, model))
    return resolved


def _panel_snapshot(
    panel_id: str, panel_name: str, resolved: Sequence[tuple[SeatSpec, Lens, ResolvedModel]]
) -> dict[str, object]:
    return {
        "schema_version": "review-panel-snapshot.v1",
        "panel_id": panel_id,
        "name": panel_name,
        "seats": [
            {
                "seat_id": seat.seat_id,
                "lens_id": lens.id,
                "lens_ref": lens.ref,
                "lens_name": lens.name,
                # The prompt text is frozen with the round so a later lens
                # version cannot silently change what an already-created
                # round runs; the hash binds the exact text.
                "lens_prompt": lens.prompt,
                "lens_prompt_hash": hashlib.sha256(
                    lens.prompt.encode("utf-8")
                ).hexdigest(),
                "required_dimensions": [
                    dimension.value for dimension in lens.required_dimensions
                ],
                "model_ref": seat.model_ref,
                "model_snapshot": model_snapshot(model),
            }
            for seat, lens, model in resolved
        ],
    }


async def _latest_snapshot_and_ticker(
    session: AsyncSession, listing_id: int
) -> tuple[ResearchSnapshot, str]:
    listing = await ListingRepository(session).get(listing_id)
    if listing is None:
        raise RoundRejected("no_snapshot", "该股票尚无已提交的研究快照")
    parsed = parse_a_share_ticker(
        format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
    )
    if parsed is None:
        raise RoundRejected("no_snapshot", "该股票尚无已提交的研究快照")
    ticker = format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
    snapshot = await ResearchSnapshotRepository(session).latest(
        symbol=listing.ticker, market=parsed[1].suffix
    )
    if snapshot is None:
        raise RoundRejected("no_snapshot", "该股票尚无已提交的研究快照")
    return snapshot, ticker


async def create_round(
    session: AsyncSession,
    settings: Settings,
    *,
    listing_id: int,
    trigger: RoundTrigger,
    panel_id: str | None = None,
    seat_ids: Sequence[str] | None = None,
    trigger_ref: str | None = None,
) -> ReviewRoundRecord:
    """Create one review round and enqueue all of its seat runs atomically."""

    if not settings.review_panel_enabled:
        raise RoundRejected("disabled", "多视角评审功能未启用")

    watchlist = await WatchlistRepository(session).get_by_listing(listing_id)
    if watchlist is None or not watchlist.enabled:
        raise RoundRejected("not_on_watchlist", "该股票不在观察池中")

    repository = ReviewPanelRepository(session)
    if await repository.active_round_exists(listing_id):
        raise RoundRejected("active_round", "该股票已有进行中的评审，请等待本轮结束")

    if trigger is not RoundTrigger.MANUAL and trigger_ref is not None:
        existing = await repository.find_round_by_trigger(
            listing_id, trigger, trigger_ref
        )
        if existing is not None:
            raise RoundRejected("duplicate_trigger", "该触发来源已创建过评审轮次")

    chosen_panel, _panels, panel = _resolve_panel(settings, panel_id)
    seats = _select_seats(panel, seat_ids)
    if not 1 <= len(seats) <= MAX_SEATS_PER_ROUND:
        raise RoundRejected("unknown_seat", "未选择有效席位")
    resolved = _resolve_models(settings, seats, load_review_lenses())

    snapshot, ticker = await _latest_snapshot_and_ticker(session, listing_id)
    change_set = await ResearchChangeRepository(session).by_snapshot(snapshot.id)
    cutoff = datetime.now(UTC)
    agent_view = await _agent_view_summary(
        session, listing_id=listing_id, cutoff=cutoff
    )
    pack = build_base_pack(
        listing_id=listing_id,
        ticker=ticker,
        snapshot=snapshot,
        change_set=change_set,
        agent_view=agent_view,
    )
    pack_hash = base_pack_hash(pack)

    seat_count = len(resolved)
    reserved_tokens = seat_count * settings.review_panel_seat_tokens
    reserved_requests = seat_count * settings.review_panel_seat_requests
    day_start = _shanghai_day_start()
    await _check_quota(
        repository,
        settings,
        day_start=day_start,
        seat_count=seat_count,
        inclusive=False,
    )

    plans = [
        SeatPlan(
            seat_id=seat.seat_id,
            lens_id=lens.id,
            lens_ref=lens.ref,
            model_ref=seat.model_ref,
            model_snapshot=model_snapshot(model),
        )
        for seat, lens, model in resolved
    ]

    async with session.begin_nested():
        round_row = await repository.create_round(
            listing_id=listing_id,
            trigger=trigger,
            seats=plans,
            panel_id=chosen_panel,
            panel_snapshot=_panel_snapshot(chosen_panel, panel.name, resolved),
            as_of=snapshot.as_of,
            evidence_cutoff=cutoff,
            base_pack=pack,
            base_pack_hash=pack_hash,
            reserved_tokens=reserved_tokens,
            reserved_requests=reserved_requests,
            trigger_ref=trigger_ref,
        )
        detail = await repository.get_round(round_row.id)
        assert detail is not None
        runs = PipelineRunRepository(session)
        for seat_row in detail.seats:
            run, _created = await runs.enqueue(
                kind=REVIEW_SEAT_KIND,
                idempotency_key=f"review_seat:{round_row.id}:{seat_row.seat_id}",
                triggered_by=f"review_panel:{trigger.value}",
                listing_id=listing_id,
                request_json={
                    "round_id": round_row.id,
                    "seat_run_id": seat_row.id,
                },
            )
            seat_row.pipeline_run_id = run.id
        await session.flush()
        # Re-check inside the write transaction: a concurrent creator that
        # committed between the first check and this insert must not be able
        # to push the day over its cap.
        await _check_quota(
            repository,
            settings,
            day_start=day_start,
            seat_count=seat_count,
            inclusive=True,
        )
    return round_row


async def _check_quota(
    repository: ReviewPanelRepository,
    settings: Settings,
    *,
    day_start: datetime,
    seat_count: int,
    inclusive: bool,
) -> None:
    """Reject when today's rounds or reserved tokens would exceed the cap.

    ``inclusive`` is used after this round's row exists, so the comparison is
    ``>`` (the new row is already counted) rather than ``>=``.
    """

    daily_rounds = settings.review_panel_daily_rounds
    if daily_rounds > 0:
        used_rounds = await repository.count_rounds_since(day_start)
        exceeded = used_rounds > daily_rounds if inclusive else used_rounds >= daily_rounds
        if exceeded:
            raise RoundRejected(
                "daily_rounds_exhausted", "今日评审轮次额度已用完，请明日再试"
            )
    used_tokens = await repository.sum_tokens_since(day_start)
    reserved = seat_count * settings.review_panel_seat_tokens
    limit = settings.review_panel_daily_tokens
    exceeded_tokens = used_tokens > limit if inclusive else used_tokens + reserved > limit
    if exceeded_tokens:
        raise RoundRejected(
            "daily_tokens_exhausted", "今日评审 token 额度不足，请明日再试"
        )


async def finalize_round(session: AsyncSession, round_id: int) -> bool:
    """Finalize one round once every seat is terminal.

    Elects a single writer with an atomic status transition, computes the
    deterministic comparison (previous = the latest earlier completed round of
    the same listing), settles seat usage and saves the comparison in the same
    transaction. Returns whether this call finalized the round.
    """

    repository = ReviewPanelRepository(session)
    detail = await repository.get_round(round_id)
    if detail is None:
        return False
    if detail.round.status not in (RoundStatus.QUEUED.value, RoundStatus.RUNNING.value):
        return False
    if any(seat.status not in _TERMINAL_SEAT_VALUES for seat in detail.seats):
        return False

    seats = [_seat_input(seat) for seat in detail.seats]
    previous_round = await repository.previous_completed_round(
        detail.round.listing_id, round_id
    )
    previous = None
    if previous_round is not None:
        previous_detail = await repository.get_round(previous_round.id)
        if previous_detail is not None:
            previous = [_seat_input(seat) for seat in previous_detail.seats]

    comparison = compare_round(seats, previous)
    produced_opinion = any(seat.opinion is not None for seat in seats)
    status = RoundStatus.COMPLETED if produced_opinion else RoundStatus.FAILED
    usage = _settled_usage(detail.seats, detail.round)
    if not await repository.complete_round_if_active(
        round_id, status, settled_usage=usage
    ):
        return False
    await repository.save_comparison(
        round_id,
        ALGORITHM_VERSION,
        comparison.model_dump(mode="json"),
    )
    # Design §10.5: one best-effort in-app notice for noteworthy rounds. It
    # runs after the comparison is stored and never changes the finalisation
    # result, even if the notification write itself fails.
    try:
        from investment_agent.review_panel.notifications import (
            notify_round_completed,
        )

        await notify_round_completed(session, detail, comparison)
    except Exception:  # noqa: BLE001 - a notice must never fail finalisation
        import structlog

        structlog.get_logger(__name__).exception(
            "review_panel.notification_failed", round_id=round_id
        )
    return True


def _seat_input(seat: ReviewSeatRunRecord) -> SeatInput:
    opinion = (
        SeatOpinion.model_validate(seat.opinion_json)
        if seat.opinion_json is not None
        else None
    )
    return SeatInput(
        seat_id=seat.seat_id,
        lens_id=seat.lens_id,
        model_ref=seat.model_ref,
        opinion=opinion,
        evidence_refs=frozenset(seat.evidence_refs or ()),
    )


def _settled_usage(
    seats: Sequence[ReviewSeatRunRecord], round_row: ReviewRoundRecord
) -> dict[str, object]:
    totals = {
        "requests": 0,
        "tool_calls": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }
    for seat in seats:
        usage = seat.usage if isinstance(seat.usage, dict) else {}
        for key in totals:
            value = usage.get(key, 0)
            if isinstance(value, int) and not isinstance(value, bool):
                totals[key] += value
    return {
        **totals,
        "reserved_tokens": round_row.reserved_tokens,
        "reserved_requests": round_row.reserved_requests,
    }


_TERMINAL_SEAT_VALUES = frozenset(
    status.value for status in (SeatStatus.SUCCEEDED, SeatStatus.PARTIAL, SeatStatus.FAILED)
)


__all__ = [
    "BASE_AGENT_VIEW_REF",
    "BASE_CHANGES_REF",
    "BASE_PACK_REFS",
    "BASE_PACK_SCHEMA_VERSION",
    "BASE_SCORE_REF",
    "BASE_SNAPSHOT_REF",
    "REVIEW_SEAT_KIND",
    "RoundRejected",
    "base_pack_hash",
    "build_base_pack",
    "canonical_json",
    "create_round",
    "finalize_round",
    "model_snapshot",
]
