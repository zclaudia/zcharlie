"""Canonical P2 hashes; intentionally independent of P1's float normalizer."""

from __future__ import annotations

import hashlib
import json
import math
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from typing import Any
from uuid import UUID

from pydantic import BaseModel


def _json_value(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return _json_value(value.model_dump(mode="python", exclude_none=False))
    if isinstance(value, dict):
        return {str(key): _json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_json_value(item) for item in value]
    if isinstance(value, (set, frozenset)):
        return sorted((_json_value(item) for item in value), key=lambda item: canonical_json(item))
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError("non-finite decimal cannot be canonicalized")
        return {"$decimal": str(value)}
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("canonical timestamps must include a timezone")
        return {"$datetime": value.isoformat()}
    if isinstance(value, date):
        return {"$date": value.isoformat()}
    if isinstance(value, UUID):
        return str(value)
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite float cannot be canonicalized")
        # Preserve the exact binary source representation; callers must still
        # retain its quality/precision and may not treat it as a Decimal.
        return {"$binary_float_hex": value.hex()}
    return value


def canonical_json(value: Any) -> str:
    """Return deterministic UTF-8 JSON while keeping Decimal distinct from float."""

    return json.dumps(
        _json_value(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"),
        allow_nan=False,
    )


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def rule_content_hash(definition: BaseModel | dict[str, Any]) -> str:
    body = definition.model_dump(mode="python") if isinstance(definition, BaseModel) else dict(definition)
    body.pop("content_hash", None)
    body.pop("semantic_hash", None)
    return canonical_hash(body)


def rule_semantic_hash(definition: BaseModel | dict[str, Any]) -> str:
    body = definition.model_dump(mode="python") if isinstance(definition, BaseModel) else dict(definition)
    selector_keys = {"selector", "contexts", "rule_type", "definition", "dimensions", "effects", "severity", "materiality", "event_ref", "issue_key", "valid_from", "valid_until", "review_due_at", "release_requirements", "source_refs"}
    return canonical_hash({key: body.get(key) for key in sorted(selector_keys)})


def stable_series_key(owner_id: str, listing_id: int | str, rule_id: UUID | str, context: str) -> str:
    """Stable series identity; content revision/issue/evaluation purpose are excluded."""

    if not owner_id or not context:
        raise ValueError("owner_id and context are required for a series")
    return canonical_hash({"owner_id": owner_id, "listing_id": str(listing_id), "rule_id": str(rule_id), "context": context})


def mutation_request_hash(command: str, target: UUID | str | None, body: BaseModel | dict[str, Any]) -> str:
    """Bind an idempotency key to versioned command, path target and full typed body."""

    payload = body.model_dump(mode="python") if isinstance(body, BaseModel) else body
    return canonical_hash({"schema_version": 1, "command": command, "target": str(target) if target is not None else None, "body": payload})
