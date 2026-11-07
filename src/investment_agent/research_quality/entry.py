"""G0 reads existing material and configuration, without admitting any work."""

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.model import AgentConfigurationError
from investment_agent.api.schemas.research_entry import (
    EntryBudget,
    EntryGap,
    EntryMaterial,
    EntryMethod,
    EntryModel,
    ResearchEntryInput,
    ResearchEntryResponse,
)
from investment_agent.config import Settings
from investment_agent.domain.a_share import format_listing_ticker
from investment_agent.domain.llm_catalog import CatalogError
from investment_agent.evidence.queries import SnapshotQueryService
from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef, OwnerId, utc_timestamp
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.methods import MethodService
from investment_agent.settings.loader import load_agent_model_config, load_settings
from investment_agent.storage.models.evidence import SavedEvidence
from investment_agent.storage.models.reference import Listing

# Small declared windows, not a claim of comprehensive company coverage.
TEMPLATE_FIELDS: dict[str, tuple[str, ...]] = {
    "company_research": ("revenue_main", "net_profit_parent", "operating_cash_flow"),
    "cash_flow": ("net_profit_parent", "operating_cash_flow", "capital_expenditure"),
    "counter_evidence": ("revenue_main", "net_profit_parent", "operating_cash_flow", "debt_ratio"),
}
EVIDENCE_LIMIT = 100


class ResearchEntryService:
    def __init__(
        self, session: AsyncSession, base: Settings, owner: OwnerId = "local_owner"
    ) -> None:
        self.session, self.base, self.owner = session, base, owner

    async def preflight(
        self, request: ResearchEntryInput, *, now: datetime | None = None
    ) -> ResearchEntryResponse:
        at = utc_timestamp(now) if now is not None else datetime.now(UTC)
        settings = await load_settings(self.session, base=self.base)
        if not settings.investor_agent_enabled:
            raise InvestorError("feature_disabled", "请先在设置中启用投资 Agent。")
        listing = await self.session.get(Listing, request.listing_ref.id)
        if listing is None:
            raise InvestorError("not_found", "证券不可用")
        if listing.market_code not in {"SSE", "SZSE", "BSE"}:
            raise InvestorError("invalid_reference", "此研究入口当前仅支持已有 A 股证券。")
        ticker = format_listing_ticker(ticker=listing.ticker, market_code=listing.market_code)
        materials: list[EntryMaterial] = []
        gaps: list[EntryGap] = []
        readable: set[str] = set()
        query = SnapshotQueryService(self.session)
        for field in TEMPLATE_FIELDS[request.template_key]:
            result = await query.get_snapshot(ticker, field, at)
            if result.status == "ok":
                readable.add(field)
            else:
                gaps.append(
                    EntryGap(
                        code=f"snapshot_{result.status}",
                        field=field,
                        message=f"{field}：{result.coverage}",
                    )
                )
            for record in result.data or ():
                materials.append(
                    EntryMaterial(
                        ref=record.source_ref,
                        field=field,
                        source_ref=record.source_ref,
                        source=record.source_kind,
                        locator=record.locator,
                        report_period=record.period.end if record.period else None,
                        value=record.value,
                        verification=record.verification,
                        status=result.status,
                    )
                )

        rows = list(
            await self.session.scalars(
                select(SavedEvidence)
                .where(
                    SavedEvidence.owner_id == self.owner,
                    SavedEvidence.payload["subject_ref"]["kind"].as_string() == "listing",
                    SavedEvidence.payload["subject_ref"]["id"].as_integer() == listing.id,
                )
                .order_by(SavedEvidence.id, SavedEvidence.revision.desc())
                .limit(EVIDENCE_LIMIT + 1)
            )
        )
        if len(rows) > EVIDENCE_LIMIT:
            gaps.append(
                EntryGap(
                    code="evidence_window_truncated",
                    field="evidence",
                    message="仅展示前100条已保存证据；此窗口不是完整性证明。",
                )
            )
        verified = 0
        for row in rows[:EVIDENCE_LIMIT]:
            ref = ObjectRef(kind="evidence", id=UUID(row.id), revision=row.revision)
            result_e = await EvidenceService(self.session).get_evidence(
                self.owner, ref, at, subject_ref=request.listing_ref, visible_at=at
            )
            saved_record = result_e.data
            if saved_record is None:
                continue
            verified += saved_record.verification == "verified" and result_e.status == "ok"
            materials.append(
                EntryMaterial(
                    ref=ref,
                    field="evidence",
                    source_ref=saved_record.source_ref,
                    source=saved_record.source_kind,
                    locator=saved_record.locator,
                    report_period=saved_record.period.end if saved_record.period else None,
                    value=saved_record.value,
                    verification=saved_record.verification,
                    status=result_e.status,
                )
            )
        if not verified:
            gaps.append(
                EntryGap(
                    code="verified_evidence_missing",
                    field="evidence",
                    message="本次已检查窗口中没有可用的已核验证据；候选快照不等于已核验事实，风险和缺失材料也不等于反证。",
                )
            )
        binding = await MethodService(self.session, self.owner).resolve_binding(
            request.listing_ref, "company_research"
        )
        model = await self._model()
        if model.availability == "unavailable":
            gaps.append(
                EntryGap(
                    code="model_unavailable", field="model", message=model.reason or "模型不可用"
                )
            )
        return ResearchEntryResponse(
            listing_ref=request.listing_ref,
            template_key=request.template_key,
            question=request.question,
            material_refs=tuple(dict.fromkeys(m.ref for m in materials)),
            coverage="partial" if materials else "missing",
            gaps=tuple(gaps),
            method=EntryMethod(
                method_ref=binding.method_ref,
                binding_schema_version=binding.schema_version,
                package_ref=binding.package_ref,
            ),
            model=model,
            preflight_at=at,
            materials=tuple(materials),
            budget=EntryBudget(
                requests=settings.investor_agent_task_requests,
                tokens=settings.investor_agent_task_tokens,
                seconds=settings.investor_agent_task_seconds,
            ),
            calculability="inputs_visible"
            if set(TEMPLATE_FIELDS[request.template_key]) <= readable
            else "missing_inputs",
        )

    async def _model(self) -> EntryModel:
        try:
            config = await load_agent_model_config(self.session, base=self.base)
            config.validate_ready()
        except (AgentConfigurationError, CatalogError):
            # Do not expose catalog errors, endpoint values or credential details.
            return EntryModel(
                availability="unavailable",
                reason="投资 Agent 主模型未配置或当前不可用，请检查模型设置。",
            )
        return EntryModel(
            availability="available",
            model=config.values["agent_model"],
            profile=config.values["agent_profile"],
            configuration_hash=config.version,
        )
