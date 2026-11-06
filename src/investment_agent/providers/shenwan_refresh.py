"""Fetch and validate the Shenwan 2021 directory (the refresh job's src core).

``tools/refresh_shenwan_directory.py`` used to own the fetch; the health
page's 刷新行业目录 job (config-docs-plan §3.2 / WP5) needs the same fetch
inside the worker, so the fetch-and-validate step moved here:

* :func:`fetch_shenwan_directory` - the official code table
  (``SwClassCode_2021.xls`` through :class:`AkshareShenwanClient`, one
  request, the retry / timeout semantics of the shared provider pipeline);
* :func:`validate_rendered_directory` - CD3-style validation: what a refresh
  will write must parse back through the runtime loader
  (:func:`parse_shenwan_directory`), so a site change that produces a
  directory the loader rejects fails the job before anything is written.

The CLI tool stays a thin wrapper over :func:`fetch_shenwan_directory` (plus
its cross-check and fixtures modes); the job writes only the runtime copy
under ``DATA_DIR`` (see
:func:`investment_agent.providers.shenwan_directory.runtime_shenwan_directory_path`).
"""

from __future__ import annotations

from typing import Protocol

import yaml

from investment_agent.providers.akshare_shenwan import AkshareShenwanClient
from investment_agent.providers.shenwan_directory import (
    ShenwanDirectory,
    dump_shenwan_directory,
    parse_shenwan_directory,
)


class DirectoryClient(Protocol):
    """The one call a refresh needs (an :class:`AkshareShenwanClient`)."""

    async def fetch_code_directory(self, *, version: str | None = None) -> ShenwanDirectory: ...


def validate_rendered_directory(text: str) -> ShenwanDirectory:
    """The loader is the validator (CD3): the rendered YAML must parse back."""

    return parse_shenwan_directory(yaml.safe_load(text))


async def fetch_shenwan_directory(
    client: DirectoryClient | None = None, *, version: str | None = None
) -> ShenwanDirectory:
    """Fetch the official code table and validate its rendered form.

    Raises the provider errors of
    :mod:`investment_agent.providers.akshare_shenwan`: transient network
    failures are retried inside the client (and again by the worker's retry
    policy), while a schema change or a round-trip the runtime loader rejects
    is terminal - the directory on disk is never replaced by one runtime code
    could not read back.
    """

    if client is None:
        client = AkshareShenwanClient()
    directory = await client.fetch_code_directory(version=version)
    validate_rendered_directory(dump_shenwan_directory(directory))
    return directory
