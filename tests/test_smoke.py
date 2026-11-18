import importlib

import pytest

PACKAGES = ["gate", "faults", "analysis", "orchestrator"]


@pytest.mark.parametrize("name", PACKAGES)
def test_package_imports(name):
    assert importlib.import_module(name).__name__ == name
