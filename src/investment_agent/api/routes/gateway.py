"""``GET /gateway/status`` - connection state of the mobile gateway link.

Read-only. Auth: ``X-API-Key`` or the web session. Reports whether this
service is registered as a gateway Backend (and under which ``backend_id`` a
phone finds it); the credential is never returned.
"""

from __future__ import annotations

from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel

from investment_agent.api.auth import verify_api_key_or_session
from investment_agent.gateway.manager import GatewayManager, GatewayStatus

router = APIRouter(
    prefix="/gateway",
    tags=["gateway"],
    dependencies=[Depends(verify_api_key_or_session)],
)


class GatewayStatusOut(BaseModel):
    state: Literal["disabled", "incomplete", "connecting", "connected", "replaced", "error"]
    detail: str | None = None
    url: str | None = None
    namespace: str | None = None
    backend_id: str | None = None
    epoch: int | None = None


@router.get("/status", response_model=GatewayStatusOut)
async def gateway_status(request: Request) -> GatewayStatusOut:
    manager: GatewayManager | None = getattr(request.app.state, "gateway", None)
    status = manager.status() if manager is not None else GatewayStatus("disabled")
    return GatewayStatusOut(**status.__dict__)
