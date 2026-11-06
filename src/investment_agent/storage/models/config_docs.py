"""Config documents (migration 057, docs/product/config-docs-plan.md §4.2).

* ``config_document`` - one row per registered kind (``investment_agent.
  config_docs.registry``): the whole document as ``value_json``. ``imported_at``
  marks the one-time import of the baseline file; afterwards the row is the
  source of truth and the file is only a drift reference (CD5).
* ``config_document_change`` - append-only full-document history: every write
  records the complete old and new value plus actor and the mandatory note
  (CD7). The number of rows per ``kind`` is that kind's revision (optimistic
  locking); every write also bumps ``app_setting_meta.revision`` so the
  worker / scheduler propagation picks it up unchanged (CD4).
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base


class ConfigDocument(Base):
    __tablename__ = "config_document"

    kind: Mapped[str] = mapped_column(String(32), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ConfigDocumentChange(Base):
    __tablename__ = "config_document_change"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    old_value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    new_value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str] = mapped_column(Text, nullable=False)
