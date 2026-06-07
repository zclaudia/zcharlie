"""/strategies endpoints."""

from fastapi import APIRouter, Depends, HTTPException

from investment_agent.api.auth import verify_api_key
from investment_agent.api.schemas.strategy import StrategyOut
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.analysis import Strategy
from investment_agent.storage.repositories.strategy import StrategyRepository

router = APIRouter(
    prefix="/strategies",
    tags=["strategies"],
    dependencies=[Depends(verify_api_key)],
)


def _to_out(s: Strategy) -> StrategyOut:
    return StrategyOut(
        code=s.code,
        name=s.name,
        version=s.version,
        description=s.description,
        enabled=s.enabled,
        config=dict(s.config_json),
    )


@router.get("", response_model=list[StrategyOut])
async def list_strategies() -> list[StrategyOut]:
    async with session_scope() as session:
        rows = await StrategyRepository(session).list_all()
        return [_to_out(r) for r in rows]


@router.get("/{code}", response_model=StrategyOut)
async def get_strategy(code: str) -> StrategyOut:
    async with session_scope() as session:
        s = await StrategyRepository(session).get(code)
        if s is None:
            raise HTTPException(
                status_code=404,
                detail=f"Strategy '{code}' not found",
            )
        return _to_out(s)
