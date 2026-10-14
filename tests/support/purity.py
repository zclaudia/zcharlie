"""Structure-aware purity checks for a single module's source file.

The ``test_material_*`` discipline tests assert that a computation module stays
free of IO/network/database imports and of wording it must never surface. They
used to read the module as raw text and search it for substrings, which failed
the build on a comment that merely mentioned a library and could not follow the
code when a module was split.

These helpers parse the file with :mod:`ast` instead: imports are checked where
Python actually imports (any scope, any alias form) and wording is checked
against string constants and docstrings — what the module can actually emit —
so a comment is free to talk while a real ``import structlog`` or a user-visible
sentence is caught. Nothing here imports ``src``; the file is only ever read.
"""

from __future__ import annotations

import ast
from pathlib import Path


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


def imported_modules(path: Path) -> set[str]:
    """Every module name imported anywhere in the file, nested scopes included.

    ``import a.b`` and ``from a.b import c`` both contribute ``"a.b"``. A
    relative import contributes its name with the leading dots (``".sibling"``),
    so a caller comparing top-level absolute names is never confused by an
    intra-package import that happens to share a name with a library.
    """

    modules: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            modules.add("." * node.level + (node.module or ""))
    return modules


def assert_no_io_imports(path: Path, *, forbidden: tuple[str, ...]) -> None:
    """Fail if the file imports a forbidden module under any top-level name.

    Comparison is against the first component of every absolute import, so
    ``urllib.request`` is rejected by ``"urllib"`` and an alias (``import httpx
    as h``) cannot hide the import. Relative imports stay inside the package.
    """

    offenders = sorted(
        name
        for name in imported_modules(path)
        if not name.startswith(".") and name.split(".")[0] in forbidden
    )
    assert not offenders, f"{path.name} imports forbidden modules: {offenders}"


def referenced_names(path: Path) -> set[str]:
    """Every identifier and attribute name referenced anywhere in the file."""

    names: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def text_of_strings_and_docstrings(path: Path) -> str:
    """All static text the file can emit to a caller: strings and docstrings.

    Docstrings are string constants themselves, so one walk covers both. A
    formatted string contributes only its literal parts (the interpolated
    expressions cannot be known statically). Fragments are joined with newlines
    so a phrase must live inside one literal instead of being stitched together
    from two unrelated strings.
    """

    fragments = [
        node.value
        for node in ast.walk(_parse(path))
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    ]
    return "\n".join(fragments)
