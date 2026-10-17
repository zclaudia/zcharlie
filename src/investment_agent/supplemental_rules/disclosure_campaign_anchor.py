"""A fail-closed sent high-water mark kept outside SQLite backups.

Operators must preserve this marker when restoring an older database. It is
fsynced before the enclosing sent transaction can commit or HTTP can start.
A rolled-back sent transaction can therefore block conservatively; this never
creates permission to retransmit. This is not a replacement for audit backups.
"""

import json
import os
import stat
from pathlib import Path
from typing import Any, cast
from uuid import UUID, uuid4

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.repositories.supplemental_updates import SupplementalUpdatesRepository
from investment_agent.supplemental_rules.update_contracts import SupplementalChangeReceipt


def _path(session: AsyncSession, campaign_id: UUID) -> Path | None:
    engine = cast(AsyncEngine, session.bind)
    database = engine.url.database if engine is not None else None
    if not database or database == ":memory:":
        # Real campaign runners reject in-memory databases; legacy unit tests
        # can still exercise the pure accounting contracts without files.
        return None
    return Path(database).resolve().parent / f".p4-08-{campaign_id}.sent-anchor.json"


def _read(path: Path, campaign_id: UUID) -> dict[str, Any]:
    try:
        info = path.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_mode & 0o077 or info.st_size > 8192):
            raise ValueError("invalid anchor file")
        value = json.loads(path.read_text())
        if (not isinstance(value, dict) or set(value) != {"version", "campaign_id", "sent_ref"}
            or value["version"] != 1 or value["campaign_id"] != str(campaign_id)):
            raise ValueError("invalid anchor identity")
        if value["sent_ref"] is not None and (
            not isinstance(value["sent_ref"], dict)
            or set(value["sent_ref"]) != {"sequence", "id", "payload_hash"}
            or type(value["sent_ref"]["sequence"]) is not int
        ):
            raise ValueError("invalid anchor receipt")
        return cast(dict[str, Any], value)
    except (OSError, ValueError, TypeError) as exc:
        raise InvestorError("invalid_reference", "campaign sent anchor missing/corrupt; no automatic restore") from exc


def _write(path: Path, payload: dict[str, Any]) -> None:
    temp = path.with_name(path.name + f".{uuid4()}.tmp")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(json.dumps(payload, sort_keys=True, separators=(",", ":")).encode())
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    except OSError as exc:
        raise InvestorError("capacity_blocked", "campaign sent anchor cannot be persisted") from exc
    finally:
        temp.unlink(missing_ok=True)


async def initialize_campaign_anchor(session: AsyncSession, campaign_id: UUID) -> None:
    path = _path(session, campaign_id)
    if path is None:
        return
    if path.exists() or path.is_symlink():
        await verify_campaign_anchor(session, campaign_id)
    else:
        _write(path, {"version": 1, "campaign_id": str(campaign_id), "sent_ref": None})


async def verify_campaign_anchor(session: AsyncSession, campaign_id: UUID) -> None:
    """Pure read: an older backup cannot erase a known sent attempt."""
    path = _path(session, campaign_id)
    if path is None:
        return
    ref = _read(path, campaign_id)["sent_ref"]
    if ref is None:
        return
    receipt = await SupplementalUpdatesRepository(session).get_change("local_owner", ref["sequence"])
    if (receipt is None or str(receipt.id) != ref["id"] or receipt.payload_hash != ref["payload_hash"]
        or receipt.payload.get("record_type") != "campaign_hop_v2"
        or receipt.payload.get("stage") != "sent"):
        raise InvestorError("invalid_reference", "database predates campaign sent anchor; reconcile without network")


async def advance_campaign_anchor(
    session: AsyncSession, campaign_id: UUID, receipt: SupplementalChangeReceipt,
) -> None:
    path = _path(session, campaign_id)
    if path is None:
        return
    await verify_campaign_anchor(session, campaign_id)
    current = _read(path, campaign_id)
    ref = {"sequence": receipt.sequence, "id": str(receipt.id), "payload_hash": receipt.payload_hash}
    previous = current["sent_ref"]
    if previous is not None and previous["sequence"] >= receipt.sequence:
        if previous != ref:
            raise InvestorError("invalid_reference", "campaign sent anchor cannot move backwards")
        return
    _write(path, {"version": 1, "campaign_id": str(campaign_id), "sent_ref": ref})
