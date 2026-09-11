"""Shared fixtures for Sable test suite."""
import pytest


def pytest_collection_modifyitems(config, items):
    """Skip live tests unless explicitly requested via -m live."""
    if config.getoption("-m", default="") != "live":
        skip_live = pytest.mark.skip(reason="Live test (use -m 'live' to run)")
        for item in items:
            if "live" in item.keywords:
                item.add_marker(skip_live)
