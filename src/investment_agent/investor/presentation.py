"""Read-only, server-resolved publication bodies at their immutable research cutoff."""
from typing import Any

from investment_agent.evidence.store import EvidenceService
from investment_agent.investor.common import ObjectRef
from investment_agent.investor.contracts import EntityNode, NumericNode
from investment_agent.investor.enums import CompanyAction
from investment_agent.investor.foundations import InvestorError, InvestorService
from investment_agent.investor.publication import PublicationService
from investment_agent.investor.publication_numbers import NumericResolver


async def recommendation_body(service: InvestorService, ref: ObjectRef) -> dict[str, Any]:
    view = await PublicationService(service.session).get_recommendation(ref)
    original = view.recommendation
    case = await service.get_case("local_owner", original.case_ids[0])
    resolver = NumericResolver(EvidenceService(service.session), case.listing_ref, original.as_of)
    resolved: dict[str, Any] = {}
    errors: dict[str, str] = {}

    async def walk(value: Any, path: str) -> None:
        if isinstance(value, dict):
            if value.get("type") in {"numeric_claim", "entity_ref"}:
                try:
                    node = NumericNode.model_validate(value) if value["type"] == "numeric_claim" else EntityNode.model_validate(value)
                    resolved[path] = (await resolver.resolve(node)).model_dump(mode="json")
                except (InvestorError, LookupError, ValueError) as exc:
                    errors[path] = str(exc)
            else:
                for key, child in value.items():
                    await walk(child, f"{path}.{key}" if path else key)
        elif isinstance(value, list):
            for index, child in enumerate(value):
                await walk(child, f"{path}.{index}")

    identity = await resolver.resolve(EntityNode(type="entity_ref", ref=case.listing_ref))
    await walk(original.model_dump(mode="json"), "recommendation")
    return {"view": view.model_dump(mode="json"), "subject_label": identity.text,
            "action_labels": {action.value: action.label for action in CompanyAction}, "subject_ref": case.listing_ref.model_dump(mode="json"),
            "body_as_of": original.as_of.isoformat(), "resolved_nodes": resolved, "render_errors": errors,
            "historical_body": True}
