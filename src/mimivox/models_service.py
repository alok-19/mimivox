"""mimivox.models_service — Verify / download HuggingFace model cache.

Does not hot-reload models into the running process. Download is for cache
population; a server restart is still required to swap model IDs.
"""

from __future__ import annotations

import json
import shutil
import threading
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mimivox.config import CAT_MODEL_ID, DATA_DIR, NOTES_MODEL_ID, WHISPER_MODEL, _models
from mimivox.diarization import (
    SPEAKER_MODEL_FILE,
    SPEAKER_MODEL_REPO,
    SPEAKER_MODEL_REVISION,
    SPEAKER_MODEL_SHA256,
    SPEAKER_MODEL_SIZE,
    speaker_model_path,
)

# Scoped cache MimiVox may clear — never the global HF home without explicit yes.
MIMIVOX_MODELS_DIR = Path(DATA_DIR) / "models"

_state_lock = threading.Lock()
_download_state: dict[str, Any] = {
    "busy": False,
    "command": None,
    "message": "",
    "error": None,
    "progress": None,
}


def get_download_state() -> dict[str, Any]:
    with _state_lock:
        return dict(_download_state)


def _set_download_state(**kwargs: Any) -> None:
    with _state_lock:
        _download_state.update(kwargs)


def _is_local_path(model_id: str) -> bool:
    p = Path(model_id)
    return p.exists() and (p.is_dir() or p.is_file())


_WEIGHT_SUFFIXES = {".safetensors", ".bin", ".gguf", ".pt", ".npz"}
_MIN_WEIGHT_BYTES = 1_000_000


def _snapshot_complete(snapshot: Path) -> bool:
    """``snapshot_download`` writes ``refs/main`` before any file, and a snapshot
    file appears only once its blob is complete."""
    try:
        files = [f for f in snapshot.rglob("*") if not f.is_dir()]
        if not all(f.is_file() for f in files):
            return False
        for index in (f for f in files if f.name.endswith(".index.json")):
            shards = json.loads(index.read_text()).get("weight_map", {}).values()
            if not all((index.parent / s).is_file() for s in shards):
                return False
        return any(
            f.suffix in _WEIGHT_SUFFIXES and f.stat().st_size > _MIN_WEIGHT_BYTES for f in files
        )
    except (OSError, ValueError):
        return False


def model_present(model_id: str) -> tuple[bool, str, str | None]:
    """Return (ready, message, resolved_path_or_none)."""
    if not model_id:
        return False, "No model id configured", None

    if _is_local_path(model_id):
        path = str(Path(model_id).resolve())
        return True, f"Local path ready: {path}", path

    try:
        from huggingface_hub import try_to_load_from_cache
        from huggingface_hub.constants import HF_HUB_CACHE
    except ImportError:
        return False, "huggingface_hub not installed", None

    # Prefer a definitive local-only snapshot probe.
    try:
        from huggingface_hub import snapshot_download

        path = snapshot_download(repo_id=model_id, local_files_only=True)
        if _snapshot_complete(Path(path)):
            return True, "Present in HuggingFace cache", path
    except Exception:
        pass

    # Fallback: any cached config/weights marker.
    try:
        cfg = try_to_load_from_cache(repo_id=model_id, filename="config.json")
        if cfg is not None and cfg is not True and _snapshot_complete(Path(str(cfg)).parent):
            return True, "Present in HuggingFace cache (config.json)", str(cfg)
    except Exception:
        pass

    return (
        False,
        f"Not found in cache ({HF_HUB_CACHE}); run download",
        None,
    )


def _loaded_in_process() -> dict[str, bool]:
    return {
        "whisper": True,  # mlx_whisper loads on demand; treat startup load as ready
        "cat_translate": _models.cat_model is not None,
        "vad": _models.silero is not None,
    }


def verify(settings: dict[str, Any] | None = None) -> dict[str, Any]:
    """Check HF cache / local path for whisper + CAT; return readiness detail."""
    settings = settings or {}
    paths = settings.get("model_paths") if isinstance(settings.get("model_paths"), dict) else {}
    whisper_id = str((paths or {}).get("whisper") or WHISPER_MODEL)
    cat_id = str((paths or {}).get("cat_translate") or CAT_MODEL_ID)

    w_ready, w_msg, w_path = model_present(whisper_id)
    c_ready, c_msg, c_path = model_present(cat_id)
    loaded = _loaded_in_process()

    # If settings path differs from the process-loaded IDs, note restart need.
    whisper_matches = whisper_id == WHISPER_MODEL
    cat_matches = cat_id == CAT_MODEL_ID
    next_steps: list[str] = []
    if not w_ready:
        next_steps.append(f"Download whisper model: {whisper_id}")
    if not c_ready:
        next_steps.append(f"Download CAT model: {cat_id}")
    if not whisper_matches or not cat_matches:
        next_steps.append("Restart server to apply model path changes")

    # In-process load counts as ready for the default IDs even if cache probe is flaky.
    if whisper_id == WHISPER_MODEL and loaded["whisper"]:
        w_ready = True
        if "Not found" in w_msg:
            w_msg = "Loaded in process at startup"
    if cat_id == CAT_MODEL_ID and loaded["cat_translate"]:
        c_ready = True
        if "Not found" in c_msg:
            c_msg = "Loaded in process at startup"

    ready = w_ready and c_ready
    n_ready, n_msg, n_path = model_present(NOTES_MODEL_ID)
    from mimivox import notes_llm

    return {
        "ready": ready,
        "next_steps": next_steps,
        "downloading": get_download_state()["busy"],
        "download": get_download_state(),
        "whisper": {
            "ready": w_ready,
            "message": w_msg,
            "model_id": whisper_id,
            "display_name": "Whisper large-v3-turbo (MLX q8)",
            "path": w_path or whisper_id,
            "loaded_path": WHISPER_MODEL,
            "matches_loaded": whisper_matches,
            "loaded_in_process": loaded["whisper"] and whisper_matches,
        },
        "cat_translate": {
            "ready": c_ready,
            "message": c_msg,
            "model_id": cat_id,
            "display_name": "CAT-Translate 1.4B (MLX q4)",
            "path": c_path or cat_id,
            "loaded_path": CAT_MODEL_ID,
            "matches_loaded": cat_matches,
            "loaded_in_process": loaded["cat_translate"] and cat_matches,
        },
        # Optional (meeting notes / Ask); never part of the top-level ``ready``.
        "notes_llm": {
            "ready": n_ready,
            "message": n_msg
            if n_ready
            else "Not downloaded (optional: meeting notes, Ask, and Chinese/Korean/English-source translation)",
            "model_id": NOTES_MODEL_ID,
            "display_name": "Qwen3 4B Instruct 2507 (MLX q4, meeting notes and non-Japanese translation)",
            "path": n_path or NOTES_MODEL_ID,
            "loaded_in_process": notes_llm.is_loaded(),
        },
        "speaker": {
            "ready": speaker_model_path().is_file(),
            "message": (
                "Ready"
                if speaker_model_path().is_file()
                else "Not downloaded (optional: speaker labels)"
            ),
            "display_name": "WeSpeaker ResNet34-LM (speaker labels)",
        },
        "vad": {
            "ready": loaded["vad"],
            "message": "Ready" if loaded["vad"] else "Not loaded",
            "display_name": "Silero VAD",
        },
    }


def _snapshot_download(model_id: str, *, force: bool = False) -> str:
    from huggingface_hub import snapshot_download

    return snapshot_download(
        repo_id=model_id,
        force_download=force,
    )


def hub_repo_sizes(repo_ids: list[str]) -> dict[str, int]:
    """Total file bytes per repo from the Hub. Raises when the Hub is unreachable."""
    from huggingface_hub import HfApi

    api = HfApi()
    return {
        repo: sum(
            s.size or 0 for s in api.model_info(repo, files_metadata=True, timeout=5).siblings
        )
        for repo in repo_ids
    }


def fetch_repos(repo_ids: list[str]) -> None:
    """Fetch repos into the HF cache, then the optional speaker model. Raises on a repo failure."""
    for repo in repo_ids:
        _snapshot_download(repo)
    _download_speaker_model()  # optional: speaker labels only, never fails the fetch


def download_models(
    settings: dict[str, Any] | None = None,
    *,
    force: bool = False,
    on_progress: Callable[[str], None] | None = None,
    only: str | None = None,
) -> dict[str, Any]:
    """Download configured model IDs into the HF cache (blocking).

    ``only="notes_llm"`` fetches just the optional meeting-notes model.
    """
    settings = settings or {}
    paths = settings.get("model_paths") if isinstance(settings.get("model_paths"), dict) else {}
    whisper_id = str((paths or {}).get("whisper") or WHISPER_MODEL)
    cat_id = str((paths or {}).get("cat_translate") or CAT_MODEL_ID)

    results: dict[str, Any] = {"whisper": None, "cat_translate": None, "errors": []}

    if only == "notes_llm":
        try:
            if on_progress:
                on_progress(f"notes_llm: downloading {NOTES_MODEL_ID}")
            results["notes_llm"] = {"path": _snapshot_download(NOTES_MODEL_ID, force=force)}
        except Exception as exc:
            results["errors"].append(f"notes_llm: {exc}")
            results["notes_llm"] = {"error": str(exc)}
        results["ok"] = not results["errors"]
        return results

    for key, model_id in (("whisper", whisper_id), ("cat_translate", cat_id)):
        if _is_local_path(model_id):
            results[key] = {"path": str(Path(model_id).resolve()), "skipped": True}
            if on_progress:
                on_progress(f"{key}: local path, skip download")
            continue
        try:
            if on_progress:
                on_progress(f"{key}: downloading {model_id}")
            path = _snapshot_download(model_id, force=force)
            results[key] = {"path": path, "skipped": False}
            if on_progress:
                on_progress(f"{key}: ready at {path}")
        except Exception as exc:
            results["errors"].append(f"{key}: {exc}")
            results[key] = {"error": str(exc)}
            if on_progress:
                on_progress(f"{key}: failed — {exc}")

    # Speaker labels are optional, so a failed speaker download never fails
    # the whole download; captions just run without speaker labels.
    results["speaker"] = _download_speaker_model(on_progress)
    results["ok"] = not results["errors"]
    return results


def _speaker_file_ok(path: Path) -> bool:
    import hashlib

    if not path.is_file() or path.stat().st_size != SPEAKER_MODEL_SIZE:
        return False
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest() == SPEAKER_MODEL_SHA256


def _download_speaker_model(on_progress: Callable[[str], None] | None = None) -> dict[str, Any]:
    """Fetch the pinned WeSpeaker ONNX (optional: speaker labels only).

    Size and SHA-256 are verified before the file is promoted to its final
    path, so a truncated or tampered download never loads.
    """
    target = speaker_model_path()
    if _speaker_file_ok(target):
        return {"path": str(target), "skipped": True}
    try:
        from huggingface_hub import hf_hub_download

        if on_progress:
            on_progress(f"speaker: downloading {SPEAKER_MODEL_REPO}")
        staging = target.parent / ".download"
        staging.mkdir(parents=True, exist_ok=True)
        fetched = Path(
            hf_hub_download(
                repo_id=SPEAKER_MODEL_REPO,
                filename=SPEAKER_MODEL_FILE,
                revision=SPEAKER_MODEL_REVISION,
                local_dir=str(staging),
            )
        )
        if not _speaker_file_ok(fetched):
            raise ValueError("checksum mismatch")
        fetched.replace(target)
        shutil.rmtree(staging, ignore_errors=True)
        return {"path": str(target), "skipped": False}
    except Exception as exc:
        return {"error": str(exc)}


def reset_cache(
    *,
    yes: bool = False,
    scoped_path: str | None = None,
) -> dict[str, Any]:
    """Remove MimiVox's own models when confirmed: the managed directory and,
    when clearing all of it, MimiVox's repos in the HuggingFace cache. Other
    repos in that cache are never touched."""
    if not yes:
        return {
            "ok": False,
            "message": (
                "Cache reset refused. Pass yes=true and a scoped path under "
                f"{MIMIVOX_MODELS_DIR} to clear MimiVox-managed models only. "
                "Global HuggingFace cache is never wiped."
            ),
        }

    target = Path(scoped_path) if scoped_path else MIMIVOX_MODELS_DIR
    managed_root = MIMIVOX_MODELS_DIR.resolve()
    try:
        resolved = target.resolve()
    except OSError as exc:
        return {"ok": False, "message": f"Invalid path: {exc}"}

    # Must be exactly managed root or a subpath of it.
    if resolved != managed_root and managed_root not in resolved.parents:
        return {
            "ok": False,
            "message": (
                f"Refusing to clear {resolved}: not under MimiVox-managed directory {managed_root}"
            ),
        }

    if get_download_state()["busy"]:
        return {
            "ok": False,
            "message": "A model download is running. Remove the models after it finishes.",
        }

    # The speech, translation and notes models live in the shared HuggingFace
    # cache, so clearing the managed root alone would free nothing.
    repos = [_hf_repo_dir(r) for r in MIMIVOX_REPOS] if resolved == managed_root else []
    targets = [t for t in [*repos, resolved] if t.exists()]
    freed = sum(_tree_size(t) for t in targets)
    for t in targets:
        t.unlink() if t.is_file() else shutil.rmtree(t)
    managed_root.mkdir(parents=True, exist_ok=True)

    removed = sum(t in targets for t in repos)
    if resolved == managed_root:
        message = (
            f"Removed {removed} model{'s' if removed != 1 else ''} ({freed / 1e9:.1f} GB)"
            if removed
            else "No downloaded models to remove"
        )
    else:
        message = f"Cleared {resolved}" if targets else f"Nothing to clear (missing): {resolved}"
    return {"ok": True, "message": message, "path": str(resolved)}


MIMIVOX_REPOS = (WHISPER_MODEL, CAT_MODEL_ID, NOTES_MODEL_ID)


def _hf_repo_dir(repo_id: str) -> Path:
    from huggingface_hub import constants

    return Path(constants.HF_HUB_CACHE) / ("models--" + repo_id.replace("/", "--"))


def _tree_size(path: Path) -> int:
    if path.is_file():
        return path.stat().st_size
    # Snapshots are symlinks into blobs/, so count real files only.
    return sum(f.stat().st_size for f in path.rglob("*") if f.is_file() and not f.is_symlink())
