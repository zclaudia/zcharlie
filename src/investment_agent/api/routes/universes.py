"""/universes endpoint."""

from fastapi import APIRouter, Depends
from sqlalchemy import select

from investment_agent.api.auth import verify_api_key
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.reference import Universe

router = APIRouter(
    prefix="/universes",
    tags=["universes"],
    dependencies=[Depends(verify_api_key)],
)


@router.get("")
async def list_universes() -> list[dict[str, object]]:
    async with session_scope() as session:
        rows = list(
            (
                await session.execute(
                    select(Universe).order_by(Universe.code),
                )
            )
            .scalars()
            .all(),
        )
        return [
            {"code": u.code, "name": u.name, "description": u.description}
            for u in rows
        ]
