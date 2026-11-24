"""Unit tests for models_service (no network, mocked HF cache)."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from mimivox.config import CAT_MODEL_ID, WHISPER_MODEL
from mimivox.models_service import (
    _set_download_state,
    download_models,
    get_download_state,
    model_present,
    reset_cache,
    verify,
)


@pytest.fixture(autouse=True)
def _reset_dl_state():
    _set_download_state(busy=False, command=None, message="", error=None, progress=None)
    yield
    _set_download_state(busy=False, command=None, message="", error=None, progress=None)


def test_model_present_local_path(tmp_path: Path):
    model_dir = tmp_path / "my-model"
    model_dir.mkdir()
    ready, msg, path = model_present(str(model_dir))
    assert ready is True
    assert "Local path" in msg
    assert path is not None


def test_model_present_missing_hf_repo():
    with (
        patch("huggingface_hub.snapshot_download", side_effect=Exception("missing")),
        patch("huggingface_hub.try_to_load_from_cache", return_value=None),
    ):
        ready, msg, path = model_present("org/does-not-exist-xyz")
    assert ready is False
    assert "Not found" in msg
    assert path is None


def _snapshot(tmp_path: Path, files: dict[str, int]) -> Path:
    repo = tmp_path / "models--org--m"
    blobs = repo / "blobs"
    snap = repo / "snapshots" / "abc"
    blobs.mkdir(parents=True)
    snap.mkdir(parents=True)
    for i, (name, size) in enumerate(files.items()):
        blob = blobs / f"blob{i}"
        blob.write_bytes(b"0" * size)
        (snap / name).symlink_to(Path("../../blobs") / blob.name)
    return snap


def _present(snap: Path) -> bool:
    with (
        patch("huggingface_hub.snapshot_download", return_value=str(snap)),
        patch("huggingface_hub.try_to_load_from_cache", return_value=str(snap / "config.json")),
    ):
        return model_present("org/m")[0]


def test_model_present_hf_cache_hit(tmp_path: Path):
    snap = _snapshot(tmp_path, {"config.json": 2, "model.safetensors": 2_000_000})
    with patch("huggingface_hub.snapshot_download", return_value=str(snap)):
        ready, msg, path = model_present("org/m")
    assert ready is True
    assert "cache" in msg.lower()
    assert path == str(snap)


def test_stale_incomplete_blob_from_an_older_revision_does_not_hide_a_complete_model(
    tmp_path: Path,
):
    snap = _snapshot(tmp_path, {"config.json": 2, "model.safetensors": 2_000_000})
    (snap.parent.parent / "blobs" / "oldrevision.incomplete").write_bytes(b"0")
    assert _present(snap) is True


def test_model_present_rejects_snapshot_without_weights(tmp_path: Path):
    assert _present(_snapshot(tmp_path, {"config.json": 2})) is False


def test_model_present_rejects_snapshot_file_whose_blob_is_gone(tmp_path: Path):
    snap = _snapshot(
        tmp_path, {"config.json": 2, "model.safetensors": 2_000_000, "tokenizer.json": 2}
    )
    (snap.parent.parent / "blobs" / "blob2").unlink()
    assert _present(snap) is False


def test_model_present_rejects_missing_shard(tmp_path: Path):
    snap = _snapshot(tmp_path, {"config.json": 2, "model-00001.safetensors": 2_000_000})
    index = {"weight_map": {"a": "model-00001.safetensors", "b": "model-00002.safetensors"}}
    (snap / "model.safetensors.index.json").write_text(json.dumps(index))
    assert _present(snap) is False


def test_verify_structure():
    with patch("mimivox.models_service.model_present", return_value=(True, "ok", "/p")):
        result = verify({"model_paths": {"whisper": WHISPER_MODEL, "cat_translate": CAT_MODEL_ID}})
    assert "whisper" in result
    assert "cat_translate" in result
    assert "next_steps" in result
    assert result["whisper"]["ready"] is True


def test_verify_missing_reports_not_ready():
    def _present(model_id: str):
        if "missing" in model_id:
            return False, "Not found in cache", None
        return True, "ok", "/p"

    with patch("mimivox.models_service.model_present", side_effect=_present):
        result = verify(
            {
                "model_paths": {
                    "whisper": "org/missing-whisper",
                    "cat_translate": CAT_MODEL_ID,
                }
            }
        )
    assert result["whisper"]["ready"] is False
    assert result["ready"] is False
    assert any("Download whisper" in s for s in result["next_steps"])


def test_download_models_calls_snapshot(tmp_path: Path):
    local = tmp_path / "local-whisper"
    local.mkdir()
    calls: list[str] = []

    def _snap(repo_id: str, *, force: bool = False):
        calls.append(repo_id)
        return f"/cache/{repo_id}"

    with (
        patch("mimivox.models_service._snapshot_download", side_effect=_snap),
        patch("mimivox.models_service._download_speaker_model", return_value={"skipped": True}),
    ):
        result = download_models(
            {
                "model_paths": {
                    "whisper": str(local),
                    "cat_translate": "org/cat-model",
                }
            }
        )
    assert result["ok"] is True, result
    assert result["whisper"]["skipped"] is True
    assert result["cat_translate"]["path"] == "/cache/org/cat-model"
    assert calls == ["org/cat-model"]


@pytest.fixture(autouse=True)
def _isolated_hf_cache(tmp_path: Path, monkeypatch):
    """reset_cache deletes MimiVox's repos from the HF cache; never let a test reach the real one."""
    from huggingface_hub import constants

    monkeypatch.setattr(constants, "HF_HUB_CACHE", str(tmp_path / "hf-hub"))


def test_reset_cache_refuses_without_yes():
    result = reset_cache(yes=False)
    assert result["ok"] is False
    assert "refused" in result["message"].lower()


def test_reset_cache_refuses_global_path(tmp_path: Path):
    outside = tmp_path / "outside"
    outside.mkdir()
    result = reset_cache(yes=True, scoped_path=str(outside))
    assert result["ok"] is False
    assert "Refusing" in result["message"]


def test_reset_cache_distinguishes_subdirectories_from_sibling_paths(tmp_path: Path, monkeypatch):
    managed = tmp_path / "models"
    scoped = managed / "download"
    sibling = tmp_path / "models-other"
    scoped.mkdir(parents=True)
    sibling.mkdir()
    monkeypatch.setattr("mimivox.models_service.MIMIVOX_MODELS_DIR", managed)

    assert not reset_cache(yes=True, scoped_path=str(sibling))["ok"]
    assert reset_cache(yes=True, scoped_path=str(scoped))["ok"]
    assert not scoped.exists()
    assert sibling.is_dir()


def test_reset_cache_leaves_linked_cache_target_alone(tmp_path: Path, monkeypatch):
    managed = tmp_path / "models"
    other = tmp_path / "other"
    other.mkdir()
    managed.symlink_to(other, target_is_directory=True)
    monkeypatch.setattr("mimivox.models_service.MIMIVOX_MODELS_DIR", managed)

    result = reset_cache(yes=True)

    assert result["ok"] is False
    assert other.is_dir()


def test_reset_cache_clears_managed_dir(tmp_path: Path, monkeypatch):
    managed = tmp_path / "models"
    managed.mkdir()
    junk = managed / "junk.bin"
    junk.write_bytes(b"x")
    monkeypatch.setattr("mimivox.models_service.MIMIVOX_MODELS_DIR", managed)

    result = reset_cache(yes=True, scoped_path=str(managed))
    assert result["ok"] is True
    assert managed.exists()  # recreated
    assert not junk.exists()


def test_reset_cache_removes_mimivox_repos_from_hf_cache(tmp_path: Path, monkeypatch):
    from huggingface_hub import constants

    from mimivox.config import CAT_MODEL_ID

    managed = tmp_path / "models"
    managed.mkdir()
    hub = Path(constants.HF_HUB_CACHE)
    ours = hub / ("models--" + CAT_MODEL_ID.replace("/", "--"))
    (ours / "blobs").mkdir(parents=True)
    (ours / "blobs" / "abc").write_bytes(b"x" * 1000)
    other = hub / "models--someone--else"
    other.mkdir(parents=True)
    monkeypatch.setattr("mimivox.models_service.MIMIVOX_MODELS_DIR", managed)

    result = reset_cache(yes=True)
    assert result["ok"] is True
    assert not ours.exists()
    assert other.exists()
    assert managed.exists()
    assert result["message"].startswith("Removed 1 model ")


def test_get_download_state_roundtrip():
    _set_download_state(busy=True, message="downloading…")
    st = get_download_state()
    assert st["busy"] is True
    assert st["message"] == "downloading…"


def test_api_models_verify():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mimivox.api_adapter import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)

    with patch(
        "mimivox.models_service.verify",
        return_value={
            "ready": True,
            "next_steps": [],
            "whisper": {"ready": True, "message": "ok"},
            "cat_translate": {"ready": True, "message": "ok"},
        },
    ):
        resp = client.post("/api/models/verify", json={})
    assert resp.status_code == 200
    assert "ready" in resp.json()["message"].lower() or "verified" in resp.json()["message"].lower()
    assert "model_readiness" in resp.json()


@pytest.mark.parametrize("body", [{}, {"yes": "false"}])
def test_api_models_reset_refused(body, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mimivox.api_adapter import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    monkeypatch.setattr("mimivox.models_service.get_download_state", lambda: {"busy": True})
    resp = client.post("/api/models/reset-cache", json=body)
    assert resp.status_code == 400
    assert "refused" in resp.json()["message"].lower()


def test_api_models_unknown():
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from mimivox.api_adapter import router

    app = FastAPI()
    app.include_router(router)
    client = TestClient(app)
    resp = client.post("/api/models/explode", json={})
    assert resp.status_code == 400
