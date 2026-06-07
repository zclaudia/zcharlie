"""/strategies endpoint schemas."""

from pydantic import BaseModel


class StrategyOut(BaseModel):
    code: str
    name: str
    version: str
    description: str | None
    enabled: bool
    config: dict[str, object]
