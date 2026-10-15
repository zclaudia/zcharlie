"""GET /healthz — unauthenticated liveness probe.

``version`` is the git revision baked into the image at build time
(``APP_VERSION``, see the Dockerfile); ``dev`` for local runs. The deploy
script compares it with the revision it just built.
"""

import os

from fastapi import APIRouter

router = APIRouter(tags=["health"])


@router.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok", "version": os.environ.get("APP_VERSION") or "dev"}
