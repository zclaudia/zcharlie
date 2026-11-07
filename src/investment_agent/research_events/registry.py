"""Explicit in-process source and consumer registrations, never user callbacks."""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from sqlalchemy.ext.asyncio import AsyncSession

from investment_agent.investor.common import OwnerId
from investment_agent.investor.foundations import InvestorError
from investment_agent.research_events.contracts import (
    EVENT_CONTRACTS,
    EventSourceRef,
    RoutingPayload,
    StoredEvent,
)

if TYPE_CHECKING:
    from investment_agent.research_events.resolvers import ResolvedSource


type SourceResolver = Callable[
    [AsyncSession, OwnerId, EventSourceRef, int, RoutingPayload], Awaitable["ResolvedSource"]
]
type ConsumerHandler = Callable[[AsyncSession, StoredEvent], Awaitable[object]]
DEFAULT_CONSUMERS = frozenset({"research_inbox.v1", "watchlist_sync.v1", "review_admission.v1"})


@dataclass(frozen=True)
class SourceRegistration:
    kind: str
    resolver: SourceResolver
    allowed_event_types: frozenset[str]
    schema_versions: frozenset[int]


@dataclass(frozen=True)
class ConsumerRegistration:
    name: str
    handler: ConsumerHandler
    event_types: frozenset[str]
    schema_versions: frozenset[int]


class EventRegistry:
    """Only reviewed code can extend the named whitelist at composition time."""

    def __init__(self, *, approved_consumers: frozenset[str] = DEFAULT_CONSUMERS) -> None:
        if any(not re.fullmatch(r"[a-z][a-z0-9_]*\.v[1-9][0-9]*", name)
               for name in approved_consumers):
            raise ValueError("consumer names must be stable versioned identifiers")
        self.approved_consumers = approved_consumers
        self._sources: dict[str, SourceRegistration] = {}
        self._consumers: dict[str, ConsumerRegistration] = {}

    @staticmethod
    def _schemas(values: tuple[int, ...]) -> frozenset[int]:
        if not values or any(type(value) is not int or value != 1 for value in values):
            raise ValueError("only the implemented schema version 1 may be registered")
        return frozenset(values)

    def register_source(self, kind: str, resolver: SourceResolver, *,
                        allowed_event_types: tuple[str, ...],
                        schema_versions: tuple[int, ...] = (1,)) -> None:
        events = frozenset(allowed_event_types)
        if not events or any(name not in EVENT_CONTRACTS or EVENT_CONTRACTS[name][0] != kind
                             for name in events):
            raise ValueError("source kind/event types are not an approved contract")
        if kind in self._sources:
            raise ValueError(f"source already registered: {kind}")
        self._sources[kind] = SourceRegistration(kind, resolver, events, self._schemas(schema_versions))

    def source(self, kind: str, event_type: str, schema_version: int = 1) -> SourceRegistration:
        registration = self._sources.get(kind)
        if (registration is None or event_type not in registration.allowed_event_types
                or type(schema_version) is not int or schema_version not in registration.schema_versions):
            raise InvestorError("invalid_reference", "Event source adapter is not installed for this contract")
        return registration

    def register_consumer(self, name: str, handler: ConsumerHandler, *,
                          event_types: tuple[str, ...],
                          schema_versions: tuple[int, ...] = (1,)) -> None:
        events = frozenset(event_types)
        if name not in self.approved_consumers or not events or not events <= EVENT_CONTRACTS.keys():
            raise ValueError("consumer or its event types have not been explicitly approved")
        if name in self._consumers:
            raise ValueError(f"consumer already registered: {name}")
        self._consumers[name] = ConsumerRegistration(name, handler, events, self._schemas(schema_versions))

    def consumer(self, name: str) -> ConsumerRegistration:
        registration = self._consumers.get(name)
        if registration is None:
            raise InvestorError("invalid_reference", "Consumer is not installed")
        return registration

    @property
    def source_kinds(self) -> tuple[str, ...]:
        return tuple(sorted(self._sources))

    @property
    def consumer_names(self) -> tuple[str, ...]:
        return tuple(sorted(self._consumers))


_default: EventRegistry | None = None


def get_default_registry() -> EventRegistry:
    global _default
    if _default is None:
        from investment_agent.research_events.resolvers import install_builtin_sources

        registry = EventRegistry()
        install_builtin_sources(registry)
        _default = registry
    return _default


def register_source(kind: str, resolver: SourceResolver, *, allowed_event_types: tuple[str, ...],
                    schema_versions: tuple[int, ...] = (1,)) -> None:
    get_default_registry().register_source(kind, resolver, allowed_event_types=allowed_event_types,
                                           schema_versions=schema_versions)


def register_consumer(name: str, handler: ConsumerHandler, *, event_types: tuple[str, ...],
                      schema_versions: tuple[int, ...] = (1,)) -> None:
    get_default_registry().register_consumer(name, handler, event_types=event_types,
                                             schema_versions=schema_versions)
