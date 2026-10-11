"""Opt-in native trusted registrations. AG13 supplies grants; AG09 grants are unchanged."""

from typing import Any
from uuid import UUID

from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.agent.execution import ExecutionDeniedError
from investment_agent.agent.tools import ToolRegistration
from investment_agent.investor.common import ObjectRef
from investment_agent.storage.repositories.listing import ListingRepository
from investment_agent.valuation.contracts import (
    PercentileRequest,
    QualificationResolver,
    ReverseDCFRequest,
)
from investment_agent.valuation.service import ValuationService


def tool_summary(output: dict[str, Any]) -> dict[str, Any]:
    """Keep daily input audit trails out of the model context; retain the immutable ref."""
    record = output["calculation"]
    details = record["details"]
    return {"status": output["status"], "method_version": output["method_version"],
            "calculation_ref": output["calculation_ref"], "result": output["result"],
            "as_of": record["as_of"], "applicable": record["applicable"],
            "input_quality": record["quality"], "errors": record["errors"],
            "assumptions": record["assumptions"],
            "scenarios": details.get("scenarios", []), "warnings": details.get("warnings", []),
            "coverage": details.get("factor", {}),
            "current_price": details.get("current_price"),
            "current_price_date": details.get("current_price_date"),
            "latest_report_period": details.get("latest_report_period")}


def valuation_registrations(qualification: QualificationResolver | None = None
                            ) -> tuple[ToolRegistration, ...]:
    """Qualification is a trusted application dependency, never a model-controlled parameter.

    AG11 implements QualificationResolver and enables only validated source mappings.
    AG13 explicitly registers these tools and supplies matching mandate-based grants.
    No get_snapshot/message admission side-effect grants calculation permissions.
    """
    async def dcf_scope(session: AsyncSession, business: BaseModel) -> tuple[ObjectRef, ...]:
        assert isinstance(business, ReverseDCFRequest)
        return await ValuationService(session, qualification).scope("local_owner", business)

    async def percentile_scope(session: AsyncSession, business: BaseModel) -> tuple[ObjectRef, ...]:
        assert isinstance(business, PercentileRequest)
        listing = await ListingRepository(session).get_a_share_listing(business.ticker)
        if listing is None:
            raise ExecutionDeniedError("listing_unavailable")
        return (ObjectRef(kind="listing", id=listing.id, revision=1),)

    async def dcf(session: AsyncSession, operation: UUID, business: BaseModel) -> dict[str, Any]:
        assert isinstance(business, ReverseDCFRequest)
        return tool_summary(await ValuationService(session, qualification).reverse_dcf("local_owner", business, operation))

    async def percentile(session: AsyncSession, operation: UUID, business: BaseModel) -> dict[str, Any]:
        assert isinstance(business, PercentileRequest)
        return tool_summary(await ValuationService(session).percentile("local_owner", business, operation))

    return (ToolRegistration("reverse_dcf_fcfe", ReverseDCFRequest, dcf_scope, database=dcf),
            ToolRegistration("historical_valuation_percentile", PercentileRequest,
                             percentile_scope, database=percentile))
