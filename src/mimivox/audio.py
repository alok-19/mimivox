"""mimivox.audio — Audio capture, resampling, and ring buffer."""

from __future__ import annotations

import asyncio
import contextlib
import subprocess
import tempfile
import threading
import time
import wave
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

import numpy as np

from mimivox.catch_up import record_gap
from mimivox.config import ASR_Q_LIVE_KEEP, SAMPLE_RATE
from mimivox.vad import SileroVAD

if TYPE_CHECKING:
    from mimivox.session_control import SessionControl


def _drop_oldest_audio(asr_q: asyncio.Queue, control: SessionControl | None) -> bool:
    """Drop one oldest non-sentinel item. Returns False if only None remains."""
    try:
        dropped = asr_q.get_nowait()
    except asyncio.QueueEmpty:
        return True
    if dropped is None:
        with contextlib.suppress(asyncio.QueueFull):
            asr_q.put_nowait(None)
        return False
    if control is not None:
        control.stale_drops += 1
    return True


def coalesce_asr_items(items: list[object]) -> object | None:
    """Merge ordered live utterances into one Whisper work item.

    Whisper has substantial fixed cost per invocation. When live inference is
    behind, one larger decode both preserves the queued speech and catches up
    faster than discarding old utterances or running many tiny decodes.
    """
    parts: list[np.ndarray] = []
    enqueue_times: list[float] = []
    audio_end_s = 0.0
    vad_wait_s = 0.0
    skip_translation = False
    for item in items:
        if not isinstance(item, tuple) or len(item) < 3:
            return None
        audio = np.asarray(item[0], dtype=np.float32)
        if audio.size:
            parts.append(audio)
        enqueue_times.append(float(item[1]))
        audio_end_s = float(item[2])
        if len(item) >= 4:
            vad_wait_s = max(vad_wait_s, float(item[3]))
        skip_translation = len(item) >= 5 and bool(item[4])
    if not parts:
        return None
    merged = parts[0] if len(parts) == 1 else np.concatenate(parts)
    out = (merged, min(enqueue_times), audio_end_s, vad_wait_s)
    return (*out, True) if skip_translation else out


async def enqueue_asr(
    asr_q: asyncio.Queue,
    item: object,
    *,
    control: SessionControl | None = None,
    drop_oldest_when_full: bool = False,
    live_keep: int | None = None,
) -> bool:
    """Put an utterance (or None sentinel) onto asr_q.

    File/replay: blocking put (drop_oldest_when_full=False) — never lose chunks.
    Live: keep at most ``live_keep`` (default ASR_Q_LIVE_KEEP) pending items by
    dropping the oldest so capture never stalls and freshest speech wins.
    Never drops the None sentinel. Returns True if ``item`` was enqueued.
    """
    if item is None or not drop_oldest_when_full:
        await asr_q.put(item)
        return True

    keep = ASR_Q_LIVE_KEEP if live_keep is None else max(1, int(live_keep))

    # Collapse pending live work before the hard maxsize. This preserves every
    # queued sample while reducing the number of fixed-cost Whisper calls.
    if asr_q.qsize() >= keep:
        pending: list[object] = []
        saw_sentinel = False
        while True:
            try:
                queued = asr_q.get_nowait()
            except asyncio.QueueEmpty:
                break
            asr_q.task_done()
            if queued is None:
                saw_sentinel = True
            else:
                pending.append(queued)
        if saw_sentinel:
            asr_q.put_nowait(None)
            return False
        merged = coalesce_asr_items([*pending, item])
        if merged is not None:
            item = merged
        elif pending and control is not None:
            # Defensive fallback for legacy item shapes.
            control.stale_drops += len(pending)

    while True:
        try:
            asr_q.put_nowait(item)
            return True
        except asyncio.QueueFull:
            if not _drop_oldest_audio(asr_q, control):
                return False


def _rms_level(samples: np.ndarray) -> float:
    """Map RMS amplitude to a 0..1 meter reading."""
    if samples.size == 0:
        return 0.0
    rms = float(np.sqrt(np.mean(np.square(samples, dtype=np.float64))))
    # Typical speech RMS is ~0.02–0.15; scale so speech lands mid-meter.
    return max(0.0, min(1.0, rms * 8.0))


# ---------------------------------------------------------------------------
# RingBuffer
# ---------------------------------------------------------------------------
class RingBuffer:
    """Thread-safe circular buffer. write() never blocks."""

    def __init__(self, capacity_s: float = 30.0, sample_rate: int = SAMPLE_RATE) -> None:
        cap = int(capacity_s * sample_rate)
        self._buf = np.zeros(cap, dtype=np.float32)
        self._cap = cap
        self._write = 0
        self._count = 0
        self._lock = threading.Lock()

    def write(self, samples: np.ndarray) -> None:
        n = len(samples)
        if n == 0:
            return
        with self._lock:
            if n >= self._cap:
                samples = samples[-self._cap :]
                n = self._cap
            end = self._write + n
            if end <= self._cap:
                self._buf[self._write : end] = samples
            else:
                first = self._cap - self._write
                self._buf[self._write :] = samples[:first]
                self._buf[: n - first] = samples[first:]
            self._write = (self._write + n) % self._cap
            self._count = min(self._count + n, self._cap)

    def read_all(self) -> np.ndarray:
        with self._lock:
            if self._count == 0:
                return np.array([], dtype=np.float32)
            start = (self._write - self._count) % self._cap
            if start + self._count <= self._cap:
                data = self._buf[start : start + self._count].copy()
            else:
                first = self._cap - start
                data = np.concatenate([self._buf[start:], self._buf[: self._count - first]])
            self._count = 0
            return data

    @property
    def available(self) -> int:
        with self._lock:
            return self._count


# ---------------------------------------------------------------------------
# WAV loading
# ---------------------------------------------------------------------------
def _load_wav_mono16k(path: str) -> np.ndarray:
    with wave.open(path, "rb") as wf:
        sr = wf.getframerate()
        ch = wf.getnchannels()
        sw = wf.getsampwidth()
        data = wf.readframes(wf.getnframes())
    # Resample / widen via ffmpeg when not already 16-bit PCM at target rate.
    # Stereo at 16 kHz is mono-mixed in-process (avoids treating L/R as interleaved mono).
    if sw != 2 or sr != SAMPLE_RATE:
        with tempfile.TemporaryDirectory(prefix="mimivox-audio-") as directory:
            output = str(Path(directory) / "converted.wav")
            subprocess.run(
                [
                    "ffmpeg",
                    "-i",
                    str(Path(path).absolute()),
                    "-ar",
                    str(SAMPLE_RATE),
                    "-ac",
                    "1",
                    output,
                ],
                capture_output=True,
                check=True,
            )
            return _load_wav_mono16k(output)
    audio = np.frombuffer(data, dtype=np.int16).astype(np.float32) / 32768.0
    if ch > 1:
        audio = audio.reshape(-1, ch).mean(axis=1).astype(np.float32)
    return audio


# ---------------------------------------------------------------------------
# Audio source: WAV file
# ---------------------------------------------------------------------------
async def feed_wav(
    wav_path: str,
    asr_q: asyncio.Queue,
    session_start: float,
    control: SessionControl | None = None,
    max_duration_s: float | None = None,
    *,
    realtime: bool = True,
) -> None:
    """Feed a WAV through VAD into asr_q.

    Cooperative with pause: while paused, the read cursor does not advance.
    Optional max_duration_s stops after that many seconds of audio time
    (used by Phase C; harmless if None).

    When ``realtime=True`` (default for Replay/UI), audio is paced at 1× wall
    clock so queue_wait / perceived latency match live capture. CLI benches
    can pass ``realtime=False`` to dump the file as fast as workers allow.
    """
    audio = _load_wav_mono16k(wav_path)
    total_s = len(audio) / SAMPLE_RATE
    if max_duration_s is not None and max_duration_s > 0:
        n_max = int(max_duration_s * SAMPLE_RATE)
        audio = audio[:n_max]
        total_s = len(audio) / SAMPLE_RATE
    pace = "1× realtime" if realtime else "max speed"
    print(f"🎧 {Path(wav_path).name}  {total_s:.1f}s  ({pace})\n{'=' * 60}")

    ring = RingBuffer(capacity_s=total_s + 5)
    ring.write(audio)
    vad = SileroVAD()
    feed_size = SAMPLE_RATE // 10  # 100ms slices

    if control is not None:
        control.audio_input_active = True

    full = ring.read_all()
    samples_seen = 0
    # Wall-clock origin for 1× pacing; pause time is excluded via pause_accum.
    pace_origin = time.perf_counter()
    pause_accum = 0.0
    if control is not None:
        control.clock_origin_perf = pace_origin

    i = 0
    while i < len(full):
        if control is not None:
            if control.stop_requested:
                break
            t_pause0 = time.perf_counter()
            await control.wait_while_paused()
            pause_accum += max(0.0, time.perf_counter() - t_pause0)
            if control.stop_requested:
                break

        if control is not None and control.skip_ahead_requested:
            control.skip_ahead_requested = False
            now_s = time.perf_counter() - pace_origin - pause_accum
            target = min(len(full), (int(now_s * SAMPLE_RATE) // feed_size) * feed_size)
            if target > i:
                await _flush_vad(vad, asr_q, control, samples_seen)
                span = full[i:target]
                if _has_speech(span):
                    record_gap(control, span, i / float(SAMPLE_RATE), target / float(SAMPLE_RATE))
                samples_seen = target
                i = target
                control.producer_lag_s = 0.0
                continue

        chunk = full[i : i + feed_size]
        i += feed_size
        samples_seen += len(chunk)
        level = _rms_level(chunk)
        if control is not None:
            control.set_audio_level(level, speech=level > 0.05)

        audio_end_s = samples_seen / float(SAMPLE_RATE)
        utterances = vad.feed(chunk)
        waits = getattr(vad, "last_emits_vad_wait", None)
        if waits is None or len(waits) != len(utterances):
            waits = [0.0] * len(utterances)
        for utterance, vad_wait in zip(utterances, waits):
            # (samples, enqueue_perf, audio timeline end seconds, vad_wait_s)
            await enqueue_asr(
                asr_q,
                (utterance, time.perf_counter(), audio_end_s, vad_wait),
                control=control,
                drop_oldest_when_full=False,
            )

        if realtime:
            # Stay aligned with audio timeline (skip catch-up if we already lag).
            due = pace_origin + pause_accum + audio_end_s
            delay = due - time.perf_counter()
            if control is not None:
                control.producer_lag_s = max(0.0, -delay)
            if delay > 0.001:
                await asyncio.sleep(delay)
            else:
                await asyncio.sleep(0)
        else:
            await asyncio.sleep(0)

    if (control is None or not control.stop_requested) and (final := vad.flush()) is not None:
        audio_end_s = samples_seen / float(SAMPLE_RATE)
        fw = float(getattr(vad, "last_flush_vad_wait", 0.0) or 0.0)
        await enqueue_asr(
            asr_q,
            (final, time.perf_counter(), audio_end_s, fw),
            control=control,
            drop_oldest_when_full=False,
        )

    if control is not None:
        control.audio_input_active = False
        control.producer_lag_s = 0.0
        control.set_audio_level(0.0, speech=False)

    await enqueue_asr(asr_q, None, control=control, drop_oldest_when_full=False)


_GAP_SPEECH_RMS = 0.05


def _has_speech(samples: np.ndarray, slice_s: float = 0.1) -> bool:
    n = int(slice_s * SAMPLE_RATE)
    return any(_rms_level(samples[k : k + n]) > _GAP_SPEECH_RMS for k in range(0, len(samples), n))


async def _flush_vad(vad, asr_q, control, samples_seen: int) -> None:
    if (pending := vad.flush()) is not None:
        fw = float(getattr(vad, "last_flush_vad_wait", 0.0) or 0.0)
        await enqueue_asr(
            asr_q,
            (pending, time.perf_counter(), samples_seen / float(SAMPLE_RATE), fw),
            control=control,
            drop_oldest_when_full=False,
        )


# ---------------------------------------------------------------------------
# Audio source: pushed by a client (macOS Core Audio process tap)
# ---------------------------------------------------------------------------
class PushAudioSource:
    """Live audio delivered over HTTP instead of opened as a local device.

    The macOS app captures system output with a Core Audio process tap and
    POSTs mono float32 PCM at SAMPLE_RATE. feed_live attaches its ingest
    callback here in place of a sounddevice stream; pushes that arrive
    before attach (or after detach) are dropped.
    """

    def __init__(self) -> None:
        self._sink: Callable[[np.ndarray], None] | None = None
        self._lock = threading.Lock()
        self.samples_received = 0

    def attach(self, sink: Callable[[np.ndarray], None]) -> None:
        with self._lock:
            self._sink = sink

    def detach(self) -> None:
        with self._lock:
            self._sink = None

    def push(self, samples: np.ndarray) -> None:
        with self._lock:
            sink = self._sink
        self.samples_received += len(samples)
        if sink is not None and len(samples):
            sink(samples)


# ---------------------------------------------------------------------------
# Audio source: live microphone (or pushed system audio)
# ---------------------------------------------------------------------------
async def feed_live(
    asr_q: asyncio.Queue,
    session_start: float,
    loop: asyncio.AbstractEventLoop,
    device: int | str | None = None,
    control: SessionControl | None = None,
    push_source: PushAudioSource | None = None,
) -> None:
    """Live capture from a local input device, or from ``push_source``.

    When paused, frames are **dropped** (not buffered) to avoid a lag spike
    on resume.
    """
    if push_source is None:
        try:
            import sounddevice as sd
        except ImportError:
            print("❌  pip install sounddevice")
            await asr_q.put(None)
            return

    ring = RingBuffer(capacity_s=60.0)
    vad = SileroVAD()
    raw_q: asyncio.Queue = asyncio.Queue(maxsize=200)
    samples_seen = 0

    def _cb(indata, frames, t, status):
        # Mono mix if the device delivers stereo despite channels=1 request.
        if indata.ndim == 2 and indata.shape[1] > 1:
            samples = indata.mean(axis=1).astype(np.float32, copy=False)
        else:
            samples = indata[:, 0].copy() if indata.ndim == 2 else indata.copy()
        _ingest(samples)

    def _ingest(samples: np.ndarray) -> None:
        # Drop mic frames while paused so resume does not dump backlog.
        if control is not None and control.paused:
            level = _rms_level(samples)
            control.set_audio_level(level, speech=False)
            return
        ring.write(samples)

        def _try_put():
            with contextlib.suppress(Exception):
                raw_q.put_nowait(len(samples))

        loop.call_soon_threadsafe(_try_put)

    if push_source is not None:
        dev_label = " (native system audio)"
    else:
        dev_label = f" (device: {device})" if device else ""
    print(f"🎙️  Live capture{dev_label} — Ctrl+C to stop\n{'=' * 60}")
    if control is not None:
        control.audio_input_active = True

    # Open/start in a worker thread so a slow CoreAudio/BlackHole open cannot
    # block the asyncio loop (which would stall POST /api/session/start).
    def _open_stream() -> object:
        if push_source is not None:
            push_source.attach(_ingest)
            return push_source
        stream = sd.InputStream(
            samplerate=SAMPLE_RATE,
            channels=1,
            blocksize=SAMPLE_RATE // 31,
            device=device,
            callback=_cb,
        )
        stream.start()
        return stream

    def _close_stream(stream: object) -> None:
        if push_source is not None:
            push_source.detach()
            return
        with contextlib.suppress(Exception):
            stream.stop()  # type: ignore[attr-defined]
        with contextlib.suppress(Exception):
            stream.close()  # type: ignore[attr-defined]

    try:
        stream = await loop.run_in_executor(None, _open_stream)
    except Exception as exc:
        print(f"❌  Live capture open failed: {exc}")
        if control is not None:
            control.audio_input_active = False
            control.set_audio_level(0.0, speech=False)
        # Unblock ASR/display workers, then fail the session so the UI does not
        # show a silent start→stop with no error.
        with contextlib.suppress(Exception):
            await asr_q.put(None)
        raise RuntimeError(f"Live capture open failed (device={device!r}): {exc}") from exc

    try:
        try:
            while True:
                if control is not None and control.stop_requested:
                    break
                if control is not None:
                    await control.wait_while_paused()
                    if control.stop_requested:
                        break
                try:
                    await asyncio.wait_for(raw_q.get(), timeout=0.25)
                except asyncio.TimeoutError:
                    continue
                samples = ring.read_all()
                if len(samples) == 0:
                    continue
                samples_seen += len(samples)
                level = _rms_level(samples)
                if control is not None:
                    control.set_audio_level(level, speech=level > 0.05)
                audio_end_s = samples_seen / float(SAMPLE_RATE)
                utterances = vad.feed(samples)
                waits = getattr(vad, "last_emits_vad_wait", None)
                if waits is None or len(waits) != len(utterances):
                    waits = [0.0] * len(utterances)
                for utterance, vad_wait in zip(utterances, waits):
                    if control is not None:
                        control.active_speech = True
                    await enqueue_asr(
                        asr_q,
                        (utterance, time.perf_counter(), audio_end_s, vad_wait),
                        control=control,
                        drop_oldest_when_full=True,
                    )
        except asyncio.CancelledError:
            try:
                if (final := vad.flush()) is not None:
                    audio_end_s = samples_seen / float(SAMPLE_RATE)
                    fw = float(getattr(vad, "last_flush_vad_wait", 0.0) or 0.0)
                    await asyncio.shield(
                        enqueue_asr(
                            asr_q,
                            (final, time.perf_counter(), audio_end_s, fw),
                            control=control,
                            drop_oldest_when_full=True,
                        )
                    )
                await asyncio.shield(
                    enqueue_asr(asr_q, None, control=control, drop_oldest_when_full=False)
                )
            except (asyncio.CancelledError, Exception):
                pass
            raise
        finally:
            if control is not None:
                control.audio_input_active = False
                control.set_audio_level(0.0, speech=False)
            await loop.run_in_executor(None, _close_stream, stream)

        # Clean stop path (stop_requested without cancel).
        try:
            if (final := vad.flush()) is not None:
                audio_end_s = samples_seen / float(SAMPLE_RATE)
                fw = float(getattr(vad, "last_flush_vad_wait", 0.0) or 0.0)
                await enqueue_asr(
                    asr_q,
                    (final, time.perf_counter(), audio_end_s, fw),
                    control=control,
                    drop_oldest_when_full=True,
                )
            await enqueue_asr(asr_q, None, control=control, drop_oldest_when_full=False)
        except Exception:
            with contextlib.suppress(Exception):
                await enqueue_asr(asr_q, None, control=control, drop_oldest_when_full=False)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        print(f"❌  Live capture failed: {exc}")
        with contextlib.suppress(Exception):
            await enqueue_asr(asr_q, None, control=control, drop_oldest_when_full=False)
