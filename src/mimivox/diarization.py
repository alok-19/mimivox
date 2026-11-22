"""mimivox.diarization — On-device speaker labels for caption lines.

``SpeakerTracker`` is one per session. ``identify`` turns each VAD utterance
into a ``Speaker`` (``speaker-N`` / ``Speaker N``):

* ``WeSpeakerEmbedder`` runs the WeSpeaker ResNet34-LM ONNX model on CPU
  (onnxruntime) with its own Kaldi-style fbank, so it never touches the MLX
  lock and costs no GPU memory.
* ``SpeakerClusterer`` is online cosine clustering over running centroids.

If the model or onnxruntime is missing, ``SpeakerTracker.create`` returns
``None`` and captions simply carry no speaker field. A runtime embedding
failure disables the tracker for the rest of the session the same way.

Model: WeSpeaker ResNet34-LM (CC-BY-4.0), see THIRD_PARTY_NOTICES.md.
"""

from __future__ import annotations

import math
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from mimivox.config import DATA_DIR, SAMPLE_RATE

SPEAKER_MODEL_REPO = "Wespeaker/wespeaker-voxceleb-resnet34-LM"
SPEAKER_MODEL_REVISION = "f0c48c298fd835726c27956a5d617bad7115627e"
SPEAKER_MODEL_FILE = "voxceleb_resnet34_LM.onnx"
SPEAKER_MODEL_SIZE = 26_530_309
SPEAKER_MODEL_SHA256 = "7bb2f06e9df17cdf1ef14ee8a15ab08ed28e8d0ef5054ee135741560df2ec068"

MIN_EMBED_S = 0.6  # shorter clips give unreliable embeddings
MAX_EMBED_S = 10.0  # cap CPU cost; the first 10 s of an utterance is plenty

_TOO_SHORT = "clip too short"


class SpeakerUnavailable(RuntimeError):
    """Embedding model missing, unloadable, or returned garbage."""


@dataclass(frozen=True)
class Speaker:
    id: str
    label: str

    def as_dict(self) -> dict[str, str]:
        return {"id": self.id, "label": self.label}


class SpeakerClusterer:
    """Online cosine clustering with running centroids.

    At or above ``match`` the utterance joins the nearest cluster and refines
    its centroid; at or below ``new`` it opens a new cluster (up to
    ``max_speakers``); in between it joins the nearest cluster without moving
    the centroid (too uncertain to learn from).
    """

    def __init__(
        self,
        match: float = 0.65,
        new: float = 0.45,
        max_speakers: int = 8,
    ) -> None:
        if not 0.0 <= new <= match <= 1.0:
            raise ValueError("need 0 <= new <= match <= 1")
        if max_speakers < 1:
            raise ValueError("max_speakers must be positive")
        self.match = match
        self.new = new
        self.max_speakers = max_speakers
        self._centroids: list[np.ndarray] = []  # unit-norm
        self._counts: list[int] = []

    def __len__(self) -> int:
        return len(self._centroids)

    def assign(self, vector: Any) -> tuple[int, float]:
        """Return (zero-based cluster index, cosine to its centroid)."""
        v = np.asarray(vector, dtype=np.float32)
        norm = float(np.linalg.norm(v))
        if v.ndim != 1 or not v.size or not math.isfinite(norm) or norm <= 0:
            raise SpeakerUnavailable("malformed embedding")
        v = v / norm
        if not self._centroids:
            return self._create(v), 1.0
        scores = [float(np.dot(v, c)) for c in self._centroids]
        best = int(np.argmax(scores))
        score = scores[best]
        if score >= self.match:
            c = self._centroids[best] * self._counts[best] + v
            self._centroids[best] = c / np.linalg.norm(c)
            self._counts[best] += 1
        elif score <= self.new and len(self._centroids) < self.max_speakers:
            return self._create(v), 1.0
        return best, score

    def _create(self, v: np.ndarray) -> int:
        self._centroids.append(v.copy())
        self._counts.append(1)
        return len(self._centroids) - 1


class WeSpeakerEmbedder:
    """Lazy CPU ONNX runtime. Thread-safe; one inference at a time."""

    def __init__(self, model_path: Path) -> None:
        self._model_path = Path(model_path)
        self._session: Any | None = None
        self._lock = threading.Lock()

    def _ensure_session(self) -> Any:
        if self._session is None:
            try:
                import onnxruntime as ort

                opts = ort.SessionOptions()
                opts.intra_op_num_threads = 2
                opts.inter_op_num_threads = 1
                self._session = ort.InferenceSession(
                    str(self._model_path),
                    sess_options=opts,
                    providers=["CPUExecutionProvider"],
                )
            except Exception as exc:
                raise SpeakerUnavailable(f"model load failed: {exc}") from exc
        return self._session

    def embed(self, audio: np.ndarray) -> np.ndarray:
        """Embed mono float32 16 kHz audio in [-1, 1] to a unit 256-d vector."""
        samples = np.asarray(audio, dtype=np.float32).reshape(-1)
        if len(samples) < int(MIN_EMBED_S * SAMPLE_RATE):
            raise SpeakerUnavailable(_TOO_SHORT)
        feats = fbank(samples[: int(MAX_EMBED_S * SAMPLE_RATE)])
        with self._lock:
            session = self._ensure_session()
            try:
                out = session.run(["embs"], {"feats": feats})[0][0]
            except Exception as exc:
                raise SpeakerUnavailable(f"inference failed: {exc}") from exc
        vec = np.asarray(out, dtype=np.float32)
        norm = float(np.linalg.norm(vec))
        if vec.shape != (256,) or not np.all(np.isfinite(vec)) or norm <= 0:
            raise SpeakerUnavailable("malformed embedding")
        return vec / norm


_MEL: np.ndarray | None = None


def _mel_filters(sample_rate: int = SAMPLE_RATE, fft_size: int = 512, bins: int = 80) -> np.ndarray:
    global _MEL
    if _MEL is not None:
        return _MEL
    low_mel = 2595 * math.log10(1 + 20 / 700)
    high_mel = 2595 * math.log10(1 + (sample_rate / 2) / 700)
    hz = 700 * (10 ** (np.linspace(low_mel, high_mel, bins + 2) / 2595) - 1)
    fft_bins = np.floor((fft_size + 1) * hz / sample_rate).astype(int)
    filters = np.zeros((bins, fft_size // 2 + 1), dtype=np.float32)
    for i in range(bins):
        left, center, right = fft_bins[i : i + 3]
        if center > left:
            filters[i, left:center] = (np.arange(left, center) - left) / (center - left)
        if right > center:
            filters[i, center:right] = (right - np.arange(center, right)) / (right - center)
    _MEL = filters
    return filters


def fbank(samples: np.ndarray, sample_rate: int = SAMPLE_RATE) -> np.ndarray:
    """80-bin log-mel filterbank, shape [1, frames, 80], mean-normalised."""
    wave = np.asarray(samples, dtype=np.float32) * (1 << 15)
    frame_len = round(sample_rate * 0.025)
    shift = round(sample_rate * 0.010)
    if wave.size < frame_len:
        wave = np.pad(wave, (0, frame_len - wave.size))
    n = 1 + (wave.size - frame_len) // shift
    idx = np.arange(frame_len)[None, :] + np.arange(n)[:, None] * shift
    frames = wave[idx].copy()
    frames -= frames.mean(axis=1, keepdims=True)
    frames[:, 1:] -= 0.97 * frames[:, :-1]
    frames *= np.hamming(frame_len).astype(np.float32)
    power = np.abs(np.fft.rfft(frames, n=512)) ** 2
    feats = np.log(np.maximum(power @ _mel_filters().T, np.finfo(np.float32).eps))
    feats -= feats.mean(axis=0, keepdims=True)
    return feats.astype(np.float32, copy=False)[None, :, :]


def speaker_model_path() -> Path:
    return Path(DATA_DIR) / "models" / "wespeaker" / SPEAKER_MODEL_FILE


class SpeakerTracker:
    """Per-session speaker assignment: audio in, ``Speaker`` out.

    ``identify`` is blocking CPU work; call it from an executor. Names are
    session-local: ``rename`` changes the label of an id for later lines.
    """

    def __init__(self, embedder: Any, clusterer: SpeakerClusterer | None = None) -> None:
        self._embedder = embedder
        self._clusterer = clusterer or SpeakerClusterer()
        self._names: dict[str, str] = {}
        self._last: Speaker | None = None
        self._lock = threading.Lock()
        self.disabled_reason: str | None = None
        self.last_embed_ms: float = 0.0

    @classmethod
    def create(cls, model_path: Path | None = None) -> SpeakerTracker | None:
        """Return a tracker, or None when the model/runtime is unavailable."""
        path = Path(model_path) if model_path else speaker_model_path()
        if not path.is_file():
            return None
        try:
            import onnxruntime  # noqa: F401
        except ImportError:
            return None
        return cls(WeSpeakerEmbedder(path))

    def _speaker(self, index: int) -> Speaker:
        sid = f"speaker-{index + 1}"
        return Speaker(sid, self._names.get(sid, f"Speaker {index + 1}"))

    def identify(self, audio: np.ndarray) -> Speaker | None:
        """Speaker for one utterance; None when unavailable.

        A clip too short to embed inherits the previous speaker (a one-word
        reply is likelier to continue a turn than to be a new voice).
        """
        if self.disabled_reason is not None:
            return None
        started = time.perf_counter()
        try:
            vec = self._embedder.embed(audio)
        except SpeakerUnavailable as exc:
            if str(exc) == _TOO_SHORT:
                with self._lock:
                    return self._last
            self.disabled_reason = str(exc)
            return None
        except Exception as exc:  # never let diarization break captions
            self.disabled_reason = f"unexpected: {exc}"
            return None
        self.last_embed_ms = (time.perf_counter() - started) * 1000.0
        with self._lock:
            try:
                index, _ = self._clusterer.assign(vec)
            except SpeakerUnavailable as exc:
                self.disabled_reason = str(exc)
                return None
            self._last = self._speaker(index)
            return self._last

    def rename(self, speaker_id: str, label: str) -> bool:
        """Rename a known speaker id. False if the id is unknown or label empty."""
        label = label.strip()
        if not label:
            return False
        with self._lock:
            known = {f"speaker-{i + 1}" for i in range(len(self._clusterer))}
            if speaker_id not in known:
                return False
            self._names[speaker_id] = label
            if self._last is not None and self._last.id == speaker_id:
                self._last = Speaker(speaker_id, label)
        return True
