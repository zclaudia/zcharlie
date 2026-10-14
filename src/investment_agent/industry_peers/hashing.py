"""The semantic-hash convention shared by the WP3 importer and the WP8 collector.

``SEMANTIC_HASH_SCHEMA`` tags the canonical serialization that the stored
``PeerAnnualSnapshot.semantic_hash`` unique key is derived from, and
:func:`canonical_json` is that serialization (sorted keys, no insignificant
whitespace, non-ASCII kept verbatim as UTF-8).

The two call sites build their payload from the same facts but deliberately
differ on exactly one point: the offline importer
(:func:`investment_agent.cli.commands.peer_annual._hashes`) excludes each
proof's ``checked_at``, while the online collector
(:func:`investment_agent.industry_peers.jobs.snapshot_hashes`) keeps
``dict(proofs)`` unchanged. That difference is already stored in the unique key
of existing rows, so the payload assembly stays at each call site and must not
be unified here - a re-import would otherwise append a new version instead of
matching the saved one. Only the schema tag and the serialization are shared.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

SEMANTIC_HASH_SCHEMA = "peer_annual_semantic_hash/1"
"""Schema tag inside the canonical payload of a peer annual semantic hash."""


def canonical_json(payload: Mapping[str, Any]) -> str:
    """The canonical semantic serialization: UTF-8 text, sorted keys, no spaces."""

    return json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
