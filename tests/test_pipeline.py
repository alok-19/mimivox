import asyncio
import inspect

import pytest

from mimivox import pipeline


def test_load_all_models_function_exists():
    assert callable(pipeline.load_all_models)


def test_run_is_coroutine():
    assert inspect.iscoroutinefunction(pipeline.run)


def test_run_accepts_asr_model_and_partial_en_params():
    sig = inspect.signature(pipeline.run)
    assert "asr_model_id" in sig.parameters
    assert "partial_en_enabled" in sig.parameters


@pytest.mark.asyncio
async def test_run_uses_one_metal_inference_lock(monkeypatch, tmp_path):
    """Whisper and CAT must not contend on Metal during a live session."""
    seen: list[bool] = []

    async def _feed(*args, **kwargs):
        return None

    async def _asr(*args, **kwargs):
        seen.append(pipeline._models.asr_lock is pipeline._models.cat_lock)

    async def _display(*args, **kwargs):
        return None

    monkeypatch.setattr(pipeline, "feed_wav", _feed)
    monkeypatch.setattr(pipeline, "asr_worker", _asr)
    monkeypatch.setattr(pipeline, "display_worker", _display)
    monkeypatch.setattr(pipeline, "DATA_DIR", str(tmp_path))

    await pipeline.run(
        wav_path="fixture.wav",
        live=False,
        asr_only=False,
        realtime_feed=False,
    )

    assert seen == [True]


async def test_session_start_waits_for_the_lock_a_notes_call_holds(monkeypatch, tmp_path):
    """A session starting mid-notes must not run Whisper beside the notes LLM."""
    from mimivox.config import shared_mlx_lock

    monkeypatch.setattr(pipeline._models, "asr_lock", None)
    monkeypatch.setattr(pipeline._models, "cat_lock", None)
    blocked: list[bool] = []

    async def _feed(*args, **kwargs):
        return None

    async def _asr(*args, **kwargs):
        try:
            await asyncio.wait_for(pipeline._models.asr_lock.acquire(), 0.05)
        except TimeoutError:
            blocked.append(True)
        else:
            pipeline._models.asr_lock.release()
            blocked.append(False)

    async def _display(*args, **kwargs):
        return None

    monkeypatch.setattr(pipeline, "feed_wav", _feed)
    monkeypatch.setattr(pipeline, "asr_worker", _asr)
    monkeypatch.setattr(pipeline, "display_worker", _display)
    monkeypatch.setattr(pipeline, "DATA_DIR", str(tmp_path))

    async with shared_mlx_lock():  # a notes or dictation call in flight
        await pipeline.run(wav_path="fixture.wav", live=False, asr_only=False, realtime_feed=False)

    assert blocked == [True]


async def test_partial_en_coordinator_bounded_and_versioned():
    coord = pipeline._PartialEnCoordinator(
        asyncio.get_running_loop(), lambda ev: None, enabled=True
    )
    # Too short -> no task scheduled.
    coord.schedule("はい")
    assert coord._run_task is None
    # Mostly English -> skipped (would be a passthrough, not a CAT call).
    coord.schedule("hello world")
    assert coord._run_task is None
    # Short JA without clause end waits for more context.
    coord.schedule("お兄ちゃん起きて")  # no 。 and < PREFER_LEN
    assert coord._run_task is None
    # Meaningful JA with clause end / length -> schedules one in-flight task.
    coord.schedule("お兄ちゃん起きなよ。早くしてね")
    assert coord._run_task is not None
    first_task = coord._run_task
    # While in-flight, a second schedule is ignored (no pile-up).
    coord.schedule("もう一段落長い日本語の文章です。さらに続きます")
    assert coord._run_task is first_task
    # Cancel invalidates via version bump (does not cancel the task itself).
    v_before = coord._version
    coord.cancel()
    assert coord._version == v_before + 1
    assert coord._final_started is True
    # No new previews while final is in progress.
    coord.schedule("キャンセル後はスケジュールしない日本語です。")
    assert coord._run_task is first_task
    # Let the scheduled task finish (it no-ops on a None lock / no models).
    await first_task
    coord.reset_utterance()
    assert coord._final_started is False
    # Disabled coordinator never schedules.
    coord2 = pipeline._PartialEnCoordinator(
        asyncio.get_running_loop(), lambda ev: None, enabled=False
    )
    coord2.schedule("お兄ちゃん起きなよ。早くしてね")
    assert coord2._run_task is None


async def test_partial_en_seeds_cache_when_complete(monkeypatch):
    """Complete preview EN is cached under space-normalized key for final reuse."""
    from mimivox import translate as tr
    from mimivox.config import _TRANSLATION_CACHE

    async def _fake_translate(self, ja, token_cb, priority="final", context=()):
        return ("We will migrate the index page next.", 0.05)

    monkeypatch.setattr(tr.CatTranslator, "translate", _fake_translate)
    events: list[dict] = []
    coord = pipeline._PartialEnCoordinator(asyncio.get_running_loop(), events.append, enabled=True)
    # Not in the seeded phrase cache.
    ja = "定年評価インデックスページを移植してほしいです。"
    key = tr.translation_cache_key(tr.apply_glossary(tr.normalize_punctuation(ja)))
    _TRANSLATION_CACHE.pop(key, None)
    coord.schedule(ja)
    assert coord._run_task is not None
    await coord._run_task
    assert _TRANSLATION_CACHE.get(key) == "We will migrate the index page next."
    # Spaced variant shares the key → final path can hit without re-CAT.
    spaced = tr.translation_cache_key("定年評価インデックスページを移植してほしいです。 ")
    assert spaced == key


@pytest.mark.asyncio
async def test_preview_translate_skips_when_final_waiting(monkeypatch):
    """Preview must not take cat_lock while a final translation is waiting."""
    from mimivox import translate as tr
    from mimivox.config import _models

    calls: list[str] = []

    def _fake_stream(ja, cb, max_tokens_cap=None, context=()):
        calls.append(ja)
        return ("ok", 0.01)

    monkeypatch.setattr(tr, "_do_translate_streaming", _fake_stream)
    _models.cat_lock = asyncio.Lock()
    _models.cat_final_waiting = 0
    translator = tr.CatTranslator()

    # Preview runs when idle.
    en, *_ = await translator.translate("テスト文章です", lambda t: None, priority="preview")
    assert en == "ok"
    assert calls == ["テスト文章です"]

    # Final waiting → preview bails without another generate.
    _models.cat_final_waiting = 1
    en2, lat2, _ = await translator.translate("二回目", lambda t: None, priority="preview")
    assert en2 == "" and lat2 == 0.0
    assert calls == ["テスト文章です"]

    _models.cat_final_waiting = 0
    # Lock held → preview bails.
    await _models.cat_lock.acquire()
    try:
        en3, lat3, _ = await translator.translate("三回目", lambda t: None, priority="preview")
        assert en3 == "" and lat3 == 0.0
    finally:
        _models.cat_lock.release()
    assert calls == ["テスト文章です"]


@pytest.mark.asyncio
async def test_asr_worker_coalesces_stale_backlog(monkeypatch):
    """Stale speech is merged into one decode instead of discarded."""
    import time

    import numpy as np

    from mimivox.config import ASR_Q_STALE_WAIT_S
    from mimivox.session_control import SessionControl

    asr_q: asyncio.Queue = asyncio.Queue()
    display_q: asyncio.Queue = asyncio.Queue()
    control = SessionControl()
    calls: list[int] = []

    def _fake_asr(audio, on_segment):
        calls.append(len(audio))
        return ("こんにちは", 0.01)

    monkeypatch.setattr(pipeline, "_do_asr_streaming", _fake_asr)

    # Bypass MLX lock (None in unit tests).
    class _NullCM:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return False

    class _LockProxy:
        def __init__(self):
            self.asr_lock = _NullCM()

    monkeypatch.setattr(pipeline, "_models", _LockProxy())

    stale_t = time.perf_counter() - (ASR_Q_STALE_WAIT_S + 0.5)
    fresh_t = time.perf_counter()
    # Stale item + one fresher still queued → one merged decode.
    await asr_q.put((np.zeros(100, dtype=np.float32), stale_t, 1.0, 0.1))
    await asr_q.put((np.zeros(200, dtype=np.float32), fresh_t, 2.0, 0.1))
    await asr_q.put(None)

    await pipeline.asr_worker(
        asr_q,
        display_q,
        session_start=time.perf_counter(),
        loop=asyncio.get_running_loop(),
        asr_only=False,
        control=control,
        drop_stale=True,
    )

    assert control.stale_drops == 0
    assert calls == [300]
    finals = []
    while True:
        item = await display_q.get()
        if item is None:
            break
        if getattr(item, "kind", None) == "final":
            finals.append(item)
    assert len(finals) == 1


@pytest.mark.asyncio
async def test_asr_worker_coalesces_multiple_stale_items(monkeypatch):
    """Several pending utterances become one ordered decode with no loss."""
    import time

    import numpy as np

    from mimivox.config import ASR_Q_STALE_WAIT_S
    from mimivox.session_control import SessionControl

    asr_q: asyncio.Queue = asyncio.Queue()
    display_q: asyncio.Queue = asyncio.Queue()
    control = SessionControl()
    calls: list[int] = []

    def _fake_asr(audio, on_segment):
        calls.append(len(audio))
        return ("ok", 0.01)

    monkeypatch.setattr(pipeline, "_do_asr_streaming", _fake_asr)

    class _NullCM:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return False

    class _LockProxy:
        def __init__(self):
            self.asr_lock = _NullCM()

    monkeypatch.setattr(pipeline, "_models", _LockProxy())

    stale_t = time.perf_counter() - (ASR_Q_STALE_WAIT_S + 0.5)
    fresh_t = time.perf_counter()
    await asr_q.put((np.zeros(10, dtype=np.float32), stale_t, 1.0, 0.1))
    await asr_q.put((np.zeros(20, dtype=np.float32), stale_t, 2.0, 0.1))
    await asr_q.put((np.zeros(30, dtype=np.float32), fresh_t, 3.0, 0.1))
    await asr_q.put(None)

    await pipeline.asr_worker(
        asr_q,
        display_q,
        session_start=time.perf_counter(),
        loop=asyncio.get_running_loop(),
        asr_only=False,
        control=control,
        drop_stale=True,
    )

    assert calls == [60]
    assert control.stale_drops == 0


@pytest.mark.asyncio
async def test_asr_worker_does_not_drop_last_utt_before_sentinel(monkeypatch):
    """A sole pending None must not cause the last real utterance to be skipped."""
    import time

    import numpy as np

    from mimivox.config import ASR_Q_STALE_WAIT_S
    from mimivox.session_control import SessionControl

    asr_q: asyncio.Queue = asyncio.Queue()
    display_q: asyncio.Queue = asyncio.Queue()
    control = SessionControl()
    calls: list[int] = []

    def _fake_asr(audio, on_segment):
        calls.append(len(audio))
        return ("最後", 0.01)

    monkeypatch.setattr(pipeline, "_do_asr_streaming", _fake_asr)

    class _NullCM:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return False

    class _LockProxy:
        def __init__(self):
            self.asr_lock = _NullCM()

    monkeypatch.setattr(pipeline, "_models", _LockProxy())

    stale_t = time.perf_counter() - (ASR_Q_STALE_WAIT_S + 1.0)
    await asr_q.put((np.zeros(50, dtype=np.float32), stale_t, 1.0, 0.1))
    await asr_q.put(None)

    await pipeline.asr_worker(
        asr_q,
        display_q,
        session_start=time.perf_counter(),
        loop=asyncio.get_running_loop(),
        asr_only=False,
        control=control,
        drop_stale=True,
    )

    assert control.stale_drops == 0
    assert calls == [50]


@pytest.mark.asyncio
async def test_asr_worker_file_replay_never_stale_drops(monkeypatch):
    """Replay/file path must decode every utterance even with backlog."""
    import time

    import numpy as np

    from mimivox.config import ASR_Q_STALE_WAIT_S
    from mimivox.session_control import SessionControl

    asr_q: asyncio.Queue = asyncio.Queue()
    display_q: asyncio.Queue = asyncio.Queue()
    control = SessionControl()
    calls: list[int] = []

    def _fake_asr(audio, on_segment):
        calls.append(len(audio))
        return ("ok", 0.01)

    monkeypatch.setattr(pipeline, "_do_asr_streaming", _fake_asr)

    class _NullCM:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return False

    class _LockProxy:
        def __init__(self):
            self.asr_lock = _NullCM()

    monkeypatch.setattr(pipeline, "_models", _LockProxy())

    stale_t = time.perf_counter() - (ASR_Q_STALE_WAIT_S + 0.5)
    await asr_q.put((np.zeros(10, dtype=np.float32), stale_t, 1.0, 0.1))
    await asr_q.put((np.zeros(20, dtype=np.float32), stale_t, 2.0, 0.1))
    await asr_q.put((np.zeros(30, dtype=np.float32), time.perf_counter(), 3.0, 0.1))
    await asr_q.put(None)

    await pipeline.asr_worker(
        asr_q,
        display_q,
        session_start=time.perf_counter(),
        loop=asyncio.get_running_loop(),
        asr_only=False,
        control=control,
        drop_stale=False,
    )

    assert control.stale_drops == 0
    assert calls == [10, 20, 30]


@pytest.mark.asyncio
async def test_asr_worker_drains_queued_audio_after_stop_requested(monkeypatch):
    import time

    import numpy as np

    from mimivox.session_control import SessionControl

    asr_q: asyncio.Queue = asyncio.Queue()
    display_q: asyncio.Queue = asyncio.Queue()
    control = SessionControl()
    control.request_stop()
    calls: list[int] = []

    def _fake_asr(audio, on_segment):
        calls.append(len(audio))
        return ("最後の発言", 0.01)

    class _NullCM:
        async def __aenter__(self):
            return None

        async def __aexit__(self, *args):
            return False

    class _LockProxy:
        def __init__(self):
            self.asr_lock = _NullCM()

    monkeypatch.setattr(pipeline, "_do_asr_streaming", _fake_asr)
    monkeypatch.setattr(pipeline, "_models", _LockProxy())
    await asr_q.put((np.zeros(100, dtype=np.float32), time.perf_counter(), 1.0, 0.0))
    await asr_q.put(None)

    await pipeline.asr_worker(
        asr_q,
        display_q,
        session_start=time.perf_counter(),
        loop=asyncio.get_running_loop(),
        asr_only=False,
        control=control,
    )

    assert calls == [100]
    assert (await display_q.get()).text == "最後の発言"


def _final(text: str, ts: str = "00:01"):
    from mimivox.config import ASRResult

    return ASRResult(kind="final", text=text, audio_ts=ts, wall_time="00:00:00", asr_latency=0.1)


async def _run_display(monkeypatch, lines: list[str], outputs: list[str]):
    """Run display_worker over finals; return the context seen by each translate call."""
    from mimivox import translate as tr

    seen: list[tuple] = []
    answers = iter(outputs)

    async def _fake_translate(self, ja, token_cb, priority="final", context=()):
        seen.append(tuple(context))
        return (next(answers), 0.01)

    monkeypatch.setattr(tr.CatTranslator, "translate", _fake_translate)
    pipeline._TRANSLATION_CACHE.clear()
    q: asyncio.Queue = asyncio.Queue()
    for i, line in enumerate(lines):
        await q.put(_final(line, f"00:{i:02d}"))
    await q.put(None)
    await pipeline.display_worker(q, [], asyncio.get_running_loop(), partial_en_enabled=False)
    return seen


async def test_display_worker_passes_prior_lines_as_context(monkeypatch):
    seen = await _run_display(
        monkeypatch,
        ["見積もりが届きました。", "来週までに返事が必要です。"],
        ["A quote arrived", "We need to reply by next week"],
    )
    assert seen[0] == ()
    assert seen[1] == (("見積もりが届きました。", "A quote arrived"),)


async def test_display_worker_context_resets_per_session(monkeypatch):
    await _run_display(monkeypatch, ["見積もりが届きました。"], ["A quote arrived"])
    seen = await _run_display(monkeypatch, ["来週までに返事が必要です。"], ["Reply by next week"])
    assert seen == [()]


async def test_display_worker_excludes_english_passthrough_from_context(monkeypatch):
    seen = await _run_display(
        monkeypatch,
        ["見積もりが届きました。", "Let's circle back on that.", "来週までに返事が必要です。"],
        ["A quote arrived", "Reply by next week"],
    )
    assert len(seen) == 2  # passthrough never reaches CAT
    assert seen[1] == (("見積もりが届きました。", "A quote arrived"),)


async def test_preview_passes_context_to_translator(monkeypatch):
    from mimivox import translate as tr

    seen = {}

    async def _fake_translate(self, ja, token_cb, priority="final", context=()):
        seen["context"] = tuple(context)
        return ("Reply by next week.", 0.01)

    monkeypatch.setattr(tr.CatTranslator, "translate", _fake_translate)
    ctx = tr.TranslationContext()
    ctx.add("見積もりが届きました。", "A quote arrived")
    coord = pipeline._PartialEnCoordinator(
        asyncio.get_running_loop(), lambda ev: None, enabled=True, context=ctx
    )
    pipeline._TRANSLATION_CACHE.clear()
    coord.schedule("来週までに返事をしないといけないですね。")
    await coord._run_task
    assert seen["context"] == (("見積もりが届きました。", "A quote arrived"),)
