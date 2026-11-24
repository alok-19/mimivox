import pytest

from mimivox.setup_service import Ready, setup_manager


@pytest.fixture(autouse=True)
def no_asr_preload(monkeypatch):
    """pipeline.run() preloads its ASR model; tests never load a real one."""
    monkeypatch.setattr("mimivox.pipeline.preload_asr_model", lambda model_id: None)


@pytest.fixture(autouse=True)
def models_ready():
    """Routes that need models run as if setup finished; setup tests override it."""
    setup_manager.reset(Ready())
    yield
    setup_manager.reset(Ready())
