"""Shared Typer argument / option parsers for the CLI commands.

Only the genuinely identical copies live here: the ``--universe`` validator
and the ``YYYY-MM-DD`` option parser that ``ia screen`` and ``ia snapshot``
both carried. Semantically different look-alikes (e.g. ``ia verify``'s
``--period`` quarter-end check versus ``ia snapshot``'s ``YYYYMMDD``-tolerant
one) stay with their commands.
"""

from __future__ import annotations

from datetime import date

import typer

from investment_agent.clock import shanghai_today
from investment_agent.providers.baostock_reference import INDEX_CODES, IndexCode

UNIVERSE_HELP = f"Universe code: {', '.join(INDEX_CODES)}"
"""``--universe`` help text (also the bad-value message)."""


def universe_code(value: str) -> IndexCode:
    """An :data:`IndexCode` member, case-insensitive (``typer.BadParameter`` otherwise)."""

    code = value.strip().upper()
    if code not in INDEX_CODES:
        raise typer.BadParameter(UNIVERSE_HELP, param_hint="--universe")
    return code


def date_option(value: str | None, name: str = "--as-of") -> date:
    """An ISO date option; ``shanghai_today()`` when absent.

    ``name`` is the ``--option`` the error message points at (``--as-of``,
    ``--date``, ``--period``, ...).
    """

    if value is None:
        return shanghai_today()
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter("expected YYYY-MM-DD", param_hint=name) from exc
