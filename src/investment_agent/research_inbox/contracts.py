"""IT-001 business contracts over the shared EV event vocabulary.

These contracts describe a projection input, not proof that source rows exist
or permission to execute an action. Adapters must first use EV's registered
same-session resolver. No provider, arbitrary callback or delivery loop lives here.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Annotated, ClassVar, Literal, Protocol, Self
from uuid import UUID

from pydantic import Field, field_validator, model_validator

from investment_agent.investor.common import Contract, ObjectRef, OwnerId, Revision
from investment_agent.research_events.contracts import (
    AccountSubject as AccountSubject,
)
from investment_agent.research_events.contracts import (
    Code,
    EventType,
    ExactRef,
    NonnegativeInt,
    PlannedObjectRef,
    PositiveInt,
    SetArray,
    StoredEvent,
)
from investment_agent.research_events.contracts import (
    InboxRebuildState as InboxRebuildState,
)
from investment_agent.research_events.contracts import (
    ListingSubject as ListingSubject,
)
from investment_agent.research_events.contracts import (
    Subject as Subject,
)

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

INBOX_CONSUMER = "research_inbox.v1"
CONTRIBUTION_VERSION = 1
type EvidenceStatus = Literal[
    "current", "source_unavailable", "source_invalidated", "superseded", "historical"
]
type InboxAction = Literal[
    "read", "acknowledge", "snooze", "request_review", "view_decision", "view_check"
]
ACCOUNT_ACTIONS = frozenset({"read", "snooze", "view_decision", "view_check"})
_LINK = re.compile(
    r"(?i)(?:://|\b(?:https?|ftp|file|data|javascript|mailto):|www\.|\[[^\]]*\]\s*\(|(?:^|\s)/{1,2}[a-z0-9])"
)


class EventRef(Contract):
    event_id: UUID
    sequence: PositiveInt


class InboxContribution(Contract):
    """One local event contribution for one subject; no free-form payload bag."""

    canonical_set_fields: ClassVar[frozenset[str]] = frozenset({"exact_source_refs", "replaces_refs"})

    schema_version: Literal[1] = 1
    owner_id: OwnerId
    subject: Subject
    event_ref: EventRef
    exact_source_refs: Annotated[SetArray[ExactRef], Field(min_length=1)]
    materiality: Literal["attention", "context"]
    reason_code: Code
    evidence_status: EvidenceStatus
    display_text: Annotated[str, Field(min_length=1, max_length=300)]
    case_ref: ObjectRef | None = None
    episode: PositiveInt | None = None
    claim_ref: ObjectRef | None = None
    monitor_condition_ref: PlannedObjectRef | None = None
    topic_ref: PlannedObjectRef | None = None
    decision_ref: ObjectRef | None = None
    reviewed_relation_ref: ExactRef | None = None
    replaces_refs: SetArray[ExactRef] = ()
    # A bounded rendering hint used by the frozen account fixture, not authority.
    allowed_actions: Annotated[tuple[InboxAction, ...], Field(max_length=6)] | None = None

    @field_validator("schema_version", mode="before")
    @classmethod
    def strict_version(cls, value: object) -> object:
        if type(value) is not int:
            raise ValueError("Contribution schema version must be a strict integer")
        return value

    @field_validator("display_text")
    @classmethod
    def safe_summary(cls, value: str) -> str:
        if (
            not value.strip()
            or any(ord(char) < 32 or ord(char) == 127 or char in "<>" for char in value)
            or _LINK.search(value)
        ):
            raise ValueError(
                "Use a short plain server-authored summary without links or source excerpts"
            )
        return value

    @model_validator(mode="after")
    def relations_and_actions(self) -> Self:
        if (self.case_ref is None) != (self.episode is None):
            raise ValueError("Case reference and episode must be supplied together")
        if self.case_ref is not None and (
            self.case_ref.kind != "case" or self.case_ref.field_path is not None
        ):
            raise ValueError("Case requires its exact whole version")
        if isinstance(self.subject, AccountSubject):
            if self.case_ref is not None or self.episode is not None:
                raise ValueError("An account contribution cannot link a company case/episode")
            if (
                self.allowed_actions is not None
                and not set(self.allowed_actions) <= ACCOUNT_ACTIONS
            ):
                raise ValueError("Account cards cannot acknowledge or invoke company manual review")
        if self.claim_ref is not None and (
            self.claim_ref.kind not in {"thesis", "recommendation"} or not self.claim_ref.field_path
        ):
            raise ValueError(
                "Claim requires an exact thesis/recommendation parent and stable field path"
            )
        for ref, kind in (
            (self.topic_ref, "topic_revision"),
            (self.monitor_condition_ref, "topic_condition"),
            (self.decision_ref, "decision"),
        ):
            if ref is not None and (ref.kind != kind or ref.field_path is not None):
                raise ValueError(f"Expected an exact whole {kind} reference")
        if self.decision_ref is not None and self.decision_ref.revision != 1:
            raise ValueError("Immutable decision reference uses revision 1")
        if self.allowed_actions is not None and len(set(self.allowed_actions)) != len(
            self.allowed_actions
        ):
            raise ValueError("Action hints must be unique")
        return self


class InboxRevisions(Contract):
    """Row CAS, material content and read acknowledgment are independent."""

    revision: Revision
    content_revision: Revision
    read_content_revision: NonnegativeInt

    @model_validator(mode="after")
    def revision_order(self) -> Self:
        if not self.read_content_revision <= self.content_revision <= self.revision:
            raise ValueError("Expected 0 <= read_content_revision <= content_revision <= revision")
        return self

    @property
    def unread(self) -> bool:
        return self.read_content_revision < self.content_revision


class InboxProjectionHead(Contract):
    """Typed view of the existing EV projection_cursor row, not another head."""

    consumer: Literal["research_inbox.v1"] = "research_inbox.v1"
    scanned_sequence: NonnegativeInt
    revision: Revision
    active_generation: UUID | None
    rebuild_state: InboxRebuildState | None

    @model_validator(mode="after")
    def rebuild_generation(self) -> Self:
        state = self.rebuild_state
        if state is not None:
            if (
                self.active_generation is None
                or state.candidate_generation == self.active_generation
            ):
                raise ValueError(
                    "A rebuild requires a distinct candidate and existing active generation"
                )
            if state.state == "ready" and state.last_replayed_sequence != state.journal_watermark:
                raise ValueError("Ready requires the captured journal watermark to be replayed")
        return self


class AdapterSpecification(Contract):
    name: Code
    projector_version: Annotated[
        str, Field(min_length=1, max_length=80, pattern=r"^[a-zA-Z0-9._/-]+$")
    ]
    event_types: Annotated[tuple[EventType, ...], Field(min_length=1, max_length=13)]
    schema_versions: Annotated[tuple[Revision, ...], Field(min_length=1, max_length=1)] = (1,)

    @model_validator(mode="after")
    def supported(self) -> Self:
        if len(set(self.event_types)) != len(self.event_types) or self.schema_versions != (1,):
            raise ValueError("Unique registered event types and supported schema v1 are required")
        return self


class IgnoredContribution(Contract):
    state: Literal["ignored"] = "ignored"
    reason: Literal[
        "noise", "no_material_signal", "unsupported_optional_adapter", "control_suppressed"
    ]


class InboxAdapter(Protocol):
    """One bounded local projection; caller owns resolver, session and commit."""

    specification: AdapterSpecification

    async def adapt(
        self, session: AsyncSession, event: StoredEvent
    ) -> InboxContribution | IgnoredContribution: ...


def validate_adapter_output(
    event: StoredEvent, contribution: InboxContribution
) -> InboxContribution:
    """Bind the parsed output to the persisted event; not a source resolver.

    A reviewed_relation_ref is only an address. IT-005/021 must read its exact
    source hash before projecting a support/opposition relationship; this DTO
    intentionally has no client/adapter-controlled relation assertion field.
    """
    if contribution.owner_id != event.owner_id or contribution.subject != event.payload.subject:
        raise ValueError("Contribution owner/subject differs from its resolved event")
    if (
        contribution.event_ref.event_id != event.event_id
        or contribution.event_ref.sequence != event.sequence
    ):
        raise ValueError("Contribution must identify its exact persisted event and sequence")
    key = (event.source_ref.kind, event.source_ref.id, event.source_ref.revision)
    if not any(
        (ref.kind, ref.id, ref.revision) == key and getattr(ref, "field_path", None) is None
        for ref in contribution.exact_source_refs
    ):
        raise ValueError("The exact whole event source must remain in source references")
    return contribution
