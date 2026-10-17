"""Storage ORM models: importing a module registers its tables on ``Base.metadata``.

Importing this package imports every model module (the 26 table-declaring
modules listed below plus :mod:`~investment_agent.storage.models.base`), so
this import list is the single source of truth for model registration.
``tests/conftest.py`` (``create_all``) and ``alembic/env.py`` (autogenerate)
import :class:`Base` from here and nothing else, so both automatically see
the same tables.  ``create_all`` and Alembic autogenerate only see the tables
whose modules have been imported: a new model module must be added to the
imports below exactly once, and it is then registered everywhere.
"""

from investment_agent.storage.models import (
    agent,
    analysis,
    app_settings,
    candidates,
    disclosure,
    discovery,
    earnings_events,
    evidence,
    investment_assessment,
    investor,
    market_data,
    material_acquisition,
    monitoring,
    ops,
    peer,
    pit_snapshot,
    portfolio,
    portfolio_plan,
    provider_call,
    publication,
    reference,
    relationship,
    research_material,
    rules,
    stock_directory,
    supplemental_rules,
    supplemental_updates,
    user_domain,
    verification,
    zones,
)
from investment_agent.storage.models.base import Base

__all__ = [
    "Base",
    "agent",
    "analysis",
    "app_settings",
    "candidates",
    "disclosure",
    "discovery",
    "earnings_events",
    "evidence",
    "investor",
    "investment_assessment",
    "market_data",
    "material_acquisition",
    "monitoring",
    "ops",
    "peer",
    "pit_snapshot",
    "portfolio",
    "portfolio_plan",
    "provider_call",
    "publication",
    "reference",
    "relationship",
    "research_material",
    "rules",
    "stock_directory",
    "supplemental_rules",
    "supplemental_updates",
    "user_domain",
    "verification",
    "zones",
]
