"""Immutable disclosure metadata, original bytes and external-operation receipts."""

from typing import Any

from sqlalchemy import JSON, ForeignKey, LargeBinary, String, UniqueConstraint, event
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class DisclosureBlob(Base):
    __tablename__ = "disclosure_blob"
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    body: Mapped[bytes] = mapped_column(LargeBinary)


class DisclosureItem(Base):
    __tablename__ = "disclosure_item"
    __table_args__ = (UniqueConstraint("owner_id", "identity_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    listing_id: Mapped[int] = mapped_column(ForeignKey("listing.id"), index=True)
    identity_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class DisclosureOriginal(Base):
    __tablename__ = "disclosure_original"
    __table_args__ = (UniqueConstraint("owner_id", "item_id", "content_hash"),)
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32), index=True)
    item_id: Mapped[str] = mapped_column(ForeignKey("disclosure_item.id"), index=True)
    content_hash: Mapped[str] = mapped_column(ForeignKey("disclosure_blob.content_hash"))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


class DisclosureReceipt(Base):
    __tablename__ = "disclosure_receipt"
    __table_args__ = (UniqueConstraint("owner_id", "operation_id"),)
    operation_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    owner_id: Mapped[str] = mapped_column(String(32))
    request_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)


def immutable(*args: object) -> None:
    raise ValueError("disclosure records and original bytes are immutable")


for model in (DisclosureBlob, DisclosureItem, DisclosureOriginal, DisclosureReceipt):
    event.listen(model, "before_update", immutable)
    event.listen(model, "before_delete", immutable)
