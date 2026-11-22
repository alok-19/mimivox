"""mimivox.vad — Stateful streaming utterance segmentation via Silero VAD.

Runs Silero in native streaming mode — one inference per 512-sample (32ms)
window — and drives a speech⇄silence state machine. Each window is processed
exactly once so segmentation is deterministic regardless of feed() slicing.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np

import mimivox.config as config
from mimivox.config import SAMPLE_RATE, _models

# Silero's native inference window: 512 samples = 32ms at 16kHz.
_WIN = 512

# Below this absolute amplitude a sample is treated as silence regardless of
# the model's probability output. Used for trimming trailing silence off
# emitted chunks (fixes Whisper's loop-hallucination-on-trailing-silence).
# Chunks are raised to _TARGET_RMS first, so the floor is relative to speech.
_AMP_FLOOR = 0.01

# Silero fires on speech from -20 down to about -60 dBFS, so the VAD runs on
# the raw signal. Each emitted chunk is then raised (never lowered) to
# _TARGET_RMS for Whisper, unless it is too quiet to trust: below -70 dBFS
# (a few int16 LSBs) or under 3 dB above the running noise floor, which is
# what a false open on steady noise or hum looks like. FLEURS speech in its
# noisiest recordings sits 6 dB over the floor; steady noise sits under 1 dB.
_TARGET_RMS = 0.1
_MIN_EMIT_RMS = 10 ** (-70 / 20)
_MIN_EMIT_SNR = 10 ** (3 / 20)
_NOISE_FLOOR_MIN = 1e-5
# Per window: the floor drops to any quieter window at once and creeps up
# 3 dB/s, so it follows a room that gets louder without riding up on speech.
_NOISE_FLOOR_RISE = 10 ** (3 / 20 * _WIN / SAMPLE_RATE)


def _trim_trailing_silence(audio: np.ndarray, floor: float = _AMP_FLOOR) -> np.ndarray:
    """Drop samples from the tail whose absolute value falls below ``floor``.

    Keeps a small (~20ms) pad after the last loud sample so the chunk
    doesn't end on an abrupt boundary, but never pads with more samples
    than actually remain in the buffer.
    """
    if audio.size == 0:
        return audio
    above = np.abs(audio) >= floor
    if not above.any():
        return audio[:0]
    last = int(np.flatnonzero(above)[-1])
    pad = min(SAMPLE_RATE // 50, audio.size - last - 1)  # cap at ~20ms / available
    end = min(audio.size, last + 1 + pad)
    return audio[:end]


def _rms(audio: np.ndarray) -> float:
    if audio.size == 0:
        return 0.0
    return float(np.sqrt(np.mean(np.square(audio, dtype=np.float64))))


class SileroVAD:
    """Streaming utterance segmenter backed by Silero in windowed mode."""

    def __init__(self) -> None:
        self._model = _models.silero
        self._model_reset()

        # Pending audio not yet aligned to a window boundary.
        self._tail: np.ndarray = np.array([], dtype=np.float32)

        # State machine -------------------------------------------------
        # Modes: "silence" (waiting for speech) or "speech" (accumulating an
        # utterance, possibly already inside a trailing-silence run).
        self._mode: str = "silence"
        self._speech_buf: np.ndarray = np.array([], dtype=np.float32)

        # Trailing-silence run inside an active utterance. Kept separately so
        # brief pauses can be re-absorbed without double-appending into
        # _speech_buf.
        self._sil_buf: np.ndarray = np.array([], dtype=np.float32)
        self._sil_windows: int = 0

        # Consecutive above-threshold windows before opening an utterance.
        self._speech_windows: int = 0
        self._pending_open: np.ndarray = np.array([], dtype=np.float32)

        # Rolling pre-roll of recent non-speech audio used as onset pad.
        self._preroll: np.ndarray = np.array([], dtype=np.float32)

        self._noise_floor: float = float("inf")

        # Per-emit endpointing diagnostics (audio-time seconds the VAD held
        # the utterance before emitting). Aligned to the list returned by
        # feed(); last_flush_vad_wait covers the single flush() emit.
        # silence-close wait = sil_windows * _WIN / SAMPLE_RATE at close time.
        self.last_emits_vad_wait: list[float] = []
        self.last_flush_vad_wait: float = 0.0

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _model_reset(self) -> None:
        if self._model is not None and hasattr(self._model, "reset_states"):
            self._model.reset_states()

    def _win_prob(self, win: np.ndarray) -> float:
        return self._model(win.astype(np.float32, copy=False))

    def _pad_samples(self) -> int:
        return max(0, int(SAMPLE_RATE * config.VAD_SPEECH_PAD_MS / 1000))

    def _push_preroll(self, win: np.ndarray) -> None:
        pad = self._pad_samples()
        if pad <= 0:
            self._preroll = np.array([], dtype=np.float32)
            return
        buf = np.concatenate([self._preroll, win]) if self._preroll.size else win.copy()
        if buf.size > pad:
            buf = buf[-pad:]
        self._preroll = buf

    def _track_noise_floor(self, win: np.ndarray) -> None:
        rms = max(_rms(win), _NOISE_FLOOR_MIN)
        self._noise_floor = min(rms, self._noise_floor * _NOISE_FLOOR_RISE)

    def _emit(self, chunk: np.ndarray) -> np.ndarray | None:
        """Apply post-emit rules: level gate, gain, trim, length; None if filtered."""
        rms = _rms(chunk)
        if rms < max(_MIN_EMIT_RMS, self._noise_floor * _MIN_EMIT_SNR):
            return None
        peak = float(np.max(np.abs(chunk)))
        gain = max(1.0, min(_TARGET_RMS / rms, 0.99 / peak))
        chunk = _trim_trailing_silence(chunk * np.float32(gain))
        if chunk.size < int(SAMPLE_RATE * config.MIN_SPEECH_S):
            return None
        return chunk

    def _maybe_split_and_emit(self, buf: np.ndarray) -> list[np.ndarray]:
        """Honour MAX_EMIT_S by splitting an oversized utterance."""
        max_emit = int(SAMPLE_RATE * config.MAX_EMIT_S)
        out: list[np.ndarray] = []
        for offset in range(0, len(buf), max_emit):
            sub = buf[offset : offset + max_emit]
            emitted = (
                self._emit(sub) if sub.size >= int(SAMPLE_RATE * config.MIN_SPEECH_S) else None
            )
            if emitted is not None:
                out.append(emitted)
        return out

    def _close_utterance(self) -> list[np.ndarray]:
        """Close the active utterance, attaching a short trailing pad from silence."""
        pad = self._pad_samples()
        chunk = self._speech_buf
        if pad > 0 and self._sil_buf.size:
            chunk = np.concatenate([chunk, self._sil_buf[: min(pad, self._sil_buf.size)]])
        emitted: list[np.ndarray] = []
        result = self._emit(chunk)
        if result is not None:
            if result.size > int(SAMPLE_RATE * config.MAX_EMIT_S):
                emitted.extend(self._maybe_split_and_emit(result))
            else:
                emitted.append(result)
        self._speech_buf = np.array([], dtype=np.float32)
        self._sil_buf = np.array([], dtype=np.float32)
        self._sil_windows = 0
        self._speech_windows = 0
        self._pending_open = np.array([], dtype=np.float32)
        self._mode = "silence"
        self._preroll = np.array([], dtype=np.float32)
        # Silero's recurrent state remembers the level of the speech it just
        # heard: after a loud speaker it scores a -60 dBFS reply as silence.
        self._model_reset()
        return emitted

    # ------------------------------------------------------------------
    # Public API (unchanged contract)
    # ------------------------------------------------------------------
    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        """Consume ``samples`` and return any utterances that closed.

        Each call may emit zero, one, or several utterances. Callers (the
        WAV and live mic feeds) slice input however they like; this method
        processes every 512-sample window exactly once, so chunking is
        deterministic across runs.
        """
        if self._model is None:
            raise RuntimeError("SileroVAD used before load_vad_model()")

        buf = np.concatenate([self._tail, samples.astype(np.float32, copy=False)])
        n_full = (buf.size // _WIN) * _WIN
        if n_full == 0:
            self._tail = buf
            return []

        windows = buf[:n_full].reshape(-1, _WIN)
        self._tail = buf[n_full:]

        emitted: list[np.ndarray] = []
        waits: list[float] = []
        min_open_windows = max(1, int(SAMPLE_RATE * config.VAD_MIN_SPEECH_MS / 1000 / _WIN))
        min_sil_windows = max(1, int(SAMPLE_RATE * config.VAD_MIN_SILENCE_MS / 1000 / _WIN))

        for win in windows:
            self._track_noise_floor(win)
            prob = self._win_prob(win)
            is_speech = prob >= config.VAD_THRESHOLD

            if self._mode == "silence":
                if is_speech:
                    self._speech_windows += 1
                    self._pending_open = (
                        np.concatenate([self._pending_open, win])
                        if self._pending_open.size
                        else win.copy()
                    )
                    if self._speech_windows >= min_open_windows:
                        # Open with onset pad from recent silence/pre-roll.
                        pad = self._preroll
                        self._mode = "speech"
                        parts = [p for p in (pad, self._pending_open) if p.size]
                        self._speech_buf = (
                            np.concatenate(parts) if parts else np.array([], dtype=np.float32)
                        )
                        self._sil_buf = np.array([], dtype=np.float32)
                        self._sil_windows = 0
                        self._pending_open = np.array([], dtype=np.float32)
                        self._preroll = np.array([], dtype=np.float32)
                else:
                    # Reset partial open and keep rolling pre-roll.
                    self._speech_windows = 0
                    self._pending_open = np.array([], dtype=np.float32)
                    self._push_preroll(win)
                continue

            # mode == "speech"
            if is_speech:
                # Speech resumed: re-absorb pending silence once, then append.
                if self._sil_buf.size:
                    self._speech_buf = np.concatenate([self._speech_buf, self._sil_buf])
                    self._sil_buf = np.array([], dtype=np.float32)
                    self._sil_windows = 0
                self._speech_buf = np.concatenate([self._speech_buf, win])
                if self._speech_buf.size >= int(SAMPLE_RATE * config.MAX_EMIT_S):
                    # Mid-utterance cap split — no silence close, zero wait.
                    split = self._maybe_split_and_emit(self._speech_buf)
                    emitted.extend(split)
                    waits.extend([0.0] * len(split))
                    self._speech_buf = np.array([], dtype=np.float32)
            else:
                # Do NOT also append silence into _speech_buf (avoids double-insert
                # when speech resumes and sil_buf is re-absorbed).
                self._sil_buf = np.concatenate([self._sil_buf, win])
                self._sil_windows += 1
                if self._sil_windows >= min_sil_windows:
                    # Silence won: close the utterance. Endpointing wait is the
                    # audio-time silence held back before the close decision.
                    sil_wait = self._sil_windows * _WIN / float(SAMPLE_RATE)
                    closed = self._close_utterance()
                    emitted.extend(closed)
                    waits.extend([sil_wait] * len(closed))

        self.last_emits_vad_wait = waits
        return emitted

    def flush(self) -> np.ndarray | None:
        """Emit any pending utterance at end-of-stream. Returns at most one."""
        if self._mode != "speech" or self._speech_buf.size == 0:
            self._speech_buf = np.array([], dtype=np.float32)
            self._sil_buf = np.array([], dtype=np.float32)
            self._pending_open = np.array([], dtype=np.float32)
            self._mode = "silence"
            self.last_flush_vad_wait = 0.0
            return None
        self.last_flush_vad_wait = self._sil_windows * _WIN / float(SAMPLE_RATE)
        closed = self._close_utterance()
        return closed[0] if closed else None


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------
class SileroOnnx:
    """Silero VAD (silero_vad.onnx from silero-vad 6.2.1) on onnxruntime: one
    512-sample 16 kHz window in, P(speech) out.

    Carries the recurrent state and the 64-sample context that the model expects
    to be prepended to each window, as silero_vad's own OnnxWrapper does.
    """

    _MODEL = Path(__file__).parent / "silero" / "silero_vad.onnx"
    _CONTEXT = 64

    def __init__(self) -> None:
        import onnxruntime as ort

        opts = ort.SessionOptions()
        opts.inter_op_num_threads = 1
        opts.intra_op_num_threads = 1
        self._session = ort.InferenceSession(
            str(self._MODEL), providers=["CPUExecutionProvider"], sess_options=opts
        )
        self._sr = np.array(SAMPLE_RATE, dtype=np.int64)
        self.reset_states()

    def reset_states(self) -> None:
        self._state = np.zeros((2, 1, 128), dtype=np.float32)
        self._context = np.zeros((1, self._CONTEXT), dtype=np.float32)

    def __call__(self, win: np.ndarray) -> float:
        x = np.concatenate([self._context, win.reshape(1, -1)], axis=1)
        out, self._state = self._session.run(
            None, {"input": x, "state": self._state, "sr": self._sr}
        )
        self._context = x[:, -self._CONTEXT :]
        return float(out[0, 0])


def load_vad_model() -> None:
    """Load Silero VAD into the global model state."""
    import time

    print("⏳ Silero VAD : loading...")
    t0 = time.perf_counter()
    _models.silero = SileroOnnx()
    print(f"   ✅ {time.perf_counter() - t0:.2f}s")
