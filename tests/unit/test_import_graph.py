"""Import-graph guard: package layering direction plus a no-new-coupling ratchet (A4).

What this guard protects
------------------------
The repository had no static guard on its module dependency graph, so "move an import
around" refactors were invisible until something broke at runtime.  This module parses
every file under ``src/investment_agent/`` with :mod:`ast` and turns the import graph into
two kinds of assertion:

1. **Absolute invariants** -- small, non-negotiable layering rules (``storage`` must not
   reach into ``api``; ``api/schemas/*`` must not reach into ``storage.models``;
   ``domain`` is a leaf; ``providers`` must not reach into ``api``/``cli``).  These are
   asserted directly, so a violation fails here with the offending ``file:line``.
2. **A ratchet** -- the set of remaining package-pair edges (``src_package -> dst_package``)
   is frozen in ``tests/fixtures/import_graph_baseline.json`` and may only *shrink*.
   Adding an edge fails; deleting an edge from the baseline is not required (and removing
   a real edge from the code is always allowed, so the graph can only improve).

Function-level imports count.  At the time of writing, 479 of the 2,024 intra-project import
statements (counting each statement once; cross-checked with a separate parent-chain walk)
sit inside function bodies, which is exactly where the graph hides its cycles: e.g.
``storage/repositories/pipeline_run.py`` imports ``agent.operations`` / ``agent.tasks``
lazily in three places, and ``mcp_server/server.py`` reaches into ``api.routes.*`` from
inside seven handlers.  Therefore the AST walk visits the whole tree (``ast.NodeVisitor``
over every node, not just ``tree.body``); ``scope`` is reported as ``module`` or
``function`` in failure messages so the reader knows which one it is.

``domain`` is a sanctioned shared leaf.  A parallel work package adds
``investment_agent/domain/time.py`` (already imported by storage, api, mcp_server,
snapshots, verification, industry_peers, ...), so edges whose *destination* package is
``domain`` are always allowed and are deliberately absent from the baseline -- adding such
an edge must never trip the ratchet.  ``domain`` itself must stay a leaf (no intra-project
imports, which holds today, ``time.py`` included).

Regenerating the baseline
-------------------------
Run the module directly::

    .venv/bin/python tests/unit/test_import_graph.py

It rewrites ``tests/fixtures/import_graph_baseline.json`` with a sorted, path-free,
timestamp-free document, so regeneration is byte-for-byte reproducible.  Only regenerate
after a refactor that legitimately removes edges (or after consciously accepting a new
one, which a reviewer must see in the diff).

Known blind spots (documented, not silently ignored)
----------------------------------------------------
Dynamic imports are invisible to an AST scan.  The in-project ones today are
``candidates/promote.py`` (``importlib.import_module("investment_agent.verification")`` -- a
real ``candidates -> verification`` edge this guard cannot see) and the hook module names in
``rules/drafting.py`` (``_HOOK_MODULES`` / ``_WATCH_HOOK_MODULES``: ``candidates.lifecycle``,
``watchlist.drafts``, both already present as static edges).  The root
``investment_agent/__init__.py`` belongs to no sub-package and has no imports, so it is not
scanned as an edge source.
"""

from __future__ import annotations

import ast
import json
import re
from collections import defaultdict
from dataclasses import dataclass
from pathlib import Path

# Located relative to this file so the guard is cwd-independent and works from a clean
# checkout (same pattern as tests/unit/test_material_metrics.py).
PACKAGE_ROOT = Path(__file__).resolve().parents[2] / "src" / "investment_agent"
BASELINE_PATH = Path(__file__).resolve().parents[1] / "fixtures" / "import_graph_baseline.json"

PROJECT_PACKAGE = "investment_agent"
# Edges into these packages are sanctioned (always allowed) and never ratcheted.
SANCTIONED_TARGET_PACKAGES = frozenset({"domain"})
EDGE_ARROW = " -> "
_EDGE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(?: -> [A-Za-z_][A-Za-z0-9_]*)$")

REGEN_COMMAND = ".venv/bin/python tests/unit/test_import_graph.py"


@dataclass(frozen=True)
class ImportSite:
    """One import statement: which module wrote it, where, and what it pulls in."""

    source_module: str
    line: int
    imported: str
    scope: str  # "module" or "function"

    def describe(self) -> str:
        return f"{self.source_module}:{self.line} ({self.scope}-level import of {self.imported})"


@dataclass(frozen=True)
class ImportGraph:
    """All intra-project import sites plus the package-pair edges they induce."""

    sites: tuple[ImportSite, ...]
    # ("api", "storage") -> the sites that create that edge (each with a concrete line).
    edge_sites: dict[tuple[str, str], tuple[ImportSite, ...]]

    def sites_from(self, source_prefix: str) -> tuple[ImportSite, ...]:
        return tuple(
            s for s in self.sites
            if s.source_module == source_prefix
            or s.source_module.startswith(source_prefix + ".")
        )


def _module_name(path: Path) -> str:
    """Dotted module name for a file under the scanned package root."""
    relative = path.relative_to(PACKAGE_ROOT.parent).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _package_of(module: str) -> str | None:
    """Top-level package under ``investment_agent`` for a dotted name.

    ``investment_agent.api.routes.stocks`` -> ``"api"``.  ``None`` for the project root
    package itself and for any third-party name.
    """
    prefix = PROJECT_PACKAGE + "."
    if not module.startswith(prefix):
        return None
    return module[len(prefix):].split(".")[0]


def _iter_module_files() -> tuple[Path, ...]:
    return tuple(
        sorted(p for p in PACKAGE_ROOT.rglob("*.py") if "__pycache__" not in p.parts)
    )


class _ImportCollector(ast.NodeVisitor):
    """Collects every import in a module, at module level and nested in functions."""

    def __init__(self, module: str, package_parts: tuple[str, ...], known: frozenset[str]):
        self.module = module
        # The package that contains this module (used to resolve relative imports).
        self.package_parts = package_parts
        # Every dotted module name that exists in the tree, so ``from X import y`` can be
        # refined to ``X.y`` when ``y`` really is a module (not just an attribute).
        self.known = known
        self.function_depth = 0
        self.sites: list[ImportSite] = []

    def _note(self, node: ast.Import | ast.ImportFrom, dotted: str) -> None:
        self.sites.append(
            ImportSite(
                source_module=self.module,
                line=node.lineno,
                imported=dotted,
                scope="function" if self.function_depth else "module",
            )
        )

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self.function_depth += 1
        try:
            self.generic_visit(node)
        finally:
            self.function_depth -= 1

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function(node)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._note(node, alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if node.level:
            # ``level=1`` is the package containing this module; each extra level goes one
            # package up.  (The tree uses absolute imports today, but resolving this keeps
            # the guard honest if that ever changes.)
            climb = node.level - 1
            if climb >= len(self.package_parts):
                self.generic_visit(node)
                return  # escapes the project package: nothing to record
            parts = list(self.package_parts[: len(self.package_parts) - climb])
            if node.module:
                parts.extend(node.module.split("."))
            base = ".".join(parts)
        else:
            base = node.module or ""
        if not base:
            self.generic_visit(node)
            return
        self._note(node, base)
        for alias in node.names:
            if alias.name != "*" and f"{base}.{alias.name}" in self.known:
                self._note(node, f"{base}.{alias.name}")
        self.generic_visit(node)


def _collect_sites(files: tuple[Path, ...]) -> tuple[ImportSite, ...]:
    modules = {_module_name(path) for path in files}
    known = frozenset(modules | {PROJECT_PACKAGE})
    sites: list[ImportSite] = []
    for path in files:
        module = _module_name(path)
        # ``__init__.py`` *is* its package; for a plain module the package is its parent.
        if path.name == "__init__.py":
            package_parts = tuple(module.split("."))
        else:
            package_parts = tuple(module.split(".")[:-1])
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        collector = _ImportCollector(module, package_parts, known)
        collector.visit(tree)
        sites.extend(collector.sites)
    return tuple(sorted(sites, key=lambda s: (s.source_module, s.line, s.imported, s.scope)))


def _build_graph(files: tuple[Path, ...]) -> ImportGraph:
    sites = _collect_sites(files)
    by_edge: dict[tuple[str, str], list[ImportSite]] = defaultdict(list)
    for site in sites:
        source_package = _package_of(site.source_module)
        target_package = _package_of(site.imported)
        if source_package is None or target_package is None:
            continue  # root package / third-party import
        if source_package == target_package:
            continue  # inside one package, not a package-pair edge
        if target_package in SANCTIONED_TARGET_PACKAGES:
            continue  # sanctioned shared leaf: always allowed, never ratcheted
        by_edge[(source_package, target_package)].append(site)
    return ImportGraph(
        sites=sites,
        edge_sites={edge: tuple(edge_sites) for edge, edge_sites in by_edge.items()},
    )


GRAPH = _build_graph(_iter_module_files())

EDGE_NAMES = frozenset(
    f"{source}{EDGE_ARROW}{target}" for source, target in GRAPH.edge_sites
)


def _format_sites(sites: tuple[ImportSite, ...], limit: int = 5) -> str:
    lines = [f"    - {site.describe()}" for site in sites[:limit]]
    if len(sites) > limit:
        lines.append(f"    - ... and {len(sites) - limit} more site(s) for this edge")
    return "\n".join(lines)


def _violations(source_prefix: str, imported_prefix: str) -> tuple[ImportSite, ...]:
    """Import sites written *by* ``source_prefix`` that pull in ``imported_prefix``."""
    return tuple(
        site
        for site in GRAPH.sites_from(source_prefix)
        if site.imported == imported_prefix or site.imported.startswith(imported_prefix + ".")
    )


def _assert_no_violations(
    *, rule: str, sites: tuple[ImportSite, ...], fix_hint: str
) -> None:
    if sites:
        details = _format_sites(sites)
        raise AssertionError(
            f"Import-graph invariant broken: {rule}\n"
            f"Offending import(s), {len(sites)} total:\n{details}\n"
            f"{fix_hint}"
        )


# --------------------------------------------------------------------------- #
# Absolute invariants
# --------------------------------------------------------------------------- #


def test_storage_never_imports_api() -> None:
    """Invariant 1 (holds today): nothing under storage/ imports investment_agent.api."""
    _assert_no_violations(
        rule="storage must not depend on api (layer direction: api -> storage, never back)",
        sites=_violations("investment_agent.storage", "investment_agent.api"),
        fix_hint=(
            "Move the shared piece down into storage (or into the sanctioned domain leaf) "
            "instead of importing upward."
        ),
    )


def test_api_schemas_never_imports_storage_models() -> None:
    """Invariant 2 (holds today): api/schemas/* never imports investment_agent.storage.models."""
    _assert_no_violations(
        rule="api/schemas/* (wire format) must not depend on storage ORM models",
        sites=_violations("investment_agent.api.schemas", "investment_agent.storage.models"),
        fix_hint=(
            "Define the response shape in the schema module and map explicitly from the ORM "
            "row at the route/service boundary."
        ),
    )


def test_domain_is_a_leaf() -> None:
    """Invariant 3 (holds today): domain/ has no intra-project imports at all."""
    _assert_no_violations(
        rule=f"domain must stay a leaf: no intra-project imports ({PROJECT_PACKAGE}.*)",
        sites=_violations("investment_agent.domain", PROJECT_PACKAGE),
        fix_hint=(
            "domain is the sanctioned shared leaf; it may only import the standard library "
            "and third-party packages. Move the dependency into the caller."
        ),
    )


def test_providers_never_import_api_or_cli() -> None:
    """Invariant 4 (holds today): nothing under providers/ imports api or cli."""
    offenders = (
        _violations("investment_agent.providers", "investment_agent.api")
        + _violations("investment_agent.providers", "investment_agent.cli")
    )
    _assert_no_violations(
        rule="providers must not depend on api or cli (transport/entry layers are on top)",
        sites=tuple(sorted(offenders, key=lambda s: (s.source_module, s.line))),
        fix_hint=(
            "Invert the dependency: let the api/cli module import the provider, or pass the "
            "needed value in as an argument."
        ),
    )


# --------------------------------------------------------------------------- #
# Ratchet: frozen allowlist that may only shrink
# --------------------------------------------------------------------------- #


def _load_baseline_edges() -> frozenset[str]:
    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    return frozenset(document["edges"])


def test_no_new_package_edges_beyond_baseline() -> None:
    """The ratchet: every current package-pair edge must be in the frozen baseline.

    Reductions are allowed and never fail: the baseline is a superset check, not equality,
    so refactors can only improve the graph without a baseline edit.
    """
    baseline = _load_baseline_edges()
    new_edges = sorted(EDGE_NAMES - baseline)
    if new_edges:
        blocks = []
        for edge in new_edges:
            source, target = edge.split(EDGE_ARROW)
            blocks.append(
                f"  {edge}\n" + _format_sites(GRAPH.edge_sites[(source, target)], limit=1)
            )
        raise AssertionError(
            f"{len(new_edges)} new intra-project package edge(s) not in the baseline:\n"
            + "\n".join(blocks)
            + "\n\nWhat this means: the import created a coupling between two packages that "
            "did not exist before, so the dependency graph got worse.\n"
            "Note that function-level (lazy) imports count too: moving an import inside a "
            "function does not hide it from this guard.\n"
            "If the edge is genuinely intended (and a reviewer agrees it is not a layering "
            "reversal), regenerate the baseline with:\n"
            f"    {REGEN_COMMAND}\n"
            f"and make sure the new edge is visible in the diff of {BASELINE_PATH.name}."
        )


def test_baseline_document_is_deterministic() -> None:
    """The baseline is a sorted, unique, path-free document so regeneration never churns."""
    document = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    edges = document["edges"]
    assert isinstance(edges, list) and all(isinstance(edge, str) for edge in edges)
    assert edges == sorted(edges), "baseline edges must be sorted"
    assert len(edges) == len(set(edges)), "baseline edges must be unique"
    assert all(_EDGE_RE.match(edge) for edge in edges), "every entry must be 'src -> dst'"
    assert document["generated_by"] == "tests/unit/test_import_graph.py"
    assert document["note"], "the note must explain the ratchet rule and how to regenerate"
    assert document["sanctioned_target_packages"] == sorted(SANCTIONED_TARGET_PACKAGES)


def _resolve_relative_imports(module: str, source: str, *, is_package: bool) -> set[str]:
    """Run the collector over a synthetic module and return what it resolved."""
    package_parts = (
        tuple(module.split(".")) if is_package else tuple(module.split(".")[:-1])
    )
    collector = _ImportCollector(
        module, package_parts, frozenset({"investment_agent.api.routes.schemas.stocks"})
    )
    collector.visit(ast.parse(source))
    return {site.imported for site in collector.sites}


def test_relative_import_resolution_is_correct() -> None:
    """The tree uses absolute imports today; the relative path must not silently lie."""
    plain = {"is_package": False}
    assert _resolve_relative_imports(
        "investment_agent.api.routes.stocks", "from . import schemas\n", **plain
    ) == {"investment_agent.api.routes"}
    assert _resolve_relative_imports(
        "investment_agent.api.routes.stocks", "from .schemas import stocks\n", **plain
    ) == {"investment_agent.api.routes.schemas", "investment_agent.api.routes.schemas.stocks"}
    assert _resolve_relative_imports(
        "investment_agent.api.routes.stocks", "from .. import auth\n", **plain
    ) == {"investment_agent.api"}
    assert _resolve_relative_imports(
        "investment_agent.api.routes.stocks", "from ... import web\n", **plain
    ) == {"investment_agent"}, "three levels up from api.routes is the project root package"
    assert _resolve_relative_imports(
        "investment_agent.api.routes.stocks", "from .... import web\n", **plain
    ) == set(), "a level that escapes the project package must record nothing"
    assert _resolve_relative_imports(
        "investment_agent.api.schemas", "from . import stocks\n", is_package=True
    ) == {"investment_agent.api.schemas"}


def test_sanctioned_domain_edges_are_never_ratcheted() -> None:
    """A new edge into domain must stay green (the parallel work package relies on this)."""
    domain_targets = {target for _, target in GRAPH.edge_sites}
    assert "domain" not in domain_targets, (
        "domain edges must be filtered out of the ratcheted edge set, never stored"
    )
    baseline = _load_baseline_edges()
    assert not any(edge.endswith(f"{EDGE_ARROW}domain") for edge in baseline)


# --------------------------------------------------------------------------- #
# Baseline regeneration (run as a script, never by pytest)
# --------------------------------------------------------------------------- #


def render_baseline(graph: ImportGraph) -> str:
    """Render the frozen document: sorted, path-free and timestamp-free."""
    document = {
        "edges": sorted(f"{source}{EDGE_ARROW}{target}" for source, target in graph.edge_sites),
        "generated_by": "tests/unit/test_import_graph.py",
        "note": (
            "Frozen allowlist of intra-project package-pair import edges (src_package -> "
            "dst_package) for src/investment_agent, scanned by tests/unit/test_import_graph.py "
            "with ast over module-level AND function-level imports. The test asserts that no "
            "edge exists that is missing here (the ratchet), so this list may only shrink: "
            "removing an edge from the code never fails the test, and therefore never requires "
            "regenerating this file. Edges whose destination package is 'domain' are sanctioned "
            "and are deliberately not listed. To regenerate after an intentional change run: "
            f"{REGEN_COMMAND} -- then review the diff for newly accepted couplings."
        ),
        "sanctioned_target_packages": sorted(SANCTIONED_TARGET_PACKAGES),
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def main() -> None:
    BASELINE_PATH.write_text(render_baseline(GRAPH), encoding="utf-8")
    print(f"wrote {len(GRAPH.edge_sites)} frozen edges to {BASELINE_PATH}")


if __name__ == "__main__":
    main()
