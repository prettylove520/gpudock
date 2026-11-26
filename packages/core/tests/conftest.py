# Share the local jupyter_server fixture across test modules.
from tests.test_jupyter_integration import jupyter  # noqa: F401
