"""Durable daily-forward simulation application service. Caller owns commit.

Every mutation obtains SQLite's writer reservation before reading; account heads
also use revision CAS. Model/network work belongs outside these transactions.
Storage payloads are exact contracts; only round ``_service`` holds recovery pins.
"""

from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from decimal import ROUND_HALF_UP, Decimal, localcontext
from pathlib import Path
from typing import Any, Literal, TypeVar, cast

from pydantic import BaseModel
from sqlalchemy import func, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.clock import SHANGHAI_TZ
from investment_agent.domain.time import as_utc
from investment_agent.investor.common import utc_timestamp
from investment_agent.scheduler.trading_calendar import TradingCalendar
from investment_agent.simulation.artifacts import ArtifactStore, safe_export
from investment_agent.simulation.baselines import baseline_decision
from investment_agent.simulation.context import (
    FrozenFacts,
    PrivateContext,
    build_common_manifest,
    own_account_context,
    verify_frozen_facts,
)
from investment_agent.simulation.contracts import (
    AccountScope,
    AccountState,
    Decision,
    Experiment,
    ExperimentConfig,
    LedgerEvent,
    NavSnapshot,
    Participant,
    SimulationAccount,
    SimulationFill,
    SimulationOrder,
    SimulationReview,
    SimulationRound,
)
from investment_agent.simulation.data import (
    ExecutionEvidenceInput,
    content_hash,
    execution_eligibility,
    import_execution_evidence,
    next_trading_day,
    validate_listing_scope,
)
from investment_agent.simulation.errors import SimulationError
from investment_agent.simulation.execution import ExecutionBar, match_daily_open
from investment_agent.simulation.ledger import CorporateAction, apply_corporate_action, settle_order
from investment_agent.simulation.metrics import ValuationMark, value_account
from investment_agent.simulation.reviews import review_decision
from investment_agent.simulation.risk import (
    ExecutionTerms,
    ReservationTerms,
    reserve_decision,
    stable_id,
    validated_update,
)
from investment_agent.storage.models.pit_snapshot import MarketSnapshot
from investment_agent.storage.models.simulation import SimulationAccountRecord as AccountRow
from investment_agent.storage.models.simulation import SimulationDecisionRecord as DecisionRow
from investment_agent.storage.models.simulation import SimulationExperimentRecord as ExperimentRow
from investment_agent.storage.models.simulation import SimulationFillRecord as FillRow
from investment_agent.storage.models.simulation import SimulationLedgerEventRecord as EventRow
from investment_agent.storage.models.simulation import SimulationNavRecord as NavRow
from investment_agent.storage.models.simulation import SimulationOrderRecord as OrderRow
from investment_agent.storage.models.simulation import SimulationParticipantRecord as ParticipantRow
from investment_agent.storage.models.simulation import SimulationRequestRecord as RequestRow
from investment_agent.storage.models.simulation import SimulationReviewRecord as ReviewRow
from investment_agent.storage.models.simulation import SimulationRoundRecord as RoundRow

T = TypeVar("T", bound=BaseModel)
Scoped = TypeVar("Scoped", bound=AccountScope)

SCOPE = ("experiment_id", "participant_id", "account_id")
OBSERVATION_HISTORY_DAYS = 60


def _calendar_payload(calendar: TradingCalendar) -> dict[str, Any]:
    payload = calendar.model_dump(mode="json")
    payload["years"] = sorted(payload["years"], key=lambda year: year["year"])
    for year in payload["years"]:
        for key in ("closed_dates", "open_dates", "verified_by"):
            year[key] = sorted(year[key])
    return payload


class SimulationService:
    def __init__(
        self,
        session: AsyncSession,
        *,
        data_dir: Path,
        calendar: TradingCalendar,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self.session = session
        self.artifacts = ArtifactStore(data_dir)
        self.calendar = TradingCalendar.model_validate(calendar.model_dump())
        self._clock = now or (lambda: datetime.now(UTC))
        self._calendars: dict[str, TradingCalendar] = {}

    def now(self) -> datetime:
        return utc_timestamp(self._clock())

    async def _reserve(self, experiment_id: str | None = None) -> None:
        statement = update(ExperimentRow).values(revision=ExperimentRow.revision)
        if experiment_id is not None:
            statement = statement.where(ExperimentRow.id == experiment_id)
        await self.session.execute(statement.execution_options(synchronize_session=False))

    @staticmethod
    def _decode(row: Any, contract: type[T], fields: tuple[str, ...]) -> T:
        raw = dict(row.payload)
        raw.pop("_service", None)
        value = contract.model_validate(raw)
        for name in fields:
            stored = (
                row.id
                if name == "account_id" and isinstance(row, AccountRow)
                else getattr(row, name)
            )
            expected = getattr(value, name)
            if isinstance(stored, datetime):
                stored = as_utc(stored)
            if stored != expected:
                raise SimulationError(
                    "reconciliation_failed",
                    f"stored {contract.__name__} {name} differs from payload",
                )
        return value

    async def _experiment(self, experiment_id: str) -> tuple[ExperimentRow, Experiment]:
        row = await self.session.get(ExperimentRow, experiment_id, populate_existing=True)
        if row is None:
            raise SimulationError("not_found", "simulation experiment not found")
        value = self._decode(row, Experiment, ("id", "revision", "status", "config_hash"))
        if row.config != value.config.model_dump(mode="json") or value.config_hash != content_hash(
            row.config
        ):
            raise SimulationError(
                "reconciliation_failed", "experiment frozen configuration mismatch"
            )
        calendar = row.payload.get("_service", {}).get("calendar")
        calendar_hash = row.payload.get("_service", {}).get("calendar_hash")
        if calendar is None or content_hash(calendar) != calendar_hash:
            raise SimulationError("reconciliation_failed", "frozen calendar artifact hash mismatch")
        pinned = TradingCalendar.model_validate(calendar)
        if (
            pinned.calendar_id != value.config.rules.calendar_version
            or pinned.timezone != "Asia/Shanghai"
        ):
            raise SimulationError("unsupported_rule", "frozen calendar differs from rule package")
        self._calendars[value.id] = pinned
        return row, value

    def _calendar_for(self, experiment: Experiment) -> TradingCalendar:
        return self._calendars[experiment.id]

    def _check_current_calendar(self, experiment: Experiment) -> None:
        """Calendar maintenance outside this season cannot invalidate history.

        Historical projections always use the archived calendar. Changed sessions
        in the frozen season stop new progression, while reads remain available.
        """
        frozen = self._calendar_for(experiment)
        day = experiment.config.start_date
        while day <= experiment.config.end_date:
            if self.calendar.is_trading_day(day) != frozen.is_trading_day(day):
                raise SimulationError(
                    "unsupported_rule",
                    "current trading sessions differ from frozen season calendar",
                )
            day += timedelta(days=1)

    async def _participant(
        self, experiment_id: str, participant_id: str
    ) -> tuple[ParticipantRow, Participant, AccountRow, SimulationAccount]:
        _, experiment = await self._experiment(experiment_id)
        row = await self.session.get(ParticipantRow, participant_id, populate_existing=True)
        if row is None or row.experiment_id != experiment_id:
            raise SimulationError("not_found", "simulation participant not found")
        participant = self._decode(
            row, Participant, ("id", "experiment_id", "account_id", "version", "config_hash")
        )
        if (
            row.config != participant.config.model_dump(mode="json")
            or participant.config_hash != content_hash(row.config)
            or row.name != participant.config.name
            or row.kind != participant.config.kind
        ):
            raise SimulationError("reconciliation_failed", "participant configuration mismatch")
        if participant.config not in experiment.config.participants:
            raise SimulationError(
                "reconciliation_failed", "participant differs from frozen experiment"
            )
        account_row = await self.session.get(
            AccountRow, participant.account_id, populate_existing=True
        )
        if account_row is None:
            raise SimulationError("reconciliation_failed", "participant account missing")
        account = self._decode(account_row, SimulationAccount, (*SCOPE, "revision"))
        if (
            account.experiment_id != experiment_id
            or account.participant_id != participant_id
            or account.account_id != participant.account_id
            or account_row.id != account.account_id
        ):
            raise SimulationError("scope_mismatch", "participant/account scope mismatch")
        head = await self.session.scalar(
            select(EventRow).where(
                EventRow.account_id == account.account_id,
                EventRow.account_revision == account.revision,
            )
        )
        if head is None:
            raise SimulationError("reconciliation_failed", "account head lacks ledger event")
        event = self._decode(
            head,
            LedgerEvent,
            (
                "id",
                *SCOPE,
                "account_revision",
                "sequence",
                "kind",
                "economic_at",
                "confirmed_at",
                "idempotency_key",
            ),
        )
        if (
            any(getattr(event, key) != getattr(account, key) for key in SCOPE)
            or event.state_after != account.state
            or account.initial_cash != experiment.config.initial_cash
        ):
            raise SimulationError(
                "reconciliation_failed", "account head differs from append-only ledger"
            )
        return row, participant, account_row, account

    async def _round(
        self, experiment_id: str, round_id: str
    ) -> tuple[RoundRow, SimulationRound, dict[str, Any]]:
        _, experiment = await self._experiment(experiment_id)
        row = await self.session.get(RoundRow, round_id, populate_existing=True)
        if row is None or row.experiment_id != experiment_id:
            raise SimulationError("not_found", "simulation round not found")
        value = self._decode(
            row,
            SimulationRound,
            (
                "id",
                "experiment_id",
                "revision",
                "status",
                "observation_date",
                "execution_date",
                "cutoff",
                "decision_deadline",
                "execution_at",
            ),
        )
        if value.manifest.content_hash != row.manifest_hash:
            raise SimulationError("reconciliation_failed", "round manifest mismatch")
        metadata = dict(row.payload.get("_service", {}))
        frozen = FrozenFacts(
            manifest=value.manifest,
            payload_json=self.artifacts.read(
                value.manifest.artifact_path, expected_hash=value.manifest.content_hash
            ),
        )
        verify_frozen_facts(frozen)
        if frozen.payload().get("observation_date") != str(value.observation_date):
            raise SimulationError(
                "reconciliation_failed", "manifest observation date differs from round"
            )
        if value.manifest.universe != tuple(sorted(experiment.config.universe)):
            raise SimulationError("scope_mismatch", "round universe differs from frozen experiment")
        if metadata.get("position_prices") != {
            str(key): str(price)
            for key, price in self._observation_prices(frozen, value.observation_date).items()
        }:
            raise SimulationError(
                "reconciliation_failed", "reservation prices differ from frozen facts"
            )
        if set(metadata.get("reservation_terms", {})) != set(
            metadata.get("reservation_proofs", {})
        ):
            raise SimulationError(
                "reconciliation_failed", "reservation proof/term identities differ"
            )
        for listing, identity in metadata.get("reservation_proofs", {}).items():
            eligibility = await execution_eligibility(
                self.session,
                experiment_id=experiment_id,
                listing_id=int(listing),
                trade_date=value.observation_date,
                cutoff=value.cutoff,
                evidence_id=identity,
            )
            terms = ReservationTerms.model_validate(metadata["reservation_terms"][listing])
            proof = eligibility.evidence
            if (
                not eligibility.ready
                or proof is None
                or int(listing) not in value.manifest.universe
            ):
                raise SimulationError(
                    "reconciliation_failed", "frozen reservation proof is unavailable"
                )
            turnover = self._observation_turnover(frozen, int(listing), value.observation_date)
            expected_terms = ReservationTerms(
                listing_id=int(listing),
                trade_date=value.execution_date,
                rule_version=proof.rule_version,
                source_ref=identity,
                verified=True,
                tick_size=proof.tick_size,
                lot_size=proof.lot_size,
                known_at=max(proof.captured_at, proof.published_at),
                historical_turnover=turnover,
                historical_available_at=value.cutoff if turnover is not None else None,
                corporate_actions_complete=proof.corporate_actions_complete,
                unsupported_corporate_actions=proof.unsupported_corporate_actions,
            )
            if terms != expected_terms or terms.rule_version != experiment.config.rules.version:
                raise SimulationError(
                    "reconciliation_failed", "frozen reservation proof differs from terms"
                )
        if value.manifest.complete and set(metadata.get("reservation_terms", {})) != {
            str(i) for i in value.manifest.universe
        }:
            raise SimulationError("reconciliation_failed", "complete round lacks reservation proof")
        for listing, pin in metadata.get("execution_pins", {}).items():
            eligibility = await execution_eligibility(
                self.session,
                experiment_id=experiment_id,
                listing_id=int(listing),
                trade_date=value.execution_date,
                cutoff=self.now(),
                evidence_id=pin["evidence_id"],
            )
            market = await self._market(
                int(listing), value.execution_date, cutoff=self.now(), identity=pin["market_id"]
            )
            if (
                market is None
                or eligibility.evidence_hash != pin["evidence_hash"]
                or content_hash(self._market_payload(market)) != pin["market_hash"]
            ):
                raise SimulationError("reconciliation_failed", "pinned execution evidence changed")
        return row, value, metadata

    def _save_round(self, row: RoundRow, value: SimulationRound, metadata: dict[str, Any]) -> None:
        for key in (
            "revision",
            "status",
            "observation_date",
            "execution_date",
            "cutoff",
            "decision_deadline",
            "execution_at",
        ):
            setattr(row, key, getattr(value, key))
        row.manifest_hash = value.manifest.content_hash
        row.payload = {**value.model_dump(mode="json"), "_service": metadata}

    async def _replay(
        self,
        scope: str,
        key: str,
        body: object,
        contract: type[T],
        *,
        expected: dict[str, object] | None = None,
    ) -> T | None:
        row = await self.session.get(RequestRow, (scope, key), populate_existing=True)
        if row is None:
            return None
        if row.input_hash != content_hash(body):
            raise SimulationError(
                "idempotency_conflict", "idempotency key reused with another request"
            )
        result = contract.model_validate(row.payload)
        if row.result_id != row.payload.get("id", "result") or any(
            getattr(result, field) != value for field, value in (expected or {}).items()
        ):
            raise SimulationError(
                "scope_mismatch", "idempotent result differs from bound request scope"
            )
        return result

    def _remember(
        self, scope: str, key: str, body: object, value: BaseModel | dict[str, Any]
    ) -> None:
        payload = value.model_dump(mode="json") if isinstance(value, BaseModel) else value
        self.session.add(
            RequestRow(
                scope=scope,
                idempotency_key=key,
                input_hash=content_hash(body),
                result_id=payload.get("id", "result"),
                payload=payload,
            )
        )

    async def _account_cas(
        self, row: AccountRow, before: SimulationAccount, after: SimulationAccount
    ) -> None:
        result = await self.session.execute(
            update(AccountRow)
            .where(
                AccountRow.id == before.account_id,
                AccountRow.experiment_id == before.experiment_id,
                AccountRow.participant_id == before.participant_id,
                AccountRow.revision == before.revision,
            )
            .values(revision=after.revision, payload=after.model_dump(mode="json"))
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise SimulationError("revision_conflict", "simulation account changed")
        self.session.expire(row)

    async def _sequence(self, account_id: str) -> int:
        return (
            await self.session.scalar(
                select(func.max(EventRow.sequence)).where(EventRow.account_id == account_id)
            )
            or 0
        ) + 1

    def _event(self, value: LedgerEvent, metadata: dict[str, Any] | None = None) -> None:
        self.session.add(
            EventRow(
                id=value.id,
                **{
                    key: getattr(value, key)
                    for key in (
                        *SCOPE,
                        "sequence",
                        "account_revision",
                        "idempotency_key",
                        "kind",
                        "economic_at",
                        "confirmed_at",
                    )
                },
                payload={
                    **value.model_dump(mode="json"),
                    **({"_service": metadata} if metadata else {}),
                },
            )
        )

    async def create(self, config: ExperimentConfig, *, idempotency_key: str) -> Experiment:
        config = ExperimentConfig.model_validate(config.model_dump())
        await self._reserve()
        body = config.model_dump(mode="json")
        old = await self._replay(
            "create",
            idempotency_key,
            body,
            Experiment,
            expected={"id": stable_id("experiment", idempotency_key), "config": config},
        )
        if old:
            return old
        if (
            self.calendar.timezone != "Asia/Shanghai"
            or self.calendar.calendar_id != config.rules.calendar_version
        ):
            raise SimulationError(
                "unsupported_rule", "reviewed calendar differs from frozen rule package"
            )
        self.calendar.is_trading_day(config.start_date)
        self.calendar.is_trading_day(config.end_date)
        await validate_listing_scope(self.session, config.universe)
        now = self.now()
        value = Experiment(
            id=stable_id("experiment", idempotency_key),
            config=config,
            config_hash=content_hash(body),
            created_at=now,
        )
        self.session.add(
            ExperimentRow(
                id=value.id,
                revision=0,
                status=value.status,
                config_hash=value.config_hash,
                config=body,
                payload={
                    **value.model_dump(mode="json"),
                    "_service": {
                        "calendar": _calendar_payload(self.calendar),
                        "calendar_hash": content_hash(_calendar_payload(self.calendar)),
                    },
                },
            )
        )
        for index, participant_config in enumerate(config.participants):
            pid = stable_id("participant", value.id, index)
            aid = stable_id("account", pid)
            participant = Participant(
                id=pid,
                experiment_id=value.id,
                account_id=aid,
                config=participant_config,
                config_hash=content_hash(participant_config.model_dump(mode="json")),
            )
            self.session.add(
                ParticipantRow(
                    id=pid,
                    experiment_id=value.id,
                    account_id=aid,
                    name=participant_config.name,
                    kind=participant_config.kind,
                    version=1,
                    config_hash=participant.config_hash,
                    config=participant_config.model_dump(mode="json"),
                    payload=participant.model_dump(mode="json"),
                )
            )
            account = SimulationAccount(
                experiment_id=value.id,
                participant_id=pid,
                account_id=aid,
                revision=1,
                initial_cash=config.initial_cash,
                state=AccountState(cash=config.initial_cash),
            )
            self.session.add(
                AccountRow(
                    id=aid,
                    experiment_id=value.id,
                    participant_id=pid,
                    revision=1,
                    payload=account.model_dump(mode="json"),
                )
            )
            self._event(
                LedgerEvent(
                    **{key: getattr(account, key) for key in SCOPE},
                    id=stable_id("event", aid, "initial"),
                    sequence=1,
                    account_revision=1,
                    idempotency_key=stable_id("initial", aid),
                    kind="initial_cash",
                    economic_at=now,
                    confirmed_at=now,
                    cash_delta=config.initial_cash,
                    source_ref=value.config_hash,
                    state_after=account.state,
                )
            )
        self._remember("create", idempotency_key, body, value)
        await self.session.flush()
        return value

    async def list(self) -> tuple[Experiment, ...]:
        ids = (
            await self.session.scalars(
                select(ExperimentRow.id).order_by(ExperimentRow.created_at, ExperimentRow.id)
            )
        ).all()
        return tuple([(await self._experiment(identity))[1] for identity in ids])

    async def get(self, experiment_id: str) -> Experiment:
        return (await self._experiment(experiment_id))[1]

    async def participants(self, experiment_id: str) -> tuple[Participant, ...]:
        await self._experiment(experiment_id)
        ids = (
            await self.session.scalars(
                select(ParticipantRow.id)
                .where(ParticipantRow.experiment_id == experiment_id)
                .order_by(ParticipantRow.id)
            )
        ).all()
        return tuple([(await self._participant(experiment_id, identity))[1] for identity in ids])

    async def account(self, experiment_id: str, participant_id: str) -> SimulationAccount:
        return (await self._participant(experiment_id, participant_id))[3]

    async def rounds(self, experiment_id: str) -> tuple[SimulationRound, ...]:
        await self._experiment(experiment_id)
        ids = (
            await self.session.scalars(
                select(RoundRow.id)
                .where(RoundRow.experiment_id == experiment_id)
                .order_by(RoundRow.observation_date)
            )
        ).all()
        return tuple([(await self._round(experiment_id, identity))[1] for identity in ids])

    async def _records(
        self,
        model: Any,
        contract: type[Scoped],
        experiment_id: str,
        participant_id: str | None,
        fields: tuple[str, ...],
    ) -> tuple[Scoped, ...]:
        await self._experiment(experiment_id)
        statement = select(model).where(model.experiment_id == experiment_id)
        if participant_id is not None:
            participant = (await self._participant(experiment_id, participant_id))[1]
            statement = statement.where(model.participant_id == participant_id)
        rows = (await self.session.scalars(statement.order_by(model.id))).all()
        values = []
        for row in rows:
            value = self._decode(row, contract, ("id", *SCOPE, *fields))
            bound = (
                participant
                if participant_id is not None
                else (await self._participant(experiment_id, value.participant_id))[1]
            )
            if value.account_id != bound.account_id:
                raise SimulationError(
                    "scope_mismatch", "stored record account differs from participant"
                )
            if hasattr(value, "round_id"):
                await self._round(experiment_id, value.round_id)
            if isinstance(value, (SimulationOrder, SimulationReview)):
                related_row = await self.session.get(
                    DecisionRow, value.decision_id, populate_existing=True
                )
                if related_row is None:
                    raise SimulationError("reconciliation_failed", "record decision missing")
                related = self._decode(related_row, Decision, ("id", *SCOPE, "round_id", "status"))
                if any(getattr(related, key) != getattr(value, key) for key in SCOPE) or (
                    isinstance(value, SimulationOrder) and related.round_id != value.round_id
                ):
                    raise SimulationError(
                        "scope_mismatch", "record decision belongs to another scope"
                    )
            if isinstance(value, SimulationFill):
                order_row = await self.session.get(OrderRow, value.order_id, populate_existing=True)
                if order_row is None:
                    raise SimulationError("reconciliation_failed", "fill order missing")
                locked_order = self._decode(
                    order_row, SimulationOrder, ("id", *SCOPE, "round_id", "status", "listing_id")
                )
                if any(
                    getattr(locked_order, key) != getattr(value, key)
                    for key in (
                        *SCOPE,
                        "round_id",
                        "listing_id",
                        "side",
                        "quantity",
                        "rule_version",
                    )
                ):
                    raise SimulationError("scope_mismatch", "fill order belongs to another scope")
            if isinstance(value, LedgerEvent) and value.kind == "fill":
                fill_row = await self.session.get(FillRow, value.fill_id, populate_existing=True)
                if fill_row is None:
                    raise SimulationError("reconciliation_failed", "ledger fill reference missing")
                ledger_fill = self._decode(
                    fill_row,
                    SimulationFill,
                    (
                        "id",
                        *SCOPE,
                        "round_id",
                        "order_id",
                        "execution_key",
                        "sequence",
                        "economic_at",
                        "confirmed_at",
                    ),
                )
                with localcontext() as context:
                    context.prec = 100
                    expected_quantity = (
                        ledger_fill.quantity if ledger_fill.side == "buy" else -ledger_fill.quantity
                    )
                    expected_cash = (
                        -ledger_fill.quantity * ledger_fill.price
                        if ledger_fill.side == "buy"
                        else ledger_fill.quantity * ledger_fill.price
                    ) - ledger_fill.fee
                if (
                    any(getattr(ledger_fill, key) != getattr(value, key) for key in SCOPE)
                    or value.quantity_delta != expected_quantity
                    or value.cash_delta != expected_cash
                    or value.listing_id != ledger_fill.listing_id
                ):
                    raise SimulationError(
                        "reconciliation_failed", "ledger event differs from its scoped fill"
                    )
            values.append(value)
        return tuple(values)

    async def decisions(
        self, experiment_id: str, participant_id: str | None = None
    ) -> tuple[Decision, ...]:
        return await self._records(
            DecisionRow,
            Decision,
            experiment_id,
            participant_id,
            ("round_id", "revision", "account_revision", "status", "idempotency_key"),
        )

    async def orders(
        self, experiment_id: str, participant_id: str | None = None
    ) -> tuple[SimulationOrder, ...]:
        return await self._records(
            OrderRow,
            SimulationOrder,
            experiment_id,
            participant_id,
            (
                "round_id",
                "decision_id",
                "listing_id",
                "sequence",
                "account_revision",
                "execution_date",
                "status",
                "idempotency_key",
            ),
        )

    async def fills(
        self, experiment_id: str, participant_id: str | None = None
    ) -> tuple[SimulationFill, ...]:
        return await self._records(
            FillRow,
            SimulationFill,
            experiment_id,
            participant_id,
            ("round_id", "order_id", "execution_key", "sequence", "economic_at", "confirmed_at"),
        )

    async def nav(
        self, experiment_id: str, participant_id: str | None = None
    ) -> tuple[NavSnapshot, ...]:
        return await self._records(
            NavRow,
            NavSnapshot,
            experiment_id,
            participant_id,
            (
                "account_revision",
                "valuation_date",
                "version",
                "quality",
                "input_hash",
                "valuation_version",
                "metrics_version",
            ),
        )

    async def ledger(self, experiment_id: str, participant_id: str) -> tuple[LedgerEvent, ...]:
        return await self._records(
            EventRow,
            LedgerEvent,
            experiment_id,
            participant_id,
            (
                "sequence",
                "account_revision",
                "idempotency_key",
                "kind",
                "economic_at",
                "confirmed_at",
            ),
        )

    async def reviews(
        self, experiment_id: str, participant_id: str | None = None
    ) -> tuple[SimulationReview, ...]:
        return await self._records(
            ReviewRow,
            SimulationReview,
            experiment_id,
            participant_id,
            ("decision_id", "version", "status", "available_at"),
        )

    async def detail(self, experiment_id: str) -> dict[str, Any]:
        experiment = (await self._experiment(experiment_id))[1]
        participants = await self.participants(experiment_id)
        return cast(
            dict[str, Any],
            safe_export(
                {
                    "experiment": experiment.model_dump(mode="json"),
                    "participants": [p.model_dump(mode="json") for p in participants],
                    "accounts": [
                        (await self.account(experiment_id, p.id)).model_dump(mode="json")
                        for p in participants
                    ],
                    "rounds": [r.model_dump(mode="json") for r in await self.rounds(experiment_id)],
                    "data_gaps": {
                        r.id: (await self._round(experiment_id, r.id))[2].get(
                            "blocking_reasons", []
                        )
                        for r in await self.rounds(experiment_id)
                    },
                    "leaderboard": await self.leaderboard(experiment_id),
                }
            ),
        )

    async def _transition(
        self,
        experiment_id: str,
        target: Literal["running", "paused", "completed"],
        *,
        expected_revision: int,
        idempotency_key: str,
    ) -> Experiment:
        await self._reserve(experiment_id)
        body = {"expected_revision": expected_revision, "target": target}
        scope = f"experiment:{experiment_id}:{target}"
        previous = await self._replay(
            scope, idempotency_key, body, Experiment, expected={"id": experiment_id}
        )
        if previous:
            return previous
        row, value = await self._experiment(experiment_id)
        if value.revision != expected_revision:
            raise SimulationError("revision_conflict", "experiment revision changed")
        allowed = {
            "running": {"draft", "paused"},
            "paused": {"running", "blocked"},
            "completed": {"draft", "running", "paused", "blocked"},
        }
        if value.status not in allowed[target]:
            raise SimulationError("invalid_transition", "experiment cannot enter requested state")
        now = self.now()
        if target == "running":
            self._check_current_calendar(value)
        if target == "running" and now.astimezone(SHANGHAI_TZ).date() > value.config.end_date:
            raise SimulationError("deadline_passed", "experiment season has ended")
        if target in {"paused", "completed"}:
            await self._cancel_future(experiment_id, now)
        value = validated_update(
            value,
            status=target,
            revision=value.revision + 1,
            started_at=(value.started_at or now) if target == "running" else value.started_at,
            ended_at=now if target == "completed" else value.ended_at,
        )
        result = await self.session.execute(
            update(ExperimentRow)
            .where(ExperimentRow.id == experiment_id, ExperimentRow.revision == expected_revision)
            .values(
                status=value.status,
                revision=value.revision,
                payload={**value.model_dump(mode="json"), "_service": row.payload["_service"]},
            )
            .execution_options(synchronize_session=False)
        )
        if getattr(result, "rowcount", 0) != 1:
            raise SimulationError("revision_conflict", "experiment revision changed")
        self.session.expire(row)
        self._remember(scope, idempotency_key, body, value)
        await self.session.flush()
        return value

    async def start(
        self, experiment_id: str, *, expected_revision: int, idempotency_key: str
    ) -> Experiment:
        return await self._transition(
            experiment_id,
            "running",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    async def pause(
        self, experiment_id: str, *, expected_revision: int, idempotency_key: str
    ) -> Experiment:
        return await self._transition(
            experiment_id,
            "paused",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    async def end(
        self, experiment_id: str, *, expected_revision: int, idempotency_key: str
    ) -> Experiment:
        return await self._transition(
            experiment_id,
            "completed",
            expected_revision=expected_revision,
            idempotency_key=idempotency_key,
        )

    async def get_round(self, experiment_id: str, round_id: str) -> SimulationRound:
        return (await self._round(experiment_id, round_id))[1]

    async def round_data_gaps(self, experiment_id: str, round_id: str) -> tuple[str, ...]:
        """Public reasons only; private recovery metadata never crosses the API."""
        _, _, metadata = await self._round(experiment_id, round_id)
        reasons = metadata.get("blocking_reasons", [])
        if not isinstance(reasons, list) or any(not isinstance(reason, str) for reason in reasons):
            raise SimulationError("reconciliation_failed", "invalid round blocking reasons")
        return tuple(reasons)

    async def common_facts(self, experiment_id: str, round_id: str) -> FrozenFacts:
        round_ = (await self._round(experiment_id, round_id))[1]
        serialized = self.artifacts.read(
            round_.manifest.artifact_path, expected_hash=round_.manifest.content_hash
        )
        facts = FrozenFacts(manifest=round_.manifest, payload_json=serialized)
        verify_frozen_facts(facts)
        return facts

    async def prepare_round(
        self,
        experiment_id: str,
        *,
        observation_date: date,
        cutoff: datetime,
        decision_deadline: datetime,
        idempotency_key: str,
        require_financials: bool = True,
    ) -> SimulationRound:
        await self._reserve(experiment_id)
        cutoff, decision_deadline = utc_timestamp(cutoff), utc_timestamp(decision_deadline)
        body = dict(
            observation_date=str(observation_date),
            cutoff=cutoff.isoformat(),
            decision_deadline=decision_deadline.isoformat(),
            require_financials=require_financials,
        )
        previous = await self._replay(
            f"prepare:{experiment_id}",
            idempotency_key,
            body,
            SimulationRound,
            expected={"experiment_id": experiment_id},
        )
        if previous:
            return previous
        _, experiment = await self._experiment(experiment_id)
        if experiment.status != "running":
            raise SimulationError(
                "invalid_transition", "only running experiments prepare decisions"
            )
        now = self.now()
        if cutoff > now or cutoff < experiment.created_at or decision_deadline <= now:
            raise SimulationError(
                "deadline_passed",
                "forward round requires a known cutoff and future submission window",
            )
        if not experiment.config.start_date <= observation_date <= experiment.config.end_date:
            raise SimulationError("invalid_config", "observation outside experiment season")
        self._check_current_calendar(experiment)
        calendar = self._calendar_for(experiment)
        if not calendar.is_trading_day(observation_date):
            raise SimulationError("invalid_config", "observation day is not a reviewed trading day")
        execution_date = next_trading_day(calendar, observation_date)
        execution_at = datetime.combine(execution_date, time(9, 30), SHANGHAI_TZ)
        if decision_deadline >= execution_at:
            raise SimulationError(
                "deadline_passed", "decision deadline must precede execution open"
            )
        existing = await self.session.scalar(
            select(RoundRow).where(
                RoundRow.experiment_id == experiment_id,
                RoundRow.observation_date == observation_date,
            )
        )
        if existing is not None:
            _, old_round, old_meta = await self._round(experiment_id, existing.id)
            if old_round.status not in {"waiting_data", "blocked"} or old_meta.get(
                "decisions_opened"
            ):
                if old_round.cutoff != cutoff or old_round.decision_deadline != decision_deadline:
                    raise SimulationError(
                        "idempotency_conflict", "official observation round already frozen"
                    )
                self._remember(f"prepare:{experiment_id}", idempotency_key, body, old_round)
                return old_round
        outstanding = [
            order
            for order in await self.orders(experiment_id)
            if order.status == "locked" and order.execution_date <= observation_date
        ]
        if outstanding:
            raise SimulationError(
                "settlement_blocked",
                "settle earlier economic orders before freezing next context",
                retryable=True,
            )
        for participant in await self.participants(experiment_id):
            if any(
                event.confirmed_at > cutoff
                for event in await self.ledger(experiment_id, participant.id)
            ):
                raise SimulationError(
                    "future_evidence", "account facts were confirmed after requested cutoff"
                )
        facts = await build_common_manifest(
            self.session,
            listing_ids=experiment.config.universe,
            observation_date=observation_date,
            history_start=observation_date - timedelta(days=OBSERVATION_HISTORY_DAYS),
            cutoff=cutoff,
            archive=self.artifacts.archive,
            require_financials=require_financials,
        )
        missing = list(facts.manifest.missing_reasons)
        terms: dict[int, ReservationTerms] = {}
        proof_pins = {}
        prices = self._observation_prices(facts, observation_date)
        for listing_id in experiment.config.universe:
            eligible = await execution_eligibility(
                self.session,
                experiment_id=experiment_id,
                listing_id=listing_id,
                trade_date=observation_date,
                cutoff=cutoff,
            )
            if not eligible.ready or eligible.evidence is None or eligible.evidence_id is None:
                missing.extend(f"reservation:{listing_id}:{reason}" for reason in eligible.missing)
                continue
            proof = eligible.evidence
            if proof.rule_version != experiment.config.rules.version:
                missing.append(f"reservation_rule_mismatch:{listing_id}")
                continue
            proof_pins[str(listing_id)] = eligible.evidence_id
            turnover = self._observation_turnover(facts, listing_id, observation_date)
            if turnover is None:
                missing.append(f"historical_turnover_missing:{listing_id}")
            terms[listing_id] = ReservationTerms(
                listing_id=listing_id,
                trade_date=execution_date,
                rule_version=proof.rule_version,
                source_ref=eligible.evidence_id,
                verified=True,
                tick_size=proof.tick_size,
                lot_size=proof.lot_size,
                known_at=max(proof.captured_at, proof.published_at),
                historical_turnover=turnover,
                historical_available_at=cutoff if turnover is not None else None,
                corporate_actions_complete=proof.corporate_actions_complete,
                unsupported_corporate_actions=proof.unsupported_corporate_actions,
            )
        if missing != list(facts.manifest.missing_reasons):
            payload = facts.payload()
            payload["missing_reasons"] = sorted(set(missing))
            path, digest = self.artifacts.put(payload)
            manifest = validated_update(
                facts.manifest,
                complete=not missing,
                missing_reasons=tuple(sorted(set(missing))),
                content_hash=digest,
                artifact_path=path,
            )
            facts = FrozenFacts(
                manifest=manifest, payload_json=self.artifacts.read(path, expected_hash=digest)
            )
        status: Literal["collecting_decisions", "waiting_data", "skipped"] = (
            "collecting_decisions" if facts.manifest.complete else "waiting_data"
        )
        if facts.manifest.complete and experiment.config.rebalance_frequency == "weekly":
            previous_week_rounds = [
                r
                for r in await self.rounds(experiment_id)
                if r.observation_date < observation_date
                and r.observation_date.isocalendar()[:2] == observation_date.isocalendar()[:2]
                and (await self._round(experiment_id, r.id))[2].get("decisions_opened")
            ]
            if previous_week_rounds:
                status = "skipped"
        round_ = SimulationRound(
            id=stable_id("round", experiment_id, observation_date),
            experiment_id=experiment_id,
            observation_date=observation_date,
            cutoff=cutoff,
            decision_deadline=decision_deadline,
            execution_date=execution_date,
            execution_at=execution_at,
            status=status,
            manifest=facts.manifest,
            revision=old_round.revision + 1 if existing is not None else 0,
        )
        metadata = {
            "created_at": now.isoformat(),
            "reservation_terms": {str(k): v.model_dump(mode="json") for k, v in terms.items()},
            "reservation_proofs": proof_pins,
            "position_prices": {str(k): str(v) for k, v in prices.items()},
            "execution_pins": {},
            "blocking_reasons": list(facts.manifest.missing_reasons),
            "decisions_opened": status == "collecting_decisions",
        }
        row = existing or RoundRow(id=round_.id, experiment_id=experiment_id)
        self._save_round(row, round_, metadata)
        if existing is None:
            self.session.add(row)
        await self.session.flush()
        if facts.manifest.complete:
            await self.value(experiment_id, observation_date)
        if status == "collecting_decisions":
            for participant in await self.participants(experiment_id):
                if participant.config.kind == "agent":
                    continue
                account = await self.account(experiment_id, participant.id)
                already = any(
                    d.status == "locked" and d.action == "trade"
                    for d in await self.decisions(experiment_id, participant.id)
                )
                decision = baseline_decision(
                    participant,
                    account,
                    round_,
                    experiment.config.rules,
                    terms,
                    prices,
                    submitted_at=now,
                    already_attempted=already,
                )
                await self._store_decision(decision)
        self._remember(f"prepare:{experiment_id}", idempotency_key, body, round_)
        await self.session.flush()
        return round_

    @staticmethod
    def _observation_prices(facts: FrozenFacts, day: date) -> dict[int, Decimal]:
        return {
            int(item["data"]["listing_id"]): Decimal(item["data"]["close"])
            for item in cast(list[dict[str, Any]], facts.payload()["facts"])
            if item["ref"].startswith("market_snapshot:") and item["data"]["trade_date"] == str(day)
        }

    @staticmethod
    def _observation_turnover(facts: FrozenFacts, listing_id: int, day: date) -> Decimal | None:
        for item in cast(list[dict[str, Any]], facts.payload()["facts"]):
            data = item["data"]
            if (
                item["ref"].startswith("market_snapshot:")
                and data["listing_id"] == listing_id
                and data["trade_date"] == str(day)
            ):
                if data["amount"] is not None:
                    return Decimal(data["amount"])
                # A verified suspended, zero-volume observation proves zero
                # capacity. Unknown active turnover must remain missing.
                volume = Decimal(data["volume"])
                if data["trade_status"] == 0 and volume.is_finite() and volume == 0:
                    return Decimal(0)
                return None
        return None

    async def _store_decision(self, decision: Decision) -> None:
        self.session.add(
            DecisionRow(
                id=decision.id,
                **{
                    key: getattr(decision, key)
                    for key in (
                        *SCOPE,
                        "round_id",
                        "revision",
                        "account_revision",
                        "status",
                        "idempotency_key",
                    )
                },
                payload=decision.model_dump(mode="json"),
            )
        )
        await self.session.flush()

    async def submit_decision(
        self, experiment_id: str, round_id: str, participant_id: str, decision: Decision
    ) -> Decision:
        decision = Decision.model_validate(decision.model_dump())
        await self._reserve(experiment_id)
        body = decision.model_dump(mode="json")
        scope = f"submit:{experiment_id}:{participant_id}:{round_id}"
        previous = await self._replay(
            scope,
            decision.idempotency_key,
            body,
            Decision,
            expected={
                "experiment_id": experiment_id,
                "participant_id": participant_id,
                "round_id": round_id,
                "account_id": decision.account_id,
            },
        )
        if previous:
            return previous
        _, experiment = await self._experiment(experiment_id)
        _, participant, _, account = await self._participant(experiment_id, participant_id)
        _, round_, metadata = await self._round(experiment_id, round_id)
        if (
            any(getattr(decision, key) != getattr(account, key) for key in SCOPE)
            or decision.round_id != round_id
        ):
            raise SimulationError(
                "scope_mismatch", "decision identity differs from server-bound scope"
            )
        if participant.config.kind != "agent":
            raise SimulationError(
                "invalid_decision", "deterministic baseline decisions are server-managed"
            )
        failed_call = (
            decision.status in {"invalid", "timeout", "failed", "unknown"}
            and decision.action == "no_submission"
            and not decision.orders
        )
        if (
            not failed_call and decision.account_revision != account.revision
        ) or decision.account_revision > account.revision:
            raise SimulationError("revision_conflict", "decision account revision is stale")
        now = self.now()
        if not failed_call and (
            experiment.status != "running" or round_.status != "collecting_decisions"
        ):
            raise SimulationError("invalid_transition", "round does not accept decisions")
        if now < max(round_.cutoff, datetime.fromisoformat(metadata["created_at"])) or (
            not failed_call and now > round_.decision_deadline
        ):
            raise SimulationError(
                "deadline_passed", "decision is outside the actual forward submission window"
            )
        if decision.status not in {"submitted", "invalid", "timeout", "failed", "unknown"}:
            raise SimulationError(
                "invalid_decision", "decision is not a submission or explicit failure"
            )
        if decision.status != "submitted" and (
            decision.action != "no_submission" or decision.orders
        ):
            raise SimulationError(
                "invalid_decision", "failed invocation cannot submit executable orders or fake hold"
            )
        if any(
            ref not in {ref.id for ref in round_.manifest.evidence}
            for ref in decision.evidence_refs
        ):
            raise SimulationError(
                "future_evidence", "decision cites evidence outside frozen common data"
            )
        prior = [
            d for d in await self.decisions(experiment_id, participant_id) if d.round_id == round_id
        ]
        expected = max((d.revision for d in prior), default=-1) + 1
        if decision.revision != expected:
            raise SimulationError(
                "revision_conflict", "decision revision is not next in this round"
            )
        decision = validated_update(
            decision,
            id=stable_id("decision", account.account_id, round_id, decision.idempotency_key),
            submitted_at=now,
        )
        # Validate the whole set now, but reserve only the final revision at close.
        if decision.status == "submitted":
            reserve_decision(
                account,
                decision,
                round_,
                experiment.config.rules,
                {
                    int(k): ReservationTerms.model_validate(v)
                    for k, v in metadata["reservation_terms"].items()
                },
                position_prices={
                    int(k): Decimal(v) for k, v in metadata["position_prices"].items()
                },
            )
        await self._store_decision(decision)
        self._remember(scope, decision.idempotency_key, body, decision)
        await self.session.flush()
        return decision

    async def close_round(self, experiment_id: str, round_id: str) -> SimulationRound:
        await self._reserve(experiment_id)
        _, experiment = await self._experiment(experiment_id)
        row, round_, metadata = await self._round(experiment_id, round_id)
        if round_.status in {
            "locked",
            "awaiting_execution_data",
            "settled",
            "cancelled",
            "skipped",
        }:
            return round_
        now = self.now()
        if round_.status != "collecting_decisions":
            return round_
        self._check_current_calendar(experiment)
        if now < round_.decision_deadline:
            raise SimulationError("invalid_transition", "decision window has not closed")
        # Once the economic opening is reached, never create a new locked order.
        allow_orders = experiment.status == "running" and now < round_.execution_at
        for participant in await self.participants(experiment_id):
            _, _, account_row, account = await self._participant(experiment_id, participant.id)
            decisions = [
                d
                for d in await self.decisions(experiment_id, participant.id)
                if d.round_id == round_id
            ]
            decision = max(decisions, key=lambda d: d.revision) if decisions else None
            if decision is None or (not allow_orders and decision.status == "submitted"):
                revision = max((d.revision for d in decisions), default=-1) + 1
                missing = Decision(
                    **{key: getattr(account, key) for key in SCOPE},
                    id=stable_id("decision", account.account_id, round_id, "missed", revision),
                    round_id=round_id,
                    revision=revision,
                    account_revision=account.revision,
                    status="timeout",
                    action="no_submission",
                    rationale="No timely final decision was locked before the economic event.",
                    submitted_at=now,
                    failure_code="deadline_passed",
                    idempotency_key=stable_id("missing", participant.id, round_id, revision),
                )
                await self._store_decision(missing)
                continue
            if decision.status != "submitted":
                continue
            try:
                reserved = reserve_decision(
                    account,
                    decision,
                    round_,
                    experiment.config.rules,
                    {
                        int(k): ReservationTerms.model_validate(v)
                        for k, v in metadata["reservation_terms"].items()
                    },
                    position_prices={
                        int(k): Decimal(v) for k, v in metadata["position_prices"].items()
                    },
                )
            except SimulationError as exc:
                failed = validated_update(
                    decision,
                    status="invalid",
                    action="no_submission",
                    orders=(),
                    failure_code=exc.code,
                )
                decision_row = await self.session.get(DecisionRow, decision.id)
                assert decision_row is not None
                decision_row.status, decision_row.payload = (
                    failed.status,
                    failed.model_dump(mode="json"),
                )
                continue
            locked = validated_update(decision, status="locked")
            decision_row = await self.session.get(DecisionRow, decision.id)
            assert decision_row is not None
            decision_row.status, decision_row.payload = (
                locked.status,
                locked.model_dump(mode="json"),
            )
            for order in reserved.orders:
                self.session.add(
                    OrderRow(
                        id=order.id,
                        **{
                            key: getattr(order, key)
                            for key in (
                                *SCOPE,
                                "round_id",
                                "decision_id",
                                "listing_id",
                                "sequence",
                                "account_revision",
                                "execution_date",
                                "status",
                                "idempotency_key",
                            )
                        },
                        payload=order.model_dump(mode="json"),
                    )
                )
            if reserved.orders:
                self._event(
                    LedgerEvent(
                        **{key: getattr(account, key) for key in SCOPE},
                        id=stable_id("event", decision.id, "reserve"),
                        sequence=await self._sequence(account.account_id),
                        account_revision=reserved.account.revision,
                        idempotency_key=stable_id("reserve", decision.id),
                        kind="reserve",
                        economic_at=now,
                        confirmed_at=now,
                        source_ref=decision.id,
                        state_after=reserved.account.state,
                    )
                )
                await self._account_cas(account_row, account, reserved.account)
            await self.session.flush()
        round_ = validated_update(
            round_, status="locked" if allow_orders else "skipped", revision=round_.revision + 1
        )
        self._save_round(row, round_, metadata)
        await self.session.flush()
        return round_

    async def _market(
        self, listing_id: int, day: date, *, cutoff: datetime, identity: int | None = None
    ) -> MarketSnapshot | None:
        query = select(MarketSnapshot).where(
            MarketSnapshot.listing_id == listing_id,
            MarketSnapshot.trade_date == day,
            MarketSnapshot.adjust == "none",
            MarketSnapshot.captured_at <= cutoff,
        )
        if identity is not None:
            query = query.where(MarketSnapshot.id == identity)
        return cast(
            MarketSnapshot | None,
            await self.session.scalar(
                query.order_by(MarketSnapshot.version.desc(), MarketSnapshot.id.desc()).limit(1)
            ),
        )

    @staticmethod
    def _market_payload(bar: MarketSnapshot) -> dict[str, Any]:
        return {
            key: as_utc(bar.captured_at).isoformat()
            if key == "captured_at"
            else str(getattr(bar, key))
            for key in (
                "id",
                "listing_id",
                "trade_date",
                "adjust",
                "version",
                "open",
                "high",
                "low",
                "close",
                "volume",
                "amount",
                "trade_status",
                "is_st",
                "captured_at",
                "raw_ref",
            )
        }

    async def _persist_settlement(
        self,
        order: SimulationOrder,
        fill: SimulationFill | None,
        *,
        now: datetime,
        rules: Any,
        reason: str,
        cancelled: bool = False,
    ) -> None:
        _, _, account_row, account = await self._participant(
            order.experiment_id, order.participant_id
        )
        result = settle_order(
            account,
            order,
            fill,
            confirmed_at=now,
            source_ref=reason,
            expected_revision=account.revision,
            next_trading_day=next_trading_day(
                self._calendar_for((await self._experiment(order.experiment_id))[1]),
                order.execution_date,
            )
            if fill and order.side == "buy"
            else None,
            event_sequence=await self._sequence(account.account_id),
            expiry_reason=reason,
            cancelled=cancelled,
            rules=rules,
        )
        if fill is not None:
            prior = await self.session.get(FillRow, fill.id)
            if prior is not None:
                raise SimulationError(
                    "reconciliation_failed", "fill exists without its atomic order settlement"
                )
            self.session.add(
                FillRow(
                    id=fill.id,
                    **{
                        key: getattr(fill, key)
                        for key in (
                            *SCOPE,
                            "round_id",
                            "order_id",
                            "execution_key",
                            "sequence",
                            "economic_at",
                            "confirmed_at",
                        )
                    },
                    payload=fill.model_dump(mode="json"),
                )
            )
        for event in result.events:
            self._event(event)
        row = await self.session.get(OrderRow, order.id)
        assert row is not None
        row.status, row.payload = result.order.status, result.order.model_dump(mode="json")
        await self._account_cas(account_row, account, result.account)
        await self.session.flush()

    async def _cancel_future(self, experiment_id: str, now: datetime) -> None:
        _, experiment = await self._experiment(experiment_id)
        for order in await self.orders(experiment_id):
            if order.status == "locked" and order.valid_from > now:
                await self._persist_settlement(
                    order,
                    None,
                    now=now,
                    rules=experiment.config.rules,
                    reason="experiment_paused_or_ended_before_execution",
                    cancelled=True,
                )
        for row in (
            await self.session.scalars(
                select(RoundRow).where(RoundRow.experiment_id == experiment_id)
            )
        ).all():
            _, round_, metadata = await self._round(experiment_id, row.id)
            if round_.execution_at > now and round_.status in {
                "waiting_data",
                "collecting_decisions",
                "locked",
                "blocked",
            }:
                self._save_round(
                    row,
                    validated_update(round_, status="cancelled", revision=round_.revision + 1),
                    metadata,
                )

    async def settle_round(self, experiment_id: str, round_id: str) -> SimulationRound:
        await self._reserve(experiment_id)
        _, experiment = await self._experiment(experiment_id)
        row, round_, metadata = await self._round(experiment_id, round_id)
        if round_.status in {"settled", "cancelled", "skipped"}:
            return round_
        if round_.status not in {
            "locked",
            "awaiting_execution_data",
            "blocked",
        } or not metadata.get("decisions_opened"):
            raise SimulationError("invalid_transition", "only a previously locked round can settle")
        now = self.now()
        self._check_current_calendar(experiment)
        if now < round_.execution_at:
            raise SimulationError("invalid_transition", "execution event has not occurred")
        if now < datetime.combine(round_.execution_date, time(15), SHANGHAI_TZ):
            raise SimulationError(
                "incomplete_data", "daily execution confirmation requires the closed session"
            )
        reasons: list[str] = []
        pins = dict(metadata.get("execution_pins", {}))
        for order in await self.orders(experiment_id):
            if order.round_id != round_id or order.status != "locked":
                continue
            pin = pins.get(str(order.listing_id), {})
            eligibility = await execution_eligibility(
                self.session,
                experiment_id=experiment_id,
                listing_id=order.listing_id,
                trade_date=order.execution_date,
                cutoff=now,
                evidence_id=pin.get("evidence_id"),
            )
            bar = await self._market(
                order.listing_id, order.execution_date, cutoff=now, identity=pin.get("market_id")
            )
            if (
                not eligibility.ready
                or eligibility.evidence is None
                or eligibility.evidence_id is None
                or bar is None
            ):
                reasons.extend(
                    f"{order.listing_id}:{reason}"
                    for reason in (eligibility.missing or ("raw_execution_bar_missing",))
                )
                continue
            proof = eligibility.evidence
            reserved = ReservationTerms.model_validate(
                metadata["reservation_terms"][str(order.listing_id)]
            )
            if (
                proof.rule_version != reserved.rule_version
                or proof.tick_size != reserved.tick_size
                or proof.lot_size != reserved.lot_size
            ):
                reasons.append(f"execution_rule_changed:{order.listing_id}")
                continue
            if (
                any(
                    not value.is_finite()
                    for value in (bar.open, bar.high, bar.low, bar.close, bar.volume)
                )
                or min(bar.open, bar.high, bar.low, bar.close) <= 0
                or bar.low > min(bar.open, bar.close)
                or bar.high < max(bar.open, bar.close)
                or bar.volume < 0
                or bar.trade_status not in {0, 1}
                or bar.is_st is None
                or bar.is_st != proof.is_st
            ):
                reasons.append(f"invalid_raw_execution_bar:{order.listing_id}")
                continue
            if proof.corporate_action_refs:
                account = await self.account(experiment_id, order.participant_id)
                applied = {
                    event.source_ref
                    for event in await self.ledger(experiment_id, order.participant_id)
                    if event.kind == "corporate_action"
                }
                if (account.state.lots or account.state.receivables) and not set(
                    proof.corporate_action_refs
                ) <= applied:
                    reasons.append(f"unapplied_corporate_actions:{order.listing_id}")
                    continue
            digest = content_hash(self._market_payload(bar))
            if pin and (
                pin.get("market_hash") != digest
                or pin.get("evidence_hash") != eligibility.evidence_hash
            ):
                raise SimulationError("reconciliation_failed", "pinned execution evidence changed")
            execution_terms = ExecutionTerms(
                listing_id=proof.listing_id,
                trade_date=proof.trade_date,
                rule_version=proof.rule_version,
                source_ref=eligibility.evidence_id,
                verified=True,
                tick_size=proof.tick_size,
                lot_size=proof.lot_size,
                known_at=max(proof.captured_at, proof.published_at),
                upper_limit=proof.upper_limit,
                lower_limit=proof.lower_limit,
                corporate_actions_complete=proof.corporate_actions_complete,
                unsupported_corporate_actions=proof.unsupported_corporate_actions,
            )
            execution_bar = ExecutionBar(
                listing_id=bar.listing_id,
                trade_date=bar.trade_date,
                open=bar.open,
                volume=bar.volume,
                suspended=bar.trade_status == 0,
                source_ref=f"market_snapshot:{bar.id}:v{bar.version}",
                confirmed_at=as_utc(bar.captured_at),
                verified=True,
            )
            outcome = match_daily_open(
                order, execution_bar, execution_terms, experiment.config.rules, confirmed_at=now
            )
            if outcome.status in {"awaiting_data", "blocked"}:
                reasons.append(f"{order.listing_id}:{outcome.reason}")
                continue
            pins[str(order.listing_id)] = {
                "evidence_id": eligibility.evidence_id,
                "evidence_hash": eligibility.evidence_hash,
                "market_id": bar.id,
                "market_hash": digest,
            }
            await self._persist_settlement(
                order,
                outcome.fill,
                now=now,
                rules=experiment.config.rules,
                reason=outcome.reason or "daily_open_v1",
            )
        metadata["execution_pins"], metadata["blocking_reasons"] = pins, sorted(set(reasons))
        remaining = [
            order
            for order in await self.orders(experiment_id)
            if order.round_id == round_id and order.status == "locked"
        ]
        round_ = validated_update(
            round_,
            status="awaiting_execution_data" if remaining else "settled",
            revision=round_.revision + 1,
        )
        self._save_round(row, round_, metadata)
        await self.session.flush()
        await self.value(experiment_id, round_.execution_date)
        return round_

    async def ingest_execution_evidence(
        self,
        experiment_id: str,
        evidence: ExecutionEvidenceInput,
        *,
        source_kind: Literal["user", "provider"],
        source_ref: str,
    ) -> str:
        await self._reserve(experiment_id)
        experiment = (await self._experiment(experiment_id))[1]
        return await import_execution_evidence(
            self.session,
            experiment_id=experiment_id,
            allowed_listing_ids=experiment.config.universe,
            evidence=evidence,
            source_kind=source_kind,
            source_ref=source_ref,
            imported_at=self.now(),
        )

    async def value(self, experiment_id: str, valuation_date: date) -> tuple[NavSnapshot, ...]:
        await self._reserve(experiment_id)
        experiment = (await self._experiment(experiment_id))[1]
        now = self.now()
        if now < datetime.combine(valuation_date, time(15), SHANGHAI_TZ):
            raise SimulationError("incomplete_data", "daily close has not occurred")
        if valuation_date < experiment.created_at.astimezone(SHANGHAI_TZ).date():
            raise SimulationError("invalid_config", "valuation predates account creation")
        if not self._calendar_for(experiment).is_trading_day(valuation_date):
            raise SimulationError("invalid_config", "valuation requires reviewed trading day")
        result = []
        outstanding = [
            o
            for o in await self.orders(experiment_id)
            if o.status == "locked" and o.execution_date <= valuation_date
        ]
        for participant in await self.participants(experiment_id):
            account = await self.account(experiment_id, participant.id)
            if any(
                event.economic_at.astimezone(SHANGHAI_TZ).date() > valuation_date
                for event in await self.ledger(experiment_id, participant.id)
            ):
                raise SimulationError(
                    "invalid_transition",
                    "historical valuation requires its original account projection",
                )
            marks = []
            reasons = [
                f"unsettled_order:{o.id}" for o in outstanding if o.participant_id == participant.id
            ]
            for listing_id in sorted({lot.listing_id for lot in account.state.lots}):
                bar = await self._market(listing_id, valuation_date, cutoff=now)
                eligibility = await execution_eligibility(
                    self.session,
                    experiment_id=experiment_id,
                    listing_id=listing_id,
                    trade_date=valuation_date,
                    cutoff=now,
                )
                if not eligibility.ready or eligibility.evidence is None:
                    reasons.extend(
                        f"valuation:{listing_id}:{reason}" for reason in eligibility.missing
                    )
                elif eligibility.evidence.corporate_action_refs:
                    applied = {
                        e.source_ref
                        for e in await self.ledger(experiment_id, participant.id)
                        if e.kind == "corporate_action"
                    }
                    if not set(eligibility.evidence.corporate_action_refs) <= applied:
                        reasons.append(f"unapplied_corporate_actions:{listing_id}")
                if (
                    bar is not None
                    and bar.close.is_finite()
                    and bar.close > 0
                    and bar.trade_status in {0, 1}
                ):
                    marks.append(
                        ValuationMark(
                            listing_id=listing_id,
                            price=bar.close,
                            price_date=bar.trade_date,
                            source_ref=f"market_snapshot:{bar.id}:v{bar.version}",
                            known_at=as_utc(bar.captured_at),
                            verified=True,
                            stale_reason="suspended" if bar.trade_status == 0 else None,
                        )
                    )
            previous = await self.nav(experiment_id, participant.id)
            latest: dict[date, NavSnapshot] = {}
            for snapshot in previous:
                if snapshot.valuation_date < valuation_date and (
                    snapshot.valuation_date not in latest
                    or snapshot.version > latest[snapshot.valuation_date].version
                ):
                    latest[snapshot.valuation_date] = snapshot
            history = tuple(latest[day] for day in sorted(latest))
            snapshot = value_account(
                account,
                valuation_date,
                tuple(marks),
                experiment.config.rules,
                created_at=now,
                history=history,
                blocking_reasons=tuple(sorted(set(reasons))),
            )
            same = next(
                (
                    n
                    for n in previous
                    if n.valuation_date == valuation_date and n.input_hash == snapshot.input_hash
                ),
                None,
            )
            if same is not None:
                result.append(same)
                continue
            version = (
                max((n.version for n in previous if n.valuation_date == valuation_date), default=0)
                + 1
            )
            snapshot = validated_update(snapshot, version=version)
            self.session.add(
                NavRow(
                    id=snapshot.id,
                    **{
                        key: getattr(snapshot, key)
                        for key in (
                            *SCOPE,
                            "account_revision",
                            "valuation_date",
                            "version",
                            "quality",
                            "input_hash",
                            "valuation_version",
                            "metrics_version",
                        )
                    },
                    payload=snapshot.model_dump(mode="json"),
                )
            )
            result.append(snapshot)
        await self.session.flush()
        return tuple(result)

    async def leaderboard(self, experiment_id: str) -> dict[str, Any]:
        participants = await self.participants(experiment_id)
        navs = await self.nav(experiment_id)
        by_account = {}
        for p in participants:
            latest: dict[date, NavSnapshot] = {}
            for n in navs:
                if n.participant_id == p.id and (
                    n.valuation_date not in latest or n.version > latest[n.valuation_date].version
                ):
                    latest[n.valuation_date] = n
            by_account[p.id] = latest
        common = (
            set.intersection(*(set(values) for values in by_account.values()))
            if by_account
            else set()
        )
        day = max(common) if common else None
        rows: list[dict[str, Any]] = []
        for p in participants:
            snapshot = by_account[p.id].get(day) if day is not None else None
            valid = (
                snapshot is not None
                and snapshot.quality == "complete"
                and snapshot.cumulative_return is not None
                and len(common) >= 2
            )
            rows.append(
                {
                    "participant_id": p.id,
                    "name": p.config.name,
                    "kind": p.config.kind,
                    "quality": snapshot.quality if snapshot else "incomplete",
                    "official": valid,
                    "cumulative_return": str(snapshot.cumulative_return)
                    if snapshot and snapshot.cumulative_return is not None
                    else None,
                    "max_drawdown": str(snapshot.max_drawdown)
                    if snapshot and snapshot.max_drawdown is not None
                    else None,
                    "nav_id": snapshot.id if snapshot else None,
                    "rank": None,
                }
            )
        eligible = sorted(
            (r for r in rows if r["official"]),
            key=lambda r: (-Decimal(r["cumulative_return"]), r["participant_id"]),
        )
        for rank, row in enumerate(eligible, 1):
            row["rank"] = rank
        return {
            "valuation_date": str(day) if day else None,
            "common_observations": len(common),
            "ranking_policy": "net_return_complete_common_dates_minimum_2",
            "rows": rows,
        }

    async def corporate_action(
        self,
        experiment_id: str,
        participant_id: str,
        action: CorporateAction,
        *,
        expected_revision: int,
        idempotency_key: str,
        entitlement_date: date | None = None,
        cash_per_share: Decimal | None = None,
    ) -> SimulationAccount:
        """Trusted writer with explicit record-date cash entitlement reconciliation.

        cash_per_share is the experimentally configured net cash entitlement,
        after any explicit tax assumptions, rounded once to cents HALF_UP.
        """
        action = CorporateAction.model_validate(action.model_dump())
        await self._reserve(experiment_id)
        body = {
            "action": action.model_dump(mode="json"),
            "expected_revision": expected_revision,
            "entitlement_date": str(entitlement_date) if entitlement_date is not None else None,
            "cash_per_share": str(cash_per_share) if cash_per_share is not None else None,
        }
        request_scope = f"corporate:{experiment_id}:{participant_id}"
        replay = await self._replay(
            request_scope,
            idempotency_key,
            body,
            SimulationAccount,
            expected={"experiment_id": experiment_id, "participant_id": participant_id},
        )
        if replay:
            return replay
        _, experiment = await self._experiment(experiment_id)
        _, _, account_row, account = await self._participant(experiment_id, participant_id)
        if action.listing_id not in experiment.config.universe:
            raise SimulationError(
                "unsupported_security", "corporate action listing is outside experiment"
            )
        if account.revision != expected_revision:
            raise SimulationError("revision_conflict", "account changed before corporate action")
        if action.confirmed_at > self.now() or action.economic_at < experiment.created_at:
            raise SimulationError(
                "future_evidence",
                "corporate action is outside account lifetime or confirmation clock",
            )
        event_id = stable_id("event", account.account_id, action.id)
        prior = await self.session.get(EventRow, event_id)
        if prior is not None:
            if prior.payload.get("_service", {}).get("corporate_action") != action.model_dump(
                mode="json"
            ):
                raise SimulationError(
                    "idempotency_conflict", "corporate action identity reused with another body"
                )
            proof_metadata = prior.payload.get("_service", {})
            if (
                proof_metadata.get("entitlement_date") != body["entitlement_date"]
                or proof_metadata.get("cash_per_share") != body["cash_per_share"]
            ):
                raise SimulationError("idempotency_conflict", "corporate action proof changed")
            self._remember(request_scope, idempotency_key, body, account)
            return account
        ledger = await self.ledger(experiment_id, participant_id)
        entitlement_quantity = None
        if action.kind == "cash_entitlement":
            if (
                entitlement_date is None
                or not isinstance(cash_per_share, Decimal)
                or not cash_per_share.is_finite()
                or cash_per_share <= 0
            ):
                raise SimulationError(
                    "incomplete_data",
                    "cash entitlement requires record date and finite positive net cash per share",
                )
            record_close = datetime.combine(entitlement_date, time(15), SHANGHAI_TZ)
            if (
                not self._calendar_for(experiment).is_trading_day(entitlement_date)
                or record_close > action.economic_at
                or record_close < experiment.created_at
            ):
                raise SimulationError(
                    "invalid_config", "record date is outside verified account history"
                )
            with localcontext() as context:
                context.prec = 100
                entitlement_quantity = sum(
                    (
                        event.quantity_delta
                        for event in ledger
                        if event.listing_id == action.listing_id
                        and event.economic_at <= record_close
                    ),
                    Decimal(0),
                )
                expected_cash = (entitlement_quantity * cash_per_share).quantize(
                    Decimal("0.01"), rounding=ROUND_HALF_UP
                )
            if entitlement_quantity <= 0 or expected_cash != action.amount:
                raise SimulationError(
                    "reconciliation_failed",
                    "cash entitlement differs from record-date holdings and per-share cash",
                )
            for event_row in (
                await self.session.scalars(
                    select(EventRow).where(EventRow.account_id == account.account_id)
                )
            ).all():
                raw = event_row.payload.get("_service", {}).get("corporate_action")
                if raw is not None:
                    historical = CorporateAction.model_validate(raw)
                    if (
                        historical.kind == "cash_entitlement"
                        and historical.listing_id == action.listing_id
                        and historical.entitlement_ref == action.entitlement_ref
                    ):
                        raise SimulationError(
                            "idempotency_conflict", "record-date entitlement was already accrued"
                        )
        elif entitlement_date is not None or cash_per_share is not None:
            raise SimulationError(
                "invalid_config", "record-date proof fields apply only to cash entitlement"
            )
        if action.kind == "split" and any(
            event.quantity_delta
            and event.listing_id == action.listing_id
            and event.economic_at >= action.economic_at
            for event in ledger
        ):
            raise SimulationError(
                "settlement_blocked", "late split requires replay before later economic fills"
            )
        if action.kind == "cash_payment":
            entitlements = Decimal(0)
            paid = Decimal(0)
            for row in (
                await self.session.scalars(
                    select(EventRow).where(EventRow.account_id == account.account_id)
                )
            ).all():
                self._decode(
                    row, LedgerEvent, ("id", *SCOPE, "sequence", "account_revision", "kind")
                )
                raw = row.payload.get("_service", {}).get("corporate_action")
                if raw is None:
                    continue
                historical = CorporateAction.model_validate(raw)
                if (
                    historical.listing_id == action.listing_id
                    and historical.entitlement_ref == action.entitlement_ref
                ):
                    if historical.kind == "cash_entitlement":
                        with localcontext() as context:
                            context.prec = 100
                            entitlements += historical.amount
                    elif historical.kind == "cash_payment":
                        with localcontext() as context:
                            context.prec = 100
                            paid += historical.amount
            if action.amount > entitlements - paid:
                raise SimulationError(
                    "reconciliation_failed", "payment exceeds this account/listing entitlement"
                )
        result = apply_corporate_action(
            account,
            action,
            expected_revision=expected_revision,
            event_sequence=await self._sequence(account.account_id),
        )
        self._event(
            result.event,
            {
                "corporate_action": action.model_dump(mode="json"),
                "entitlement_date": str(entitlement_date) if entitlement_date else None,
                "cash_per_share": str(cash_per_share) if cash_per_share is not None else None,
                "entitlement_quantity": str(entitlement_quantity)
                if entitlement_quantity is not None
                else None,
            },
        )
        await self._account_cas(account_row, account, result.account)
        self._remember(request_scope, idempotency_key, body, result.account)
        await self.session.flush()
        return result.account

    async def private_context(
        self, experiment_id: str, round_id: str, participant_id: str
    ) -> PrivateContext:
        _, participant, _, account = await self._participant(experiment_id, participant_id)
        round_ = (await self._round(experiment_id, round_id))[1]
        if round_.status != "collecting_decisions":
            raise SimulationError(
                "invalid_transition", "private decision context requires an open round"
            )
        common = await self.common_facts(experiment_id, round_id)
        if any(
            event.confirmed_at > round_.cutoff
            for event in await self.ledger(experiment_id, participant_id)
        ):
            raise SimulationError(
                "future_evidence", "account projection contains facts confirmed after round cutoff"
            )
        settled = {
            r.id
            for r in await self.rounds(experiment_id)
            if r.status == "settled" and r.execution_at <= round_.cutoff
        }
        decisions = tuple(
            d
            for d in await self.decisions(experiment_id, participant_id)
            if d.round_id in settled
            and d.submitted_at is not None
            and d.submitted_at <= round_.cutoff
            and d.status in {"locked", "invalid", "timeout", "failed", "unknown", "cancelled"}
        )
        reviews = (
            await self.reviews(experiment_id, participant_id)
            if participant.config.memory_policy == "matured_own_reviews"
            else ()
        )
        latest: dict[str, SimulationReview] = {}
        for review in reviews:
            if review.available_at > round_.cutoff:
                continue
            if (
                review.decision_id not in latest
                or review.version > latest[review.decision_id].version
            ):
                latest[review.decision_id] = review
        usable = tuple(
            r
            for r in latest.values()
            if r.status == "matured" and r.memory_eligible and r.available_at <= round_.cutoff
        )
        return own_account_context(
            scope=AccountScope(**{key: getattr(account, key) for key in SCOPE}),
            account=account,
            common=common,
            decisions=decisions,
            reviews=usable,
            settled_round_ids=tuple(settled),
        )

    async def generate_reviews(self, experiment_id: str) -> tuple[SimulationReview, ...]:
        await self._reserve(experiment_id)
        settled = {r.id: r for r in await self.rounds(experiment_id) if r.status == "settled"}
        generated = []
        for participant in await self.participants(experiment_id):
            previous = await self.reviews(experiment_id, participant.id)
            navs = await self.nav(experiment_id, participant.id)
            latest_navs: dict[date, NavSnapshot] = {}
            for nav in navs:
                if nav.created_at > self.now():
                    continue
                if (
                    nav.valuation_date not in latest_navs
                    or nav.version > latest_navs[nav.valuation_date].version
                ):
                    latest_navs[nav.valuation_date] = nav
            for decision in await self.decisions(experiment_id, participant.id):
                if decision.status != "locked" or decision.round_id not in settled:
                    continue
                old = max(
                    (r for r in previous if r.decision_id == decision.id),
                    key=lambda r: r.version,
                    default=None,
                )
                review = review_decision(
                    decision,
                    tuple(latest_navs.values()),
                    observation_start=settled[decision.round_id].execution_date,
                    now=self.now(),
                    version=old.version + 1 if old else 1,
                )
                if old and old.status == review.status and old.nav_refs == review.nav_refs:
                    generated.append(old)
                    continue
                self._store_review(review)
                generated.append(review)
        await self.session.flush()
        return tuple(generated)

    def _store_review(self, review: SimulationReview) -> None:
        self.session.add(
            ReviewRow(
                id=review.id,
                **{
                    key: getattr(review, key)
                    for key in (*SCOPE, "decision_id", "version", "status", "available_at")
                },
                payload=review.model_dump(mode="json"),
            )
        )

    async def save_model_review(
        self, experiment_id: str, participant_id: str, review: SimulationReview
    ) -> SimulationReview:
        review = SimulationReview.model_validate(review.model_dump())
        await self._reserve(experiment_id)
        _, _, _, account = await self._participant(experiment_id, participant_id)
        if any(getattr(review, key) != getattr(account, key) for key in SCOPE):
            raise SimulationError("scope_mismatch", "review differs from bound account")
        decision = next(
            (
                d
                for d in await self.decisions(experiment_id, participant_id)
                if d.id == review.decision_id
            ),
            None,
        )
        if decision is None or decision.status != "locked":
            raise SimulationError("not_found", "review original decision not found")
        round_ = (await self._round(experiment_id, decision.round_id))[1]
        if round_.status != "settled":
            raise SimulationError("invalid_transition", "review requires settled decision")
        old_reviews = [
            r
            for r in await self.reviews(experiment_id, participant_id)
            if r.decision_id == decision.id
        ]
        existing = next((r for r in old_reviews if r.version == review.version), None)
        if existing is not None:
            if existing.model_dump(exclude={"id", "available_at"}) != review.model_dump(
                exclude={"id", "available_at"}
            ):
                raise SimulationError(
                    "idempotency_conflict", "review version reused with another result"
                )
            return existing
        expected = max((r.version for r in old_reviews), default=0) + 1
        now = self.now()
        navs = {n.id: n for n in await self.nav(experiment_id, participant_id)}
        latest_navs: dict[date, NavSnapshot] = {}
        for nav in navs.values():
            if nav.created_at <= now and (
                nav.valuation_date not in latest_navs
                or nav.version > latest_navs[nav.valuation_date].version
            ):
                latest_navs[nav.valuation_date] = nav
        maturity = review_decision(
            decision, tuple(latest_navs.values()), observation_start=round_.execution_date, now=now
        )
        if review.status == "matured" and (
            maturity.status != "matured" or review.observation_end != maturity.observation_end
        ):
            raise SimulationError(
                "incomplete_data", "model review cannot shorten the frozen observation horizon"
            )
        if review.available_at > now:
            raise SimulationError("future_evidence", "review availability is in the future")
        # Availability is an observed server fact; model-supplied times cannot
        # backdate knowledge into already-frozen participant memories.
        review = validated_update(review, available_at=now)
        if review.version != expected:
            raise SimulationError("revision_conflict", "review version changed")
        if (
            review.available_at > now
            or review.observation_start != round_.execution_date
            or review.observation_end > now.astimezone(SHANGHAI_TZ).date()
        ):
            raise SimulationError(
                "future_evidence", "review observation window or availability is invalid"
            )
        if not review.nav_refs or any(
            ref not in navs
            or navs[ref].created_at > review.available_at
            or not review.observation_start <= navs[ref].valuation_date <= review.observation_end
            or latest_navs.get(navs[ref].valuation_date) != navs[ref]
            for ref in review.nav_refs
        ):
            raise SimulationError(
                "scope_mismatch", "review NAV reference is not available in own account"
            )
        if review.status not in {"matured", "failed"} or (
            review.status == "matured"
            and not any(
                navs[ref].quality == "complete"
                and navs[ref].valuation_date == review.observation_end
                for ref in review.nav_refs
            )
        ):
            raise SimulationError(
                "incomplete_data", "model review requires a complete end valuation"
            )
        allowed_evidence: set[str] = set()
        for known_round in await self.rounds(experiment_id):
            if known_round.cutoff <= review.available_at:
                allowed_evidence.update(ref.id for ref in known_round.manifest.evidence)
        if not set(review.evidence_refs) <= allowed_evidence:
            raise SimulationError("future_evidence", "review cites unavailable evidence")
        review = validated_update(review, id=stable_id("review", decision.id, review.version))
        self._store_review(review)
        await self.session.flush()
        return review

    async def export(self, experiment_id: str) -> dict[str, Any]:
        value = await self.detail(experiment_id)
        for key, records in (
            ("decisions", await self.decisions(experiment_id)),
            ("orders", await self.orders(experiment_id)),
            ("fills", await self.fills(experiment_id)),
            ("nav", await self.nav(experiment_id)),
            ("reviews", await self.reviews(experiment_id)),
        ):
            value[key] = [record.model_dump(mode="json") for record in records]
        value["ledger"] = [
            event.model_dump(mode="json")
            for participant in await self.participants(experiment_id)
            for event in await self.ledger(experiment_id, participant.id)
        ]
        value["common_facts"] = [
            (await self.common_facts(experiment_id, r.id)).payload()
            for r in await self.rounds(experiment_id)
        ]
        value["schema_version"] = "simulation_export_v1"
        return cast(dict[str, Any], safe_export(value))
