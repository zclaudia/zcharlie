"""The inbox suite uses the existing early-loaded BASE-03 guard."""

import sys

import pytest


def pytest_configure(config: pytest.Config) -> None:
    if "next_iterations_offline" not in sys.modules or not config.pluginmanager.hasplugin(
        "next_iterations_offline"
    ):
        raise pytest.UsageError(
            "Inbox tests require NEXT_ITERATIONS_OFFLINE=1 and -p next_iterations_offline"
        )
