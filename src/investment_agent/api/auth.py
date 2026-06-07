"""X-API-Key authentication dependency."""

from typing import Annotated

from fastapi import Depends, Header, HTTPException, status

from investment_agent.config import Settings


def get_settings() -> Settings:
    return Settings()  # type: ignore[call-arg]


async def verify_api_key(
    settings: Annotated[Settings, Depends(get_settings)],
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
) -> None:
    if x_api_key is None or x_api_key != settings.api_key:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing or invalid X-API-Key",
            headers={"WWW-Authenticate": "ApiKey"},
        )
