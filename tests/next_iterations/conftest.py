"""Local test directory must also use the shared early-loading offline guard."""

import sys

import pytest


def pytest_configure(config: pytest.Config) -> None:
    if "next_iterations_offline" not in sys.modules:
        raise RuntimeError("use scripts/check_next_iterations.py --scope BASE-03 --offline")
