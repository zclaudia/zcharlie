"""``ia methods``: validate package files, sync them into the store, inspect, activate.

Packages are data (design §5): ``sync`` only imports append-only artifacts and
never moves any head; turning a package into the global default is a separate
explicit user operation (``activate-global``), which appends the method version
and audit event and atomically moves ``MethodDefaultHead`` only. This CLI is
the local operator path with the same ``authenticated_user`` actor as the
service layer's decide/rollback callers - no public write endpoint is added.
"""

from __future__ import annotations

import os
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import NAMESPACE_URL, UUID, uuid5

import typer
import yaml
from rich import print as rprint
from rich.markup import escape
from rich.table import Table
from sqlalchemy import select

from investment_agent.cli.runtime import bootstrap_settings
from investment_agent.investor.foundations import InvestorError
from investment_agent.investor.method_packages import (
    DEFAULT_METHODS_DIR,
    MethodPackageError,
    canonical_hash,
    load_package_file,
    load_packages,
)
from investment_agent.investor.methods import MethodBinding, MethodService

app = typer.Typer(help="Method packages (validate, sync, list, show, status, activate-global)")

_DIR_HELP = "Package directory (default: config/methods)"
_PROJECT_DIR_HELP = (
    "Change into this directory first (the checkout): the relative --dir default "
    "config/methods, .env and a relative DATABASE_URL are resolved from the working directory"
)
DEFAULT_CHANNEL = "company-research"
"""First version ships a single global default channel (design §5)."""


def _run[T](main: Callable[[], Awaitable[T]]) -> T:
    """Run ``main`` with the database engine opened and disposed on its loop."""

    from investment_agent.storage.database import run_with_engine

    return run_with_engine(bootstrap_settings().database_url, main)


def _report(exc: MethodPackageError) -> None:
    typer.echo(f"Invalid {exc.source or ''}: {len(exc.errors)} issue(s)")
    for issue in exc.errors:
        typer.echo(f"  {issue}")


@app.callback()
def enter_project(
    project_dir: Path | None = typer.Option(  # noqa: B008
        None, "--project-dir", help=_PROJECT_DIR_HELP, exists=True, file_okay=False
    ),
) -> None:
    """Shared entry: resolve relative paths (config/methods, .env) against one directory."""

    if project_dir is not None:
        os.chdir(project_dir)


@app.command("validate")
def validate(
    file: Path = typer.Argument(..., exists=True, dir_okay=False, help="Package definition file"),  # noqa: B008
) -> None:
    """Validate one package definition file (offline); every problem reported at once."""

    try:
        definition = load_package_file(file)
    except MethodPackageError as exc:
        _report(exc)
        raise typer.Exit(code=1) from exc
    rprint(f"[green]Valid package[/green] {definition.package_key} hash={canonical_hash(definition)}")


@app.command("sync")
def sync(
    directory: Path = typer.Option(DEFAULT_METHODS_DIR, "--dir", help=_DIR_HELP),  # noqa: B008
) -> None:
    """Validate every package file, then import them as append-only artifacts.

    Identical content deduplicates (idempotent re-runs); one invalid file fails
    the whole sync before anything is stored, and no method head ever moves.
    """

    try:
        packages = load_packages(directory)
    except MethodPackageError as exc:
        _report(exc)
        raise typer.Exit(code=1) from exc

    async def run() -> None:
        from investment_agent.storage.database import session_scope
        from investment_agent.storage.models.investor import MethodArtifact

        async with session_scope() as session:
            svc = MethodService(session)
            existing: dict[tuple[str, str], dict[str, Any]] = {}
            rows = (
                await session.scalars(
                    select(MethodArtifact).where(
                        MethodArtifact.owner_id == svc.owner, MethodArtifact.kind == "package"
                    )
                )
            ).all()
            for row in rows:
                existing[(row.payload["package_key"], row.payload["content_hash"])] = row.payload
            outcomes: list[tuple[str, int, str, str]] = []
            for definition in packages:
                fingerprint = (definition.package_key, canonical_hash(definition))
                saved = existing.get(fingerprint)
                action = "unchanged"
                if saved is None:
                    saved = await svc.save_package(definition, source="repo-sync")
                    action = "created"
                existing[fingerprint] = saved
                outcomes.append(
                    (definition.package_key, saved["version"], action, saved["content_hash"])
                )
        table = Table("package", "version", "action", "hash")
        for key, version, action, content_hash in outcomes:
            table.add_row(key, str(version), action, content_hash[:12])
        rprint(table)

    _run(run)


@app.command("list")
def list_packages() -> None:
    """Stored package versions."""

    async def run() -> None:
        from investment_agent.storage.database import session_scope
        from investment_agent.storage.models.investor import MethodArtifact

        async with session_scope() as session:
            svc = MethodService(session)
            rows = (
                await session.scalars(
                    select(MethodArtifact).where(
                        MethodArtifact.owner_id == svc.owner, MethodArtifact.kind == "package"
                    )
                )
            ).all()
        table = Table("package", "version", "hash", "source", "created")
        payloads = sorted(
            (row.payload for row in rows),
            key=lambda payload: (payload["package_key"], payload["version"]),
        )
        for payload in payloads:
            table.add_row(
                payload["package_key"],
                str(payload["version"]),
                payload["content_hash"][:8],
                payload["source"],
                str(payload["created_at"])[:19].replace("T", " "),
            )
        rprint(table)

    _run(run)


def _parse_ref(ref: str, *, option: str) -> tuple[str, int | None]:
    """``key@version`` or a bare ``key``; a non-numeric version is a bad parameter."""

    key, at, version_text = ref.partition("@")
    if not at:
        return key.strip(), None
    if not version_text.isdigit():
        raise typer.BadParameter("expected <key@version> with a numeric version", param_hint=option)
    return key.strip(), int(version_text)


async def _stored_package(key: str, version: int | None) -> dict[str, Any] | None:
    """The stored package payload; ``version=None`` selects the latest one."""

    from investment_agent.storage.database import session_scope
    from investment_agent.storage.models.investor import MethodArtifact

    async with session_scope() as session:
        svc = MethodService(session)
        rows = (
            await session.scalars(
                select(MethodArtifact).where(
                    MethodArtifact.owner_id == svc.owner, MethodArtifact.kind == "package"
                )
            )
        ).all()
    matches = [row.payload for row in rows if row.payload["package_key"] == key]
    if version is None:
        return max(matches, key=lambda payload: payload["version"], default=None)
    return next((payload for payload in matches if payload["version"] == version), None)


def _to_yaml(payload: dict[str, Any]) -> str:
    """Deterministic YAML (sorted keys) of the canonical definition."""

    from investment_agent.investor.method_packages import LIFECYCLE_FIELDS, canonical_definition

    content = {field: value for field, value in payload.items() if field not in LIFECYCLE_FIELDS}
    return yaml.safe_dump(
        canonical_definition(content),
        sort_keys=True,
        allow_unicode=True,
        default_flow_style=False,
    )


@app.command("show")
def show(ref: str = typer.Argument(..., help="Package key[@version], latest when no version")) -> None:
    """Print a stored package version as canonical YAML."""

    key, version = _parse_ref(ref, option="ref")

    async def run() -> str | None:
        payload = await _stored_package(key, version)
        if payload is None:
            return None
        header = (
            f"# package {payload['package_key']}@{payload['version']} "
            f"hash={payload['content_hash'][:12]}\n"
        )
        return header + _to_yaml(payload)

    text = _run(run)
    if text is None:
        rprint(f"[red]Not found[/red]: {escape(ref)}")
        raise typer.Exit(code=1)
    typer.echo(text)


async def _binding_summary(svc: MethodService, binding: MethodBinding) -> str:
    """One-line binding digest: method version, package key@version, hash, switches."""

    from investment_agent.investor.publication import FIXED_METHOD

    method = "legacy" if binding.method_ref == FIXED_METHOD else str(binding.method_ref.id)[:8]
    parts = [f"method={method}"]
    if binding.package_ref is not None and binding.package_hash is not None:
        try:
            package = await svc.package_by_ref(binding.package_ref)
            parts.append(f"package={package['package_key']}@{package['version']}")
        except InvestorError:
            parts.append(f"package={str(binding.package_ref.id)[:8]}")
        parts.append(f"hash={binding.package_hash[:8]}")
    switches = [
        name.removeprefix("require_")
        for name in (
            "require_two_counter_hypotheses",
            "require_verified_counter_evidence",
            "require_explicit_stop_decision",
        )
        if getattr(binding.config, name)
    ]
    parts.append("config=" + (",".join(switches) or "none"))
    return " ".join(parts)


@app.command("status")
def status(
    channel: str = typer.Option(DEFAULT_CHANNEL, "--channel", help="Global default channel"),
) -> None:
    """The global default head and every per-listing override."""

    async def run() -> None:
        from investment_agent.storage.database import session_scope
        from investment_agent.storage.models.investor import MethodHead
        from investment_agent.storage.models.reference import Company, Listing

        async with session_scope() as session:
            svc = MethodService(session)
            revision, binding = await svc.default_head(channel)
            summary = await _binding_summary(svc, binding) if binding else None
            heads = (
                await session.scalars(
                    select(MethodHead)
                    .where(MethodHead.owner_id == svc.owner)
                    .order_by(MethodHead.listing_id)
                )
            ).all()
            names: dict[str, str] = {}
            if heads:
                listing_ids = {head.listing_id for head in heads}
                names = {
                    str(row[0]): f"{row[2]} · {row[1]}"
                    for row in (
                        await session.execute(
                            select(Listing.id, Listing.ticker, Company.canonical_name)
                            .join(Company, Listing.company_id == Company.id)
                            .where(Listing.id.in_(listing_ids))
                        )
                    ).all()
                }
            overrides = [
                (
                    escape(names.get(str(head.listing_id), str(head.listing_id))),
                    head.revision,
                    await _binding_summary(svc, MethodBinding.model_validate(head.payload)),
                )
                for head in heads
            ]
        if binding is None:
            rprint(
                f"Global default head [bold]{escape(channel)}[/bold]: [yellow]not set[/yellow]; "
                "new company research / review tasks use the legacy v1 baseline"
            )
        else:
            rprint(
                f"Global default head [bold]{escape(channel)}[/bold] revision {revision}: {summary}"
            )
        table = Table("listing", "revision", "binding")
        for name, head_revision, digest in overrides:
            table.add_row(name, str(head_revision), digest)
        rprint(table)

    _run(run)


def _mutation_id(key: str, version: int, expected_revision: int, reason: str) -> UUID:
    """Content-derived operation id: re-running the same activation replays it."""

    return uuid5(
        NAMESPACE_URL,
        f"methods/activate-global:{DEFAULT_CHANNEL}:{key}@{version}:{expected_revision}:{reason}",
    )


@app.command("activate-global")
def activate_global(
    package: str = typer.Option(..., "--package", help="Package to activate, as key@version"),
    expected_revision: int = typer.Option(
        ..., "--expected-revision", min=0, help="Current global head revision (0 = none set yet)"
    ),
    reason: str = typer.Option(..., "--reason", help="Why this package is being activated"),
) -> None:
    """Explicitly activate one package as the global default head (user operation).

    Appends a method version and the audit event, then moves the global default
    head; per-listing heads are never touched (design §5).
    """

    key, version = _parse_ref(package, option="--package")
    if version is None:
        raise typer.BadParameter(
            "expected <key@version>; activating a bare key is ambiguous", param_hint="--package"
        )

    async def run() -> int:
        from investment_agent.investor.common import ObjectRef
        from investment_agent.investor.methods import ActivateGlobal
        from investment_agent.storage.database import session_scope

        payload = await _stored_package(key, version)
        if payload is None:
            rprint(f"[red]Not found[/red]: {escape(package)}")
            return 1
        request = ActivateGlobal(
            mutation_id=_mutation_id(key, version, expected_revision, reason),
            channel=DEFAULT_CHANNEL,
            package_ref=ObjectRef(
                kind="package", id=UUID(payload["artifact_id"]), revision=payload["version"]
            ),
            expected_revision=expected_revision,
            reason=reason,
        )
        try:
            async with session_scope() as session:
                svc = MethodService(session)
                if (replayed := await svc.replay(request.mutation_id, request)) is not None:
                    rprint(
                        f"[yellow]Replay[/yellow] {escape(key)}@{version} on channel "
                        f"{DEFAULT_CHANNEL}: head revision {replayed['head_revision']}, "
                        f"method version {replayed['id']}"
                    )
                    return 0
                result = await svc.activate_global(request, actor="authenticated_user")
        except InvestorError as exc:
            rprint(f"[red]{escape(exc.detail.code)}[/red] {escape(str(exc))}")
            return 1
        rprint(
            f"[green]Activated[/green] {escape(key)}@{version} on channel {DEFAULT_CHANNEL}: "
            f"head revision {result['head_revision']}, method version {result['id']}"
        )
        return 0

    if code := _run(run):
        raise typer.Exit(code=code)
