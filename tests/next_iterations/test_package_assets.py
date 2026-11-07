"""PK-02: inspect a real disposable wheel, never an installed/live application."""

from pathlib import Path

import pytest
from tests.support.next_iterations_package import (
    ROOT,
    assert_wheel_assets,
    asset_paths,
    build_wheel,
    prepare_source,
)


def test_packaged_assets_include_nested_css(tmp_path: Path) -> None:
    source = prepare_source(tmp_path / "source")
    probe = source / "src/investment_agent/web/static/pages/packaging_probe.css"
    probe.write_text(".packaging-probe { display: grid; }\n")
    wheel = build_wheel(source, tmp_path / "wheel")
    assert_wheel_assets(
        wheel, (*asset_paths("core"), "investment_agent/web/static/pages/packaging_probe.css"),
    )
    with pytest.raises(AssertionError, match="missing required assets"):
        assert_wheel_assets(wheel, ("investment_agent/web/static/definitely_missing.js",))
    with pytest.raises(ValueError, match="empty asset"):
        assert_wheel_assets(wheel, ())


def test_unknown_manifest_cannot_pass() -> None:
    with pytest.raises(ValueError, match="no package asset manifest"):
        asset_paths("unknown")


def test_build_refuses_original_checkout(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="disposable directory"):
        build_wheel(ROOT, tmp_path / "not_used")
