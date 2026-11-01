"""Authenticated user method proposals; no model approval or paid shadow route."""
from typing import Annotated, Any, Literal
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import Field, ValidationError, model_validator
from pydantic_ai.messages import ModelResponse, ToolCallPart
from pydantic_ai.models.function import FunctionModel
from sqlalchemy import select

from investment_agent.agent.tasks import COMPLETED_TASK_STATUSES
from investment_agent.api.auth import get_bootstrap_settings, verify_api_key_or_session
from investment_agent.api.deps import session_service
from investment_agent.config import Settings
from investment_agent.investor.common import Contract, ObjectRef, Text
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.method_packages import MethodPackageDefinition
from investment_agent.investor.methods import (
    CandidateShadowBatchRequest,
    CandidateShadowItem,
    CandidateShadowRequest,
    CandidateShadowResultRequest,
    CreateProposal,
    EvaluateProposal,
    MethodConfig,
    MethodDecision,
    MethodRollback,
    MethodService,
    ProspectiveRequest,
    ShadowRequest,
    run_offline_shadow,
)
from investment_agent.investor.publication import FIXED_METHOD
from investment_agent.storage.database import session_scope
from investment_agent.storage.models.agent import AgentTask
from investment_agent.storage.models.investor import MethodArtifact, MethodHead, MethodProposal
from investment_agent.storage.models.reference import Company, Listing

router = APIRouter(prefix="/investor/methods", tags=["methods"], dependencies=[Depends(verify_api_key_or_session)])


service = session_service(MethodService)
Service = Annotated[MethodService, Depends(service, scope="function")]
BootstrapSettings = Annotated[Settings, Depends(get_bootstrap_settings)]


def _proposal_summary(svc: MethodService, row: MethodProposal) -> dict[str, Any]:
    """List summary with the package key surfaced for package proposals (§9)."""
    summary = svc.summary(row)
    package = summary["proposal"]["request"].get("package")
    if package is not None:
        summary["proposal"]["package_key"] = package["package_key"]
    return summary


@router.get("")
async def list_methods(svc: Service, offset: Annotated[int, Query(ge=0)] = 0, limit: Annotated[int, Query(ge=1, le=20)] = 20) -> dict[str, Any]:
    proposals = (await svc.session.scalars(select(MethodProposal).where(MethodProposal.owner_id == svc.owner).order_by(MethodProposal.payload["created_at"].as_string().desc(), MethodProposal.id.desc()).offset(offset).limit(limit + 1))).all()
    reviews = (await svc.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == svc.owner, MethodArtifact.kind == "process_review").order_by(MethodArtifact.payload["created_at"].as_string().desc(), MethodArtifact.id.desc()).offset(offset).limit(limit + 1))).all()
    versions = (await svc.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == svc.owner, MethodArtifact.kind == "version").order_by(MethodArtifact.payload["created_at"].as_string().desc(), MethodArtifact.id.desc()).offset(offset).limit(limit + 1))).all()
    heads = (await svc.session.scalars(select(MethodHead).where(MethodHead.owner_id == svc.owner).order_by(MethodHead.revision.desc(), MethodHead.listing_id.desc()).offset(offset).limit(limit + 1))).all()
    tasks = (await svc.session.scalars(select(AgentTask).where(AgentTask.owner_id == svc.owner, AgentTask.status.in_(COMPLETED_TASK_STATUSES)).order_by(AgentTask.payload["created_at"].as_string().desc(), AgentTask.id.desc()).offset(offset).limit(limit + 1))).all()
    more = any(len(rows) > limit for rows in (proposals, reviews, versions, heads, tasks))
    proposals, reviews, versions, heads, tasks = proposals[:limit], reviews[:limit], versions[:limit], heads[:limit], tasks[:limit]
    listing_ids = {p.payload["request"]["listing_ref"]["id"] for p in proposals} | {h.listing_id for h in heads}
    names = (await svc.session.execute(select(Listing.id, Listing.ticker, Company.canonical_name).join(Company, Listing.company_id == Company.id).where(Listing.id.in_(listing_ids)))).all()
    default_revision, default_binding = await svc.default_head()
    default_head = {"revision": default_revision, "binding": default_binding.model_dump(mode="json")} if default_binding is not None else None
    return {"scope_names": {str(identifier): name + " · " + ticker for identifier, ticker, name in names}, "baseline_method": FIXED_METHOD.model_dump(mode="json"), "proposals": [_proposal_summary(svc, p) for p in proposals], "reviews": [svc.review_summary(r.payload) for r in reviews], "versions": [{"id": r.id, "binding": r.payload["binding"]} for r in versions], "heads": [{"revision": h.revision, "binding": h.payload} for h in heads], "default_head": default_head, "completed_tasks": [{"id": t.id, "status": t.status, "created_at": t.payload["created_at"], "base_refs": t.payload["binding"]["base_refs"][:16]} for t in tasks], "page": {"offset": offset, "limit": limit, "truncated": more, "next_offset": offset + limit if more else None, "ordering": "Newest task/proposal/review first; offsets may shift after writes; refresh to latest"}, "limits": "One listing only; offline replays cannot authorize activation. Prospective gate-shadow observes new actual outputs; no semantic quality acceptance. Existing Tasks keep frozen methods. Comparison/portfolio use baseline."}


@router.get("/proposals/{proposal_id}")
async def proposal_detail(proposal_id: UUID, svc: Service) -> dict[str, Any]:
    import json

    row = await svc.proposal(proposal_id)
    payload = await svc.evidence(row)
    if row.payload["request"].get("package_ref") is not None:
        # §9 workbench display for package proposals (card 17): read-only
        # projections over the same frozen artifacts evidence() binds. They are
        # attached after evidence_hash, so approval fencing (decide recomputing
        # evidence()) is untouched, and config-only detail keeps its exact shape.
        payload["candidate"] = await _candidate_display(svc, row)
        payload["structure_diff"] = await _structure_diff(svc, row)
        payload["replay"] = _replay_display(row, payload["evaluations"][-1] if payload["evaluations"] else None)
        payload["candidate_shadows"] = await _candidate_shadows_display(svc, row)
        payload["candidate_shadow_claims"] = await _candidate_claims_display(svc, row)
    if len(json.dumps(payload, ensure_ascii=False)) > 2_000_000:
        raise HTTPException(413, "Artifact exceeds bounded response; use exact proposal evidence summary and immutable hash")
    return payload


WORKBENCH_ITEM_LIMIT = 24
"""§9 display bound: each diff group and the shadow list show at most this many
entries, mirroring the bounded excerpt conventions of the evidence summaries."""

WORKBENCH_TEXT_LIMIT = 2000
"""The same bounded excerpt length the review summaries use."""

WORKBENCH_SHADOW_LIMIT = 20
"""Registered candidate shadows shown; the approval gate reads them all, the
display shows the newest tail when a proposal accumulates more."""


def _bounded_entries(values: list[str]) -> dict[str, Any]:
    entries = values[:WORKBENCH_ITEM_LIMIT]
    return {"items": entries, "total": len(values), "truncated": len(entries) < len(values)}


async def _candidate_display(svc: MethodService, row: MethodProposal) -> dict[str, Any]:
    """§9 ``candidate`` block: the proposal-frozen package identity (card 17).

    Read from the stored package artifact behind the frozen ref - never from
    the request copy - with the content hash excerpted to its first 8 chars.
    """
    stored = await svc.package_by_ref(ObjectRef.model_validate(row.payload["request"]["package_ref"]))
    return {"package_key": stored["package_key"], "version": stored["version"], "content_hash": stored["content_hash"][:8], "discipline": stored["discipline"]}


_STRUCTURE_DIFF_GROUPS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("new_blocking_steps", ("blocking:",)),
    ("outputs", ("outputs:",)),
    ("dimension_minimums", ("dimension_min:",)),
    ("depends_on", ("depends_on:",)),
    ("discipline", ("discipline:",)),
    ("applicable_kinds", ("kinds:",)),
    ("gap_tightenings", ("allow_missing:", "dimension_allow_missing:", "dimension_allow_not_applicable:")),
)


async def _structure_diff(svc: MethodService, row: MethodProposal) -> dict[str, Any]:
    """§9 ``structure_diff`` block: old -> candidate structural compare (card 17).

    Generated from the card-11 strengthening ids (``_strengthened_requirements``
    resolves the old baseline exactly as ``evaluate`` judged it), so the display
    can never diverge from what the evaluation enforced: new blocking steps and
    their ids, output tightenings, raised dimension minimum statuses, discipline
    switches and gap-allowance tightenings. Added dependency edges are read from
    both definitions under §8's dependency rule - the card-11 id set only covers
    edges added to steps the old package already had, never a new step's own
    edges. All ids stay in the §8 vocabulary shared with
    ``requires_candidate_shadow`` for cross-referencing; the joined text is
    bounded like every other excerpt.
    """
    from investment_agent.investor.method_packages import (
        MethodPackage,
        MethodPackageDefinition,
        legacy_package_for,
    )

    request = row.payload["request"]
    candidate = MethodPackage.model_validate(await svc.package_by_ref(ObjectRef.model_validate(request["package_ref"])))
    ids = list(await svc._strengthened_requirements(row, candidate))
    old = await svc.known(ObjectRef.model_validate(request["old_method"]))
    old_definition: MethodPackageDefinition
    if request.get("old_package_kind") == "package":
        # A v2 method version: the stored package artifact is the baseline.
        assert old.package_ref is not None
        old_definition = MethodPackage.model_validate(await svc.package_by_ref(old.package_ref))
    else:
        # FIXED_METHOD or a v1 method version: the virtual legacy baseline.
        old_definition = legacy_package_for(old.config)
    old_edges = {(step.id, dependency) for step in old_definition.steps for dependency in step.depends_on}
    grouped: dict[str, list[str]] = {name: [] for name, _ in _STRUCTURE_DIFF_GROUPS}
    for identifier in ids:
        for name, prefixes in _STRUCTURE_DIFF_GROUPS:
            if identifier.startswith(prefixes):
                grouped[name].append(identifier)
                break
    grouped["depends_on"] = sorted(f"depends_on:{step.id}:{dependency}" for step in candidate.steps for dependency in dict.fromkeys(step.depends_on) if (step.id, dependency) not in old_edges)
    diff: dict[str, Any] = {"old_package_kind": request.get("old_package_kind")}
    for name, _ in _STRUCTURE_DIFF_GROUPS:
        diff[name] = _bounded_entries(grouped[name])
    diff["enhancement_ids"] = _bounded_entries(ids)
    diff["text"] = "; ".join(identifier for name, _ in _STRUCTURE_DIFF_GROUPS for identifier in grouped[name])[:WORKBENCH_TEXT_LIMIT]
    return diff


def _replay_display(row: MethodProposal, evaluation: dict[str, Any] | None) -> dict[str, Any]:
    """§9 ``replay`` block: per-sample three-state verdicts (card 17).

    One compact entry per latest-evaluation sample: the aggregate
    ``pass | block | not_replayable`` state and that sample's not-replayable
    requirement ids; out-of-scope samples keep their baseline check path, so
    their state is None. The cumulative ``requires_candidate_shadow`` set comes
    from the proposal payload. No frozen review or raw artifact is copied.
    """
    samples = []
    for case in (evaluation or {}).get("cases", []):
        proposed = case.get("proposed", {})
        replayed = proposed.get("replayed", {})
        samples.append({
            "review_id": case.get("review_id"),
            "role": case.get("role"),
            "applicable": case.get("applicable"),
            "state": proposed.get("state"),
            "not_replayable": sorted(key for key, value in replayed.items() if value == "not_replayable"),
        })
    return {"samples": samples, "requires_candidate_shadow": sorted(row.payload.get("requires_candidate_shadow", []))}


async def _candidate_shadows_display(svc: MethodService, row: MethodProposal) -> dict[str, Any]:
    """§9 ``candidate_shadows`` block: registered prospective shadows (card 17).

    Exactly the card-15 ``candidate_shadow`` artifacts the approval coverage
    gate reads, projected to task id, expectation, outcome and covered
    requirement ids - never the raw artifacts.
    """
    identifiers = [str(item) for item in row.payload.get("candidate_shadow_ids", [])]
    shown = identifiers[-WORKBENCH_SHADOW_LIMIT:]
    shadows = []
    for identifier in shown:
        record = await svc.artifact(UUID(identifier), "candidate_shadow")
        shadows.append({"id": identifier, "task_id": record["task_id"], "expected": record["expected"], "outcome": record["outcome"], "requirement_ids": record["requirement_ids"]})
    return {"items": shadows, "total": len(identifiers), "truncated": len(shown) < len(identifiers)}


async def _candidate_claims_display(svc: MethodService, row: MethodProposal) -> dict[str, Any]:
    """§9 ``candidate_shadow_claims`` block: started shadow claims (card 20).

    Every ``candidate_shadow_claim`` artifact this proposal started (single or
    batch initiation), newest ``WORKBENCH_SHADOW_LIMIT`` shown, each projected
    to its task id, expectation, truncated prompt and whether the result has
    already been registered - the queue view between initiation and
    registration. Config-only proposals never carry this block.
    """
    claims = (await svc.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == svc.owner, MethodArtifact.kind == "candidate_shadow_claim"))).all()
    mine = sorted((item.payload for item in claims if item.payload["proposal_id"] == row.id), key=lambda payload: (payload["created_at"], payload["id"]))
    registered: set[str] = set()
    for identifier in row.payload.get("candidate_shadow_ids", []):
        registered.add((await svc.artifact(UUID(str(identifier)), "candidate_shadow"))["task_id"])
    shown = mine[-WORKBENCH_SHADOW_LIMIT:]
    items = [{"task_id": claim["task_id"], "expected": claim["expected"], "prompt": claim["prompt"][:200], "created_at": claim["created_at"], "registered": claim["task_id"] in registered} for claim in shown]
    return {"items": items, "total": len(mine), "truncated": len(shown) < len(mine)}


@router.get("/artifacts/{kind}/{artifact_id}")
async def method_artifact(kind: Literal["process_review", "evaluation", "shadow", "prospective_shadow", "version", "event", "proposal_version", "shadow_claim", "shadow_result", "candidate_shadow", "candidate_shadow_claim"], artifact_id: UUID, svc: Service) -> dict[str, Any]:
    import json
    payload = await svc.artifact(artifact_id, kind)
    if len(json.dumps(payload, ensure_ascii=False)) > 2_000_000:
        raise HTTPException(413, "Artifact exceeds bounded response; use exact proposal evidence summary and immutable hash")
    return payload


@router.post("/reviews/{task_id}")
async def freeze_review(task_id: UUID, svc: Service) -> dict[str, Any]:
    return await svc.process_review(task_id)


class CreateProposalBody(Contract):
    """Route-level proposal body (card 20 §3): the ``CreateProposal`` fields
    plus the optional ``package_yaml`` entry.

    ``config`` stays mandatory on the existing wire paths (config-only and
    direct-JSON package requests keep their exact behavior) and may be omitted
    only together with ``package_yaml``, whose parsed candidate then fills it
    from the definition's ``discipline``; a given ``config`` passes through
    unchanged and the service layer's config≡discipline check applies.
    """

    mutation_id: UUID
    review_id: UUID
    listing_ref: ObjectRef
    old_method: ObjectRef
    error_pattern: Text = Field(max_length=1000)
    summary: Text = Field(max_length=1000)
    config: MethodConfig | None = None
    package: MethodPackageDefinition | None = None
    package_yaml: Text | None = None

    @model_validator(mode="after")
    def yaml_entry_shape(self) -> "CreateProposalBody":
        if self.package_yaml is not None and self.package is not None:
            raise ValueError("package_yaml 与 package 不能同时提供")
        if self.package_yaml is None and self.config is None:
            raise ValueError("config 必填；package_yaml 请求可省略，由候选包 discipline 填充")
        return self


@router.post("/proposals")
async def create_proposal(body: CreateProposalBody, svc: Service) -> dict[str, Any]:
    import yaml

    payload = body.model_dump(mode="json", exclude={"package_yaml"})
    if body.package_yaml is not None:
        # Card 20 §3: the YAML candidate replaces the JSON package; config may
        # be filled from the parsed discipline. Parse failures carry the
        # offline-shadow invalid_reference convention (422 via the deps table).
        try:
            parsed = yaml.safe_load(body.package_yaml)
        except yaml.YAMLError as exc:
            raise InvestorError("invalid_reference", "候选包 YAML 无法解析") from exc
        if not isinstance(parsed, dict):
            raise InvestorError("invalid_reference", "候选包 YAML 无法解析")
        payload["package"] = parsed
        if body.config is None:
            payload["config"] = parsed.get("discipline") or {}
    try:
        request = CreateProposal.model_validate(payload)
    except ValidationError as exc:
        raise InvestorError("invalid_reference", "候选包 YAML 不符合方法包定义") from exc
    return await svc.create(request)


@router.post("/evaluations")
async def evaluate(body: EvaluateProposal, svc: Service) -> dict[str, Any]:
    return await svc.evaluate(body)


@router.post("/decisions")
async def decision(body: MethodDecision, svc: Service, settings: BootstrapSettings) -> dict[str, Any]:
    # Card 19: the bootstrap Settings carry the candidate-shadow operations
    # defaults into the approval gate.
    return await svc.decide(body, actor="authenticated_user", base=settings)


@router.post("/rollback")
async def rollback(body: MethodRollback, svc: Service) -> dict[str, Any]:
    return await svc.rollback(body, actor="authenticated_user")


def _frozen_progress_replay(messages: Any, info: Any) -> ModelResponse:
    """Actual native model boundary, exact saved progress. No user/model
    supplied success flag is accepted; controller recomputes every gate.

    A saved history that lost its shape (no user prompt, unparsable JSON,
    missing frozen progress) is a domain error, never a 500: the model call
    must not crash the API boundary on archive drift.
    """
    import json

    try:
        prompt = next(part.content for message in reversed(messages) for part in message.parts if getattr(part, "part_kind", None) == "user-prompt")
        progress = json.loads(prompt)["frozen_progress"]
    except (StopIteration, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise InvestorError("invalid_reference", "保存的任务进度缺失或损坏，无法离线重放") from exc
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, progress)])


def _blocking_step_ids(rendered: str) -> list[str]:
    """Blocking step ids read back from the fixed renderer text (§6.1).

    The renderer is versioned fixed server text, so parsing it back is
    deterministic; the route replay has no structured package, only the
    prompt's ``candidate_rendered_method``.
    """
    steps: list[str] = []
    identifier, blocking = "", False
    for line in rendered.splitlines():
        if line.startswith("Step ") and ": " in line:
            identifier, blocking = line[len("Step "):].split(" (", 1)[0], False
        elif line == "- This step is blocking.":
            blocking = True
        elif line == "- This step is non-blocking.":
            blocking = False
        elif line.startswith("- Depends on") and identifier:
            if blocking:
                steps.append(identifier)
            identifier, blocking = "", False
    return steps


def _candidate_progress_replay(messages: Any, info: Any) -> ModelResponse:
    """Production replay model for package-proposal offline shadows (§7.2).

    Deterministic best-effort candidate metadata over the frozen sample only:
    each blocking step read from the fixed renderer text is planned once
    against the first evidence step and settled with one satisfied result
    citing the real frozen action ids, frozen counter refs and hypothesis
    indices. Deterministic code cannot hallucinate an identity, and
    ``run_offline_shadow``'s enforcing replay boundary independently rebuilds
    the frozen content and intercepts any citation beyond the frozen sample.
    """
    import json

    try:
        prompt = next(part.content for message in reversed(messages) for part in message.parts if getattr(part, "part_kind", None) == "user-prompt")
        payload = json.loads(prompt)
        progress, actions = payload["frozen_progress"], payload.get("frozen_actions", ())
        rendered = payload["candidate_rendered_method"]
    except (StopIteration, json.JSONDecodeError, KeyError, TypeError, ValueError, AttributeError) as exc:
        raise InvestorError("invalid_reference", "保存的任务进度缺失或损坏，无法离线重放") from exc
    blocking_steps = _blocking_step_ids(rendered) if progress.get("evidence_steps") else []
    metadata = {
        "plan_bindings": [{"evidence_step_index": 0, "method_step_id": step} for step in blocking_steps],
        "step_results": [{
            "method_step_id": step,
            "evidence_step_indices": (0,),
            "status": "satisfied",
            "action_ids": tuple(action["id"] for action in actions),
            "output_refs": tuple(ref for ref in progress.get("counter_refs", ()) if ref.get("kind") == "evidence"),
            "hypothesis_indices": tuple(range(len(progress.get("hypotheses", ())))),
        } for step in blocking_steps],
    }
    return ModelResponse(parts=[ToolCallPart(info.output_tools[0].name, {**progress, **metadata})])


def _offline_progress_replay(messages: Any, info: Any) -> ModelResponse:
    """Offline shadow replay dispatch (§7.2): package-proposal prompts carry
    ``candidate_rendered_method`` and replay the candidate metadata; every
    other prompt keeps the exact frozen-progress echo, so config-only shadows
    behave unchanged."""
    import json

    try:
        prompt = next(part.content for message in reversed(messages) for part in message.parts if getattr(part, "part_kind", None) == "user-prompt")
        candidate = "candidate_rendered_method" in json.loads(prompt)
    except (StopIteration, json.JSONDecodeError, TypeError, ValueError, AttributeError):
        candidate = False
    if not candidate:
        return _frozen_progress_replay(messages, info)
    return _candidate_progress_replay(messages, info)


@router.post("/offline-shadow")
async def offline_shadow(body: ShadowRequest) -> dict[str, Any]:
    try:
        return await run_offline_shadow(session_scope, body, FunctionModel(_offline_progress_replay))
    except InvestorError as exc:
        # Deliberately NOT the canonical deps table: this endpoint has always
        # answered 422/409 only (a not_found replay id is a conflict-like 409
        # here, not the canonical 404). Frozen wire contract; see deps.py.
        raise HTTPException(422 if exc.detail.code == "invalid_reference" else 409, detail=exc.detail.model_dump(mode="json")) from exc


@router.post("/prospective-shadow")
async def prospective_shadow(body: ProspectiveRequest, svc: Service) -> dict[str, Any]:
    return await svc.prospective(body)


class CandidateShadowBody(Contract):
    """§7.3 wire body; the proposal id comes from the path."""

    mutation_id: UUID
    prompt: Annotated[str, Field(min_length=1, max_length=2000)]
    expected: Literal["pass", "block"]


@router.post("/proposals/{proposal_id}/candidate-shadow")
async def candidate_shadow(proposal_id: UUID, body: CandidateShadowBody, svc: Service, settings: BootstrapSettings) -> dict[str, Any]:
    from investment_agent.agent.execution import ExecutionDeniedError
    request = CandidateShadowRequest(proposal_id=proposal_id, **body.model_dump(mode="json"))
    try:
        return await svc.start_candidate_shadow(request, base=settings)
    except ExecutionDeniedError as exc:
        # Admission boundaries carry safe codes as their message only.
        raise HTTPException(409, detail={"code": "candidate_shadow_denied", "message": str(exc)}) from exc


class CandidateShadowBatchBody(Contract):
    """§9/card 20 wire body for the batch initiation; the proposal id comes
    from the path, one mutation covers the whole batch."""

    mutation_id: UUID
    items: Annotated[tuple[CandidateShadowItem, ...], Field(min_length=1, max_length=4)]


@router.post("/proposals/{proposal_id}/candidate-shadow-batch")
async def candidate_shadow_batch(proposal_id: UUID, body: CandidateShadowBatchBody, svc: Service, settings: BootstrapSettings) -> dict[str, Any]:
    from investment_agent.agent.execution import ExecutionDeniedError
    request = CandidateShadowBatchRequest(proposal_id=proposal_id, **body.model_dump(mode="json"))
    try:
        return await svc.start_candidate_shadows(request, base=settings)
    except ExecutionDeniedError as exc:
        # Same admission-boundary convention as the single initiation.
        raise HTTPException(409, detail={"code": "candidate_shadow_denied", "message": str(exc)}) from exc


@router.post("/candidate-shadow-results")
async def candidate_shadow_result(body: CandidateShadowResultRequest, svc: Service) -> dict[str, Any]:
    return await svc.candidate_shadow_result(body)


@router.get("/task-bindings/{task_id}")
async def frozen_task_method(task_id: UUID, svc: Service) -> dict[str, Any]:
    from investment_agent.agent.tasks import TaskService
    from investment_agent.investor.methods import task_method
    try:
        await TaskService(svc.session).get_task(task_id)
    except LookupError as exc:
        raise InvestorError("not_found", "Task unavailable") from exc
    return (await task_method(svc.session, task_id)).model_dump(mode="json")


@router.get("/proposals/{proposal_id}/history")
async def proposal_history(proposal_id: UUID, svc: Service, offset: Annotated[int, Query(ge=0)] = 0) -> dict[str, Any]:
    from investment_agent.agent.tasks import fingerprint
    await svc.proposal(proposal_id)
    rows = (await svc.session.scalars(select(MethodArtifact).where(MethodArtifact.owner_id == svc.owner, MethodArtifact.payload["proposal_id"].as_string() == str(proposal_id)).order_by(MethodArtifact.payload["created_at"].as_string().desc(), MethodArtifact.id.desc()).offset(offset).limit(21))).all()
    return {"items": [{"id": r.id, "kind": r.kind, "created_at": r.payload.get("created_at"), "hash": fingerprint(r.payload)} for r in rows[:20]], "next_offset": offset + 20 if len(rows) > 20 else None}
