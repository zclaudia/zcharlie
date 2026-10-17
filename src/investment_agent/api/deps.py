"""Shared HTTP-boundary pieces for the route modules: session-scoped service
dependencies and the one canonical ``InvestorError`` status table.

Before this module every router hand-rolled both:

* an ``async def service() -> AsyncIterator[XService]`` generator that opened a
  :func:`~investment_agent.storage.database.session_scope` and yielded the
  service built on it, and
* a status ternary over ``exc.detail.code``, whose branches disagreed between
  modules (only the portfolio module mapped ``writes_disabled``).

Both are converged here.  The wire contract is frozen: a given
``InvestorError.detail.code`` maps to exactly one status and the response body
is always ``exc.detail.model_dump(mode="json")`` (the ``BusinessError``
contract, including ``code``/``message``/``field_errors``/``retryable``/
``current_refs``).

Only :class:`~investment_agent.investor.foundations.InvestorError` crosses this
boundary here; the agent conversation dependency keeps its own, disjoint
exception taxonomy (``InvalidContextError``/``IdempotencyPayloadMismatchError``/
``ExecutionDeniedError``/``TaskBoundaryError``/``LookupError``) where it is
declared.

Known deviation kept on purpose: ``methods.offline_shadow`` answers only 422/409
(it predates this table and a replay ``not_found`` is a 409 there, not a 404), so
it still maps inline and is documented at its call site.
"""

from collections.abc import AsyncIterator, Callable
from typing import NoReturn

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.foundations import InvestorError
from investment_agent.storage.database import session_scope

INVESTOR_ERROR_FALLBACK_STATUS = 409
"""Status for every code not named in :data:`INVESTOR_ERROR_STATUS`.

``InvestorError.detail`` is a ``BusinessError`` whose ``code`` is a free-form
``str`` (``investment_agent.investor.common.BusinessError.code: Text``), not an
enum or ``Literal``, so the table can never be proven exhaustive by mypy and the
fallback is load-bearing.  409 is the historical default and the correct one for
the currently produced conflict/stale/authorization codes
(``revision_conflict``, ``stale_revision``, ``idempotency_conflict``,
``authorization_required``, ``invalid_action``, ``source_revoked``,
``proposal_consumed``/``proposal_expired``/``proposal_parameters_changed``):
"the request was well-formed but the stored state forbids it, retry after
reloading".  A new code that deserves 4xx input semantics must be added to the
table explicitly.
"""

INVESTOR_ERROR_STATUS: dict[str, int] = {
    # Union of every branch the route modules used to hand-write, so no call
    # site's status changes when it moves onto this table.
    "not_found": 404,
    "invalid_reference": 422,
    "writes_disabled": 403,
    "snapshot_required": 422,
    "account_capacity_exceeded": 422,
    "holding_presence_unknown": 422,
    "action_holding_mismatch": 422,
    "historical_manifest_unavailable": 422,
    "invalid_cursor": 422,
    "source_capacity_exceeded": 422,
    "invalid_action": 422,
    "revision_conflict": 409,
    "idempotency_conflict": 409,
    "unsupported_assessment_view": 422,
    "unsupported_subject": 422,
    "disclosure_authorization_required": 403,
    "disclosure_authorization_expired": 403,
    "disclosure_scope_forbidden": 403,
    "disclosure_polling_disabled": 403,
}
"""Canonical ``InvestorError.detail.code`` -> HTTP status mapping."""


def investor_error_status(code: str) -> int:
    """Status for *code*: the table, else :data:`INVESTOR_ERROR_FALLBACK_STATUS`."""

    return INVESTOR_ERROR_STATUS.get(code, INVESTOR_ERROR_FALLBACK_STATUS)


def raise_http_for_investor_error(exc: InvestorError) -> NoReturn:
    """Leave the API as ``HTTPException``; payload shape is fixed for all codes."""

    raise HTTPException(
        status_code=investor_error_status(exc.detail.code),
        detail=exc.detail.model_dump(mode="json"),
    ) from exc


def session_service[T](
    create: Callable[[AsyncSession], T],
) -> Callable[[], AsyncIterator[T]]:
    """Build a FastAPI dependency yielding ``create(session)`` per request.

    ``create`` is called with the session of one :func:`session_scope` (the
    dependency is registered with ``scope="function"``, so the transaction is
    per request, never shared).  Retained for the whole request, the generator
    also sees the exceptions FastAPI re-throws through the completed dependency
    when the *handler* using the yielded service raises; an ``InvestorError``
    from either side leaves via :func:`raise_http_for_investor_error`.
    """

    async def dependency() -> AsyncIterator[T]:
        try:
            async with session_scope() as session:
                yield create(session)
        except InvestorError as exc:
            raise_http_for_investor_error(exc)

    return dependency
