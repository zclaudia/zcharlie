"""Content-versioned URLs for the web UI's static files.

Templates link ``/app/static/v/<version>/<path>`` (via the ``static_url``
template global). ``<version>`` fingerprints every file under ``static/`` (path,
size and mtime), so changing any file yields new URLs. Relative ES-module
imports (``import … from "./api.js"``) resolve inside the same versioned
prefix, which makes the whole module graph safe to cache as immutable — the
embedded Android console in particular re-fetches every asset through the
gateway on each navigation unless the response is cacheable.

A request whose version is not the current one (a page rendered before a
deploy) still gets the current file, but with ``no-cache`` so it can never be
pinned under a stale key. Unversioned ``/app/static/<path>`` keeps working and
stays ``no-cache`` (see ``SecurityHeadersMiddleware``).

The fingerprint (a stat walk, ~1ms) is memoised for one second: a page render
asks for a dozen URLs, yet editing JS in development still changes URLs
without a server restart.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
from pathlib import Path
from typing import Any, Final

from starlette.exceptions import HTTPException
from starlette.responses import Response
from starlette.staticfiles import StaticFiles

STATIC_DIR: Final = Path(__file__).resolve().parent / "static"
VERSIONED_PREFIX: Final = "/app/static/v"
IMMUTABLE_CACHE_CONTROL: Final = "public, max-age=31536000, immutable"
_MEMO_SECONDS: Final = 1.0

_memo: dict[Path, tuple[float, str]] = {}
_memo_lock = threading.Lock()


def static_version(directory: Path = STATIC_DIR) -> str:
    """Fingerprint of every file under ``directory`` (12 hex chars)."""

    now = time.monotonic()
    with _memo_lock:
        cached = _memo.get(directory)
        if cached is not None and now - cached[0] < _MEMO_SECONDS:
            return cached[1]
    version = _fingerprint(directory)
    with _memo_lock:
        _memo[directory] = (now, version)
    return version


def _fingerprint(directory: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(p for p in directory.rglob("*") if p.is_file()):
        stat = path.stat()
        digest.update(
            f"{path.relative_to(directory).as_posix()}\0{stat.st_size}\0{stat.st_mtime_ns}\n".encode()
        )
    return digest.hexdigest()[:12]


def static_url(path: str) -> str:
    """Versioned URL for a file under ``static/`` (e.g. ``"app.js"``)."""

    return f"{VERSIONED_PREFIX}/{static_version()}/{path.lstrip('/')}"


class VersionedStaticFiles(StaticFiles):
    """Serve ``<version>/<path>`` from ``static/``; immutable only when current."""

    def __init__(self, *, directory: Path = STATIC_DIR) -> None:
        super().__init__(directory=str(directory))
        self._root = directory

    async def get_response(self, path: str, scope: Any) -> Response:
        version, sep, rest = path.partition(os.sep)
        if not sep or not rest:
            raise HTTPException(status_code=404)
        response = await super().get_response(rest, scope)
        current = version == static_version(self._root)
        if current and response.status_code in (200, 304):
            response.headers["cache-control"] = IMMUTABLE_CACHE_CONTROL
        else:
            response.headers["cache-control"] = "no-cache"
        return response
