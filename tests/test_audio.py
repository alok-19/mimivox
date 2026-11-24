import asyncio
import shutil
import wave
from pathlib import Path

import numpy as np
import pytest

from mimivox.audio import (
    PushAudioSource,
    RingBuffer,
    _load_wav_mono16k,
    _rms_level,
    enqueue_asr,
    feed_live,
    feed_wav,
)
from mimivox.config import SAMPLE_RATE
from mimivox.session_control import SessionControl


def test_ringbuffer_write_and_read():
    rb = RingBuffer(capacity_s=1.0)
    samples = np.ones(SAMPLE_RATE // 2, dtype=np.float32)
    rb.write(samples)
    assert rb.available == len(samples)
    data = rb.read_all()
    assert len(data) == len(samples)
    assert np.allclose(data, 1.0)
    assert rb.available == 0


def test_ringbuffer_overwrite():
    rb = RingBuffer(capacity_s=0.5)
    samples = np.ones(SAMPLE_RATE, dtype=np.float32)
    rb.write(samples)
    assert rb.available == int(0.5 * SAMPLE_RATE)


def test_rms_level_silence_and_loud():
    silence = np.zeros(1000, dtype=np.float32)
    loud = np.ones(1000, dtype=np.float32) * 0.2
    assert _rms_level(silence) == 0.0
    assert 0.0 < _rms_level(loud) <= 1.0
    assert _rms_level(np.array([], dtype=np.float32)) == 0.0


def test_load_wav_mono_mixes_16k_stereo(tmp_path):
    """16 kHz stereo must be averaged to mono, not read as interleaved mono."""
    n = SAMPLE_RATE  # 1s
    left = np.ones(n, dtype=np.float32) * 0.5
    right = np.ones(n, dtype=np.float32) * -0.5
    interleaved = np.empty(n * 2, dtype=np.float32)
    interleaved[0::2] = left
    interleaved[1::2] = right
    pcm = (interleaved * 32767.0).astype(np.int16)
    path = tmp_path / "stereo16k.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())
    audio = _load_wav_mono16k(str(path))
    assert audio.ndim == 1
    assert len(audio) == n
    assert np.allclose(audio, 0.0, atol=1e-3)


@pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg is required for conversion")
def test_load_wav_resamples_to_16k_and_cleans_up(tmp_path, monkeypatch):
    monkeypatch.setattr("tempfile.tempdir", str(tmp_path))
    path = tmp_path / "input.wav"
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(8000)
        wf.writeframes(np.zeros(8000, dtype="<i2").tobytes())

    assert _load_wav_mono16k(str(path)).shape == (SAMPLE_RATE,)
    assert list(tmp_path.iterdir()) == [path]


def _write_sine_wav(path: Path, duration_s: float, freq: float = 440.0) -> None:
    n = int(duration_s * SAMPLE_RATE)
    t = np.arange(n, dtype=np.float32) / SAMPLE_RATE
    samples = (0.2 * np.sin(2 * np.pi * freq * t)).astype(np.float32)
    pcm = (samples * 32767.0).astype(np.int16)
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(SAMPLE_RATE)
        wf.writeframes(pcm.tobytes())


class _PassThroughVAD:
    """Minimal VAD stand-in: emits one utterance per feed of non-empty audio."""

    def __init__(self) -> None:
        self.fed_samples = 0

    def feed(self, samples: np.ndarray) -> list[np.ndarray]:
        self.fed_samples += len(samples)
        if len(samples) == 0:
            return []
        return [samples.copy()]

    def flush(self) -> np.ndarray | None:
        return None


@pytest.mark.asyncio
async def test_feed_wav_max_duration_emits_fewer_chunks(tmp_path, monkeypatch):
    """max_duration_s=1.0 on a 3s WAV feeds ~1s of audio, not the full file."""
    wav_path = tmp_path / "synthetic.wav"
    _write_sine_wav(wav_path, duration_s=3.0)

    full_vad = _PassThroughVAD()
    short_vad = _PassThroughVAD()
    instances = iter([full_vad, short_vad])

    monkeypatch.setattr("mimivox.audio.SileroVAD", lambda: next(instances))

    full_q: asyncio.Queue = asyncio.Queue()
    await feed_wav(str(wav_path), full_q, session_start=0.0, max_duration_s=None, realtime=False)
    full_chunks = []
    while True:
        item = await full_q.get()
        if item is None:
            break
        full_chunks.append(item[0])

    short_q: asyncio.Queue = asyncio.Queue()
    await feed_wav(str(wav_path), short_q, session_start=0.0, max_duration_s=1.0, realtime=False)
    short_chunks = []
    while True:
        item = await short_q.get()
        if item is None:
            break
        short_chunks.append(item[0])

    full_samples = sum(len(c) for c in full_chunks)
    short_samples = sum(len(c) for c in short_chunks)
    assert full_samples == pytest.approx(3.0 * SAMPLE_RATE, rel=0.02)
    assert short_samples == pytest.approx(1.0 * SAMPLE_RATE, rel=0.02)
    assert short_samples < full_samples
    assert short_vad.fed_samples < full_vad.fed_samples


@pytest.mark.asyncio
async def test_feed_wav_realtime_paces_near_audio_duration(tmp_path, monkeypatch):
    """realtime=True should take ~audio length in wall time (not dump instantly)."""
    import time

    wav_path = tmp_path / "pace.wav"
    _write_sine_wav(wav_path, duration_s=0.4)

    monkeypatch.setattr("mimivox.audio.SileroVAD", _PassThroughVAD)

    q: asyncio.Queue = asyncio.Queue()
    t0 = time.perf_counter()
    await feed_wav(str(wav_path), q, session_start=0.0, realtime=True)
    elapsed = time.perf_counter() - t0
    # Drain
    while True:
        item = await q.get()
        if item is None:
            break
    assert elapsed >= 0.30
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_enqueue_asr_coalesces_live_backlog_without_dropping_speech():
    """Live backlog becomes one larger Whisper pass instead of lost speech."""
    q: asyncio.Queue = asyncio.Queue(maxsize=4)
    control = SessionControl()
    a = (np.zeros(10, dtype=np.float32), 1.0, 1.0, 0.1)
    b = (np.zeros(10, dtype=np.float32), 2.0, 2.0, 0.1)
    c = (np.zeros(10, dtype=np.float32), 3.0, 3.0, 0.1)

    assert await enqueue_asr(q, a, control=control, drop_oldest_when_full=True, live_keep=1)
    assert q.qsize() == 1
    assert await enqueue_asr(q, b, control=control, drop_oldest_when_full=True, live_keep=1)
    assert await enqueue_asr(q, c, control=control, drop_oldest_when_full=True, live_keep=1)

    assert control.stale_drops == 0
    assert q.qsize() == 1
    only = q.get_nowait()
    assert len(only[0]) == 30
    assert only[1] == 1.0  # oldest enqueue time retained for lag metrics
    assert only[2] == 3.0  # newest audio end retained for timeline metrics


@pytest.mark.asyncio
async def test_enqueue_asr_live_keep_two_coalesces_when_threshold_reached():
    q: asyncio.Queue = asyncio.Queue(maxsize=4)
    control = SessionControl()
    a = (np.zeros(4, dtype=np.float32), 1.0, 1.0, 0.0)
    b = (np.zeros(4, dtype=np.float32), 2.0, 2.0, 0.0)
    c = (np.zeros(4, dtype=np.float32), 3.0, 3.0, 0.0)

    await enqueue_asr(q, a, control=control, drop_oldest_when_full=True, live_keep=2)
    await enqueue_asr(q, b, control=control, drop_oldest_when_full=True, live_keep=2)
    assert q.qsize() == 2
    assert control.stale_drops == 0
    await enqueue_asr(q, c, control=control, drop_oldest_when_full=True, live_keep=2)
    assert control.stale_drops == 0
    assert q.qsize() == 1
    merged = q.get_nowait()
    assert len(merged[0]) == 12
    assert merged[1] == 1.0
    assert merged[2] == 3.0


@pytest.mark.asyncio
async def test_enqueue_asr_never_drops_on_file_path():
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    control = SessionControl()
    a = (np.zeros(4, dtype=np.float32), 1.0, 1.0, 0.0)
    b = (np.zeros(4, dtype=np.float32), 2.0, 2.0, 0.0)
    assert await enqueue_asr(q, a, control=control, drop_oldest_when_full=False)

    async def _put_second() -> None:
        await enqueue_asr(q, b, control=control, drop_oldest_when_full=False)

    task = asyncio.create_task(_put_second())
    await asyncio.sleep(0)
    assert not task.done()
    assert control.stale_drops == 0
    got = q.get_nowait()
    assert got[1] == 1.0
    await task
    assert q.get_nowait()[1] == 2.0


@pytest.mark.asyncio
async def test_enqueue_asr_preserves_none_sentinel():
    q: asyncio.Queue = asyncio.Queue(maxsize=1)
    control = SessionControl()
    await enqueue_asr(q, None, control=control, drop_oldest_when_full=False)
    item = (np.zeros(2, dtype=np.float32), 9.0, 9.0, 0.0)
    ok = await enqueue_asr(q, item, control=control, drop_oldest_when_full=True)
    assert ok is False
    assert q.get_nowait() is None
    assert q.empty()


def test_push_audio_source_drops_until_attached():
    source = PushAudioSource()
    received: list[np.ndarray] = []
    source.push(np.ones(160, dtype=np.float32))
    source.attach(received.append)
    source.push(np.ones(320, dtype=np.float32))
    source.detach()
    source.push(np.ones(160, dtype=np.float32))
    assert [len(r) for r in received] == [320]
    assert source.samples_received == 640


@pytest.mark.asyncio
async def test_feed_live_consumes_pushed_audio_without_sounddevice(monkeypatch):
    """Native system-audio sessions feed VAD/ASR from pushes, no input device."""
    monkeypatch.setattr("mimivox.audio.SileroVAD", _PassThroughVAD)
    monkeypatch.setitem(__import__("sys").modules, "sounddevice", None)

    source = PushAudioSource()
    control = SessionControl()
    asr_q: asyncio.Queue = asyncio.Queue()
    task = asyncio.create_task(
        feed_live(
            asr_q,
            0.0,
            asyncio.get_running_loop(),
            control=control,
            push_source=source,
        )
    )
    for _ in range(50):
        await asyncio.sleep(0.01)
        if control.audio_input_active:
            break
    source.push(np.full(SAMPLE_RATE // 10, 0.1, dtype=np.float32))

    item = await asyncio.wait_for(asr_q.get(), timeout=2.0)
    assert len(item[0]) == SAMPLE_RATE // 10

    control.request_stop()
    await asyncio.wait_for(task, timeout=2.0)
    source.push(np.ones(160, dtype=np.float32))  # detached: ignored
    assert asr_q.get_nowait() is None
