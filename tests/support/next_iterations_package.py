"""Build a disposable wheel and check exact assets, without installing anything."""

from __future__ import annotations

import importlib.util
import json
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "tests/fixtures/next_iterations/package-assets.json"
PACKAGE_PREFIX = "investment_agent/web/"


def asset_paths(scope: str) -> tuple[str, ...]:
    """Return a reviewed nonempty list; unknown scopes cannot pass vacuously."""
    manifest = json.loads(MANIFEST.read_text())
    values = manifest["scopes"].get(scope)
    if not isinstance(values, list) or not values:
        raise ValueError(f"no package asset manifest for {scope!r}")
    if any(not isinstance(value, str) or Path(value).is_absolute()
           or ".." in Path(value).parts for value in values):
        raise ValueError("package asset names must be relative and cannot escape the package")
    return tuple(PACKAGE_PREFIX + value for value in values)


def prepare_source(destination: Path, *, source: Path = ROOT) -> Path:
    """Copy only package source and build metadata, never .env, .git or app data."""
    destination.mkdir(parents=True, exist_ok=False)
    shutil.copy2(source / "pyproject.toml", destination / "pyproject.toml")
    shutil.copytree(
        source / "src", destination / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.egg-info", ".env", ".env.*"),
    )
    return destination


def cached_backend_paths() -> tuple[Path, ...]:
    """Use installed tools, or already cached pure-Python build tools read-only.

    This supports the existing development environment without downloading or
    upgrading dependencies. CI may simply install its declared build backend.
    Missing tools fail the packaging check; they never turn it into a skip.
    """
    from packaging.specifiers import SpecifierSet
    from packaging.version import InvalidVersion, Version

    cache = Path.home() / ".cache/uv/wheels-v6/pypi"
    found: list[Path] = []
    for package, requirement in (("setuptools", ">=68"), ("wheel", ">=0")):
        if importlib.util.find_spec(package) is not None:
            continue
        candidates: list[tuple[Version, Path]] = []
        for path in (cache / package).glob("*-py3-none-any"):
            if not path.is_dir() or not (path / package).is_dir():
                continue
            try:
                version = Version(path.name.removesuffix("-py3-none-any"))
            except InvalidVersion:
                continue
            if version in SpecifierSet(requirement):
                candidates.append((version, path.resolve()))
        if not candidates:
            raise RuntimeError(f"offline build backend unavailable: {package}{requirement}")
        found.append(max(candidates, key=lambda item: item[0])[1])
    return tuple(found)


def build_wheel(source: Path, output: Path) -> Path:
    """Run the existing PEP 517 backend in a child against a scratch source tree."""
    if source.resolve() == ROOT.resolve() or source.resolve().is_relative_to(ROOT.resolve()):
        raise ValueError("build source must be a disposable directory outside the checkout")
    output.mkdir(parents=True, exist_ok=False)
    paths = cached_backend_paths()
    code = (
        "import sys; sys.dont_write_bytecode=True; "
        "sys.path[:0]=sys.argv[2:]; "
        "from setuptools.build_meta import build_wheel; "
        "print('BUILT_WHEEL='+build_wheel(sys.argv[1]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", code, str(output.resolve()), *(str(path) for path in paths)],
        cwd=source, text=True, capture_output=True, check=False,
    )
    if result.returncode:
        raise RuntimeError(f"offline wheel build failed ({result.returncode}):\n"
                           f"{result.stdout}\n{result.stderr}")
    wheels = sorted(output.glob("*.whl"))
    if len(wheels) != 1:
        raise RuntimeError(f"expected one new wheel, found {len(wheels)}")
    return wheels[0]


def assert_wheel_assets(wheel: Path, expected: tuple[str, ...]) -> None:
    if not expected:
        raise ValueError("an empty asset check cannot validate a package")
    with zipfile.ZipFile(wheel) as archive:
        names = set(archive.namelist())
        missing = sorted(set(expected) - names)
        if missing:
            raise AssertionError(f"wheel missing required assets: {missing}")
        for name in expected:
            if not archive.read(name):
                raise AssertionError(f"wheel contains empty asset: {name}")
        forbidden = [name for name in names if any(
            part == ".env" or part.startswith(".env.") or part == ".git"
            for part in Path(name).parts
        )]
        if forbidden:
            raise AssertionError(f"wheel contains forbidden metadata: {forbidden}")
