import numpy as np
import pytest

from mimivox import vad


# ---------------------------------------------------------------------------
# Test fixtures
# ---------------------------------------------------------------------------
class _FakeModel:
    """Deterministic stand-in for the Silero model.

    Returns a pre-scripted probability per window so the state machine can
    be exercised without depending on real model weights. ``probs`` is a
    sequence of floats indexed by call number; out-of-range reads return 0.0
    (silence). Honours ``reset_states`` so each test starts from a known
    state, mirroring real Silero.
    """

    def __init__(self, probs):
        self._probs = list(probs)
        self._i = 0

    def __call__(self, win):
        if self._i >= len(self._probs):
            return 0.0
        p = self._probs[self._i]
        self._i += 1
        return p

    def reset_states(self):
        # Keep the script; only reset the playhead so callers can replay.
        self._i = 0


def _speech_at(probs, amp=0.5):
    """0.5 where the scripted model says speech, digital silence elsewhere."""
    return np.repeat(np.where(np.asarray(probs) >= 0.5, amp, 0.0), 512).astype(np.float32)


def _make_vad_with(probs, monkeypatch):
    """Build a SileroVAD whose model replays the given per-window probs."""
    monkeypatch.setattr(vad._models, "silero", _FakeModel(probs))
    return vad.SileroVAD()


# ---------------------------------------------------------------------------
# Behavioural tests
# ---------------------------------------------------------------------------
def test_bundled_silero_scores_silence_low_and_resets():
    model = vad.SileroOnnx()
    silence = [model(np.zeros(512, dtype=np.float32)) for _ in range(30)]
    assert max(silence) < 0.1
    model.reset_states()
    assert model(np.zeros(512, dtype=np.float32)) == pytest.approx(silence[0])


def test_trim_trailing_silence_basic():
    # Realistic shape: loud speech then a long silent tail (much bigger than
    # the ~20ms pad the trim function keeps).
    audio = np.concatenate(
        [
            np.ones(1000, dtype=np.float32) * 0.5,
            np.zeros(2000, dtype=np.float32),
        ]
    )
    out = vad._trim_trailing_silence(audio)
    assert out.size < audio.size  # silence trimmed
    assert out.size >= 1000  # speech kept
    assert out.size <= 1000 + 320 + 1  # at most ~20ms pad added


def test_trim_trailing_silence_noop_on_loud_tail():
    audio = np.array([0.5, -0.5, 0.4], dtype=np.float32)
    out = vad._trim_trailing_silence(audio)
    assert out.size == audio.size


def test_trim_trailing_silence_all_silent_returns_empty():
    audio = np.zeros(512, dtype=np.float32)
    out = vad._trim_trailing_silence(audio)
    assert out.size == 0


def test_pure_silence_emits_nothing(monkeypatch):
    # 5 seconds of windows, all silence (prob 0).
    probs = [0.0] * 200
    v = _make_vad_with(probs, monkeypatch)
    samples = np.zeros(5 * 16000, dtype=np.float32)
    assert v.feed(samples) == []
    assert v.flush() is None


def test_single_utterance_emits_one_chunk(monkeypatch):
    # 2s speech (prob 1.0) then 1s silence (prob 0.0) → one chunk.
    win_per_s = 16000 // 512  # ~31.25 windows/s
    probs = [1.0] * int(2 * win_per_s) + [0.0] * int(2 * win_per_s)
    v = _make_vad_with(probs, monkeypatch)
    # Drive feed in arbitrary slices (the design contract: deterministic
    # regardless of slicing). Send everything at once.
    samples = _speech_at(probs)
    chunks = v.feed(samples)
    assert len(chunks) == 1
    # Chunk should be at least MIN_SPEECH_S long (1.5s) and have no trailing silence.
    assert chunks[0].size >= int(1.5 * 16000)
    assert chunks[0].size < samples.size  # trailing silence trimmed
    assert v.flush() is None


def test_two_utterances_separated_by_silence(monkeypatch):
    win_per_s = 16000 // 512
    probs = (
        [1.0] * int(2 * win_per_s)  # speech 1
        + [0.0] * int(1 * win_per_s)  # gap (1s > 500ms threshold)
        + [1.0] * int(2 * win_per_s)  # speech 2
        + [0.0] * int(1 * win_per_s)  # trailing silence
    )
    v = _make_vad_with(probs, monkeypatch)
    samples = _speech_at(probs)
    chunks = v.feed(samples)
    assert len(chunks) == 2


def test_short_pause_does_not_split(monkeypatch):
    """A 300ms pause (below VAD_MIN_SILENCE_MS) must not split an utterance."""
    win_per_s = 16000 // 512
    probs = (
        [1.0] * int(2 * win_per_s)
        + [0.0] * int(0.3 * win_per_s)  # 300ms gap — below threshold
        + [1.0] * int(2 * win_per_s)
        + [0.0] * int(1 * win_per_s)
    )
    v = _make_vad_with(probs, monkeypatch)
    samples = _speech_at(probs)
    chunks = v.feed(samples)
    assert len(chunks) == 1  # not split — pause was below threshold


def test_vad_wait_reported_on_close(monkeypatch):
    """Silence-close endpointing wait is reported per emit (audio-time seconds)."""
    import mimivox.config as config

    monkeypatch.setattr(config, "VAD_MIN_SILENCE_MS", 700, raising=False)
    win_per_s = 16000 // 512
    # 2s speech then 1.2s silence — enough to cross the 700ms close threshold.
    probs = [1.0] * int(2 * win_per_s) + [0.0] * int(1.2 * win_per_s)
    v = _make_vad_with(probs, monkeypatch)
    samples = _speech_at(probs)
    chunks = v.feed(samples)
    assert len(chunks) == 1
    assert len(v.last_emits_vad_wait) == 1
    wait = v.last_emits_vad_wait[0]
    # min_sil_windows = int(16000*700/1000/512) = 21 -> 21*512/16000 = 0.672s
    assert 0.5 <= wait <= 0.8
    # flush emits nothing here; wait should be 0.
    assert v.flush() is None
    assert v.last_flush_vad_wait == 0.0


def test_vad_wait_empty_when_no_emit(monkeypatch):
    """Pure-silence feed emits nothing and reports an empty wait list."""
    probs = [0.0] * 200
    v = _make_vad_with(probs, monkeypatch)
    samples = np.zeros(5 * 16000, dtype=np.float32)
    assert v.feed(samples) == []
    assert v.last_emits_vad_wait == []


def test_short_pause_does_not_double_append_silence(monkeypatch):
    """Brief mid-utterance silence must be inserted once, not twice."""
    import mimivox.config as config

    win_per_s = 16000 // 512
    speech_w = int(2 * win_per_s)
    pause_w = max(1, int(0.3 * win_per_s))
    probs = [1.0] * speech_w + [0.0] * pause_w + [1.0] * speech_w + [0.0] * int(1.2 * win_per_s)
    v = _make_vad_with(probs, monkeypatch)
    samples = _speech_at(probs)
    chunks = v.feed(samples)
    assert len(chunks) == 1
    # speech + one copy of pause (+ small pads/trim slack), never 2× pause.
    expected_max = (2 * speech_w + pause_w) * 512 + int(0.25 * 16000)
    assert chunks[0].size <= expected_max
    # Sanity: still contains both speech halves + the pause once.
    expected_min = (2 * speech_w + pause_w) * 512 - int(0.05 * 16000)
    assert chunks[0].size >= expected_min
    assert config.VAD_MIN_SILENCE_MS > 300


def test_onset_pad_applied_when_silence_precedes_speech(monkeypatch):
    """Leading silence pre-roll should pad the start of an utterance."""
    import mimivox.config as config

    win_per_s = 16000 // 512
    lead_w = int(0.5 * win_per_s)
    speech_w = int(2 * win_per_s)
    probs = [0.0] * lead_w + [1.0] * speech_w + [0.0] * int(1.2 * win_per_s)
    v = _make_vad_with(probs, monkeypatch)
    samples = _speech_at(probs)
    # Make leading region actually silent so trim does not keep it as "speech".
    samples[: lead_w * 512] = 0.0
    chunks = v.feed(samples)
    assert len(chunks) == 1
    pad = int(16000 * config.VAD_SPEECH_PAD_MS / 1000)
    # With zero preroll content the pad is silence and may be trimmed from the
    # head only if we trimmed leading silence — we keep onset pad, so size
    # should be at least speech plus some pad (trailing trim leaves ~20ms).
    assert chunks[0].size >= speech_w * 512
    if pad > 0:
        # Onset zeros are present at the start before any loud sample.
        first_loud = int(np.flatnonzero(np.abs(chunks[0]) >= 0.01)[0])
        assert first_loud > 0
        assert first_loud <= pad + 512


def test_too_short_utterance_dropped(monkeypatch):
    """Utterance shorter than MIN_SPEECH_S (0.6s) should be dropped.

    With the constant-amplitude fixture, _trim_trailing_silence is a no-op,
    so the emitted chunk length ≈ speech windows + min_sil_windows. We use
    1 speech window + ~16 silence windows = ~8704 samples < 0.6s·16000.
    """
    win_per_s = 16000 // 512
    probs = [1.0] * 1 + [0.0] * int(1 * win_per_s)
    v = _make_vad_with(probs, monkeypatch)
    samples = _speech_at(probs)
    assert v.feed(samples) == []


def test_flush_emits_pending_at_end_of_stream(monkeypatch):
    """If audio ends mid-utterance (no trailing silence), flush() emits it."""
    win_per_s = 16000 // 512
    probs = [0.0] * 4 + [1.0] * int(2 * win_per_s)  # speech, no closing silence
    v = _make_vad_with(probs, monkeypatch)
    samples = _speech_at(probs)
    assert v.feed(samples) == []  # nothing closed yet
    final = v.flush()
    assert final is not None
    assert final.size >= int(1.5 * 16000)


def test_chunking_invariant_to_feed_slicing(monkeypatch):
    """Same audio fed in different slice sizes must produce identical chunks."""
    win_per_s = 16000 // 512
    probs = (
        [1.0] * int(2 * win_per_s)
        + [0.0] * int(1 * win_per_s)
        + [1.0] * int(2 * win_per_s)
        + [0.0] * int(1 * win_per_s)
    )
    samples = _speech_at(probs)

    # Run A: one big feed.
    v_a = _make_vad_with(probs, monkeypatch)
    chunks_a = v_a.feed(samples)

    # Run B: many small feeds.
    v_b = _make_vad_with(probs, monkeypatch)
    chunks_b = []
    for i in range(0, len(samples), 512):  # 32ms slices
        chunks_b.extend(v_b.feed(samples[i : i + 512]))

    assert len(chunks_a) == len(chunks_b)
    for a, b in zip(chunks_a, chunks_b):
        assert a.size == b.size


# ---------------------------------------------------------------------------
# Input level: quiet speakers vs. noise
# ---------------------------------------------------------------------------
_WPS = 16000 // 512


def _dbfs(db: float) -> float:
    return 10 ** (db / 20)


def _scene(speech_db: float, noise_db: float, seed: int = 0) -> tuple[np.ndarray, list[float]]:
    """1 s noise, 2 s of a 220 Hz tone over the noise, 1 s noise; probs mark the tone."""
    rng = np.random.default_rng(seed)
    lead, speech, tail = _WPS, 2 * _WPS, _WPS
    n = (lead + speech + tail) * 512
    audio = rng.standard_normal(n) * _dbfs(noise_db)
    t = np.arange(speech * 512) / 16000
    audio[lead * 512 : (lead + speech) * 512] += (
        np.sin(2 * np.pi * 220 * t) * _dbfs(speech_db) * np.sqrt(2)
    )
    probs = [0.0] * lead + [1.0] * speech + [0.0] * tail
    return audio.astype(np.float32), probs


def _run(audio: np.ndarray, probs: list[float], monkeypatch) -> list[np.ndarray]:
    v = _make_vad_with(probs, monkeypatch)
    chunks = v.feed(audio)
    if (final := v.flush()) is not None:
        chunks.append(final)
    return chunks


def _rms(x: np.ndarray) -> float:
    return float(np.sqrt(np.mean(np.square(x, dtype=np.float64))))


def test_quiet_speaker_is_emitted_at_speech_level(monkeypatch):
    """A -60 dBFS speaker in a quiet room yields one chunk, raised to normal speech level."""
    audio, probs = _scene(speech_db=-60, noise_db=-85)
    chunks = _run(audio, probs, monkeypatch)
    assert len(chunks) == 1
    assert chunks[0].size >= int(1.8 * 16000)
    assert _rms(chunks[0]) > 0.05


def test_speaker_above_target_level_is_not_attenuated(monkeypatch):
    audio, probs = _scene(speech_db=-14, noise_db=-70)
    chunks = _run(audio, probs, monkeypatch)
    assert len(chunks) == 1
    assert np.max(np.abs(chunks[0])) == pytest.approx(np.max(np.abs(audio)), rel=1e-3)


@pytest.mark.parametrize("noise_db", [-85, -60, -45, -30])
def test_noise_the_model_mistakes_for_speech_is_not_emitted(monkeypatch, noise_db):
    """A VAD false-open on steady noise must not become a (boosted) chunk."""
    rng = np.random.default_rng(1)
    probs = [0.0] * _WPS + [1.0] * (2 * _WPS) + [0.0] * _WPS
    audio = (rng.standard_normal(len(probs) * 512) * _dbfs(noise_db)).astype(np.float32)
    assert _run(audio, probs, monkeypatch) == []


def test_speech_below_the_absolute_floor_is_not_emitted(monkeypatch):
    """-80 dBFS is a few int16 LSBs: gain would only lift quantization noise."""
    audio, probs = _scene(speech_db=-80, noise_db=-120)
    assert _run(audio, probs, monkeypatch) == []


class _LevelMemoryModel:
    """Scores a window as speech only if it is within 20 dB of the loudest window
    heard since the last reset, the way Silero's recurrent state behaves."""

    def __init__(self):
        self._peak = 0.0

    def __call__(self, win):
        rms = _rms(win)
        self._peak = max(self._peak, rms)
        return 1.0 if rms > 0 and rms >= self._peak / 10 else 0.0

    def reset_states(self):
        self._peak = 0.0


def test_quiet_reply_after_loud_speaker_is_emitted(monkeypatch):
    _make_vad_with([], monkeypatch)
    vad._models.silero = _LevelMemoryModel()
    v = vad.SileroVAD()
    t = np.arange(2 * 16000) / 16000
    tone = np.sin(2 * np.pi * 220 * t) * np.sqrt(2)
    gap = np.zeros(16000)
    audio = np.concatenate([tone * _dbfs(-20), gap, tone * _dbfs(-60), gap]).astype(np.float32)
    chunks = v.feed(audio)
    assert len(chunks) == 2
    assert _rms(chunks[1]) > 0.05
