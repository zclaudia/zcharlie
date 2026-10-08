"""DB-backed runtime settings (migration 021, docs/product/settings-plan.md).

* ``app_setting`` - one row per overridden runtime key (``investment_agent.
  settings.registry``). A key without a row uses its registry default.
  Plain keys keep the value in ``value_json``; secret keys keep only the
  Fernet ``ciphertext`` and a ``secret_hint`` (last 4 characters) - a secret
  row whose ``ciphertext`` is ``NULL`` was cleared.
* ``app_setting_change`` - append-only change history; secret values are
  recorded as ``"***"`` + hint, never in clear.
* ``app_setting_meta`` - a single row (``id = 1``): ``revision`` is bumped by
  every write that changed something (long-running processes poll it), and
  ``imported_at`` marks the one-time import of env / ``app.yaml`` values.
"""

from datetime import datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from investment_agent.storage.models.base import Base

META_ROW_ID = 1


class AppSetting(Base):
    __tablename__ = "app_setting"

    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    ciphertext: Mapped[str | None] = mapped_column(Text, nullable=True)
    secret_hint: Mapped[str | None] = mapped_column(String(8), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_by: Mapped[str] = mapped_column(String(64), nullable=False)


class AppSettingChange(Base):
    __tablename__ = "app_setting_change"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    key: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    old_value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    new_value_json: Mapped[Any] = mapped_column(JSON, nullable=True)
    changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    actor: Mapped[str] = mapped_column(String(64), nullable=False)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)


class AppSettingMeta(Base):
    __tablename__ = "app_setting_meta"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    revision: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    imported_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
