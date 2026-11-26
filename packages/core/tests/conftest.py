# Share the local jupyter_server fixture across test modules.
import os

import pytest

from tests.test_jupyter_integration import jupyter  # noqa: F401


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "posix_runtime: runs the agent in the local Jupyter stand-in, which must be POSIX like a real Colab runtime",
    )


def pytest_collection_modifyitems(config, items):
    # A real Colab runtime is always Linux. On Windows the local stand-in hands
    # the agent Windows paths and Windows kernel interrupts, which it never sees
    # in production, so these end-to-end runs are skipped there.
    if os.name != "nt":
        return
    skip = pytest.mark.skip(reason="needs a POSIX runtime stand-in (Colab runtimes are Linux)")
    for item in items:
        if "posix_runtime" in item.keywords:
            item.add_marker(skip)
