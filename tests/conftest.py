import pytest

from mimivox.setup_service import Ready, setup_manager


@pytest.fixture(autouse=True)
def models_ready():
    """Routes that need models run as if setup finished; setup tests override it."""
    setup_manager.reset(Ready())
    yield
    setup_manager.reset(Ready())
