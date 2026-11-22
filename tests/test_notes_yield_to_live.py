"""Notes and Ask must not interrupt live captions.

During a live meeting, Ask and the Notes button run only once the shared MLX
lock has been quiet for a moment, and automatic notes wait until the meeting
ends. With no meeting live, notes take the lock as before.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

from mimivox import meeting_notes as mn
from mimivox import notes_llm, notes_service
from mimivox.config import _models, shared_mlx_lock


def _lines(n: int = 3) -> list[mn.Line]:
    return [
        mn.Line(id=i, start=float(i), ja=f"line {i}", en="", speaker=None) for i in range(1, n + 1)
    ]


@pytest.fixture(autouse=True)
def _fast_gate(monkeypatch):
    monkeypatch.setattr(_models, "asr_lock", None)
    monkeypatch.setattr(_models, "cat_lock", None)
    monkeypatch.setattr(notes_service, "IDLE_UNLOAD_S", 3600)
    monkeypatch.setattr(notes_service, "QUIET_GAP_S", 0.2)
    monkeypatch.setattr(notes_service, "POLL_S", 0.01)
    monkeypatch.setattr(notes_service, "MAX_WAIT_S", 30)
    monkeypatch.setattr(notes_llm, "load", lambda *a, **k: None)
    monkeypatch.setattr(
        notes_llm,
        "generate",
        lambda m, *, max_tokens, pause=None: json.dumps({"answer": "ok", "line_ids": [1]}),
    )
    yield
    if notes_service._idle_task is not None:
        notes_service._idle_task.cancel()


def _meeting(monkeypatch, live: dict[str, bool]) -> None:
    monkeypatch.setattr(notes_service, "meeting_is_live", lambda: live["on"])


async def test_ask_during_a_meeting_waits_for_a_quiet_gap(monkeypatch):
    live = {"on": True}
    _meeting(monkeypatch, live)
    lock = shared_mlx_lock()

    async def captions(stop: asyncio.Event) -> None:
        # Back-to-back utterances with gaps shorter than the quiet gap.
        while not stop.is_set():
            async with lock:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.05)

    stop = asyncio.Event()
    speaking = asyncio.create_task(captions(stop))
    ask = asyncio.create_task(notes_service.ask(_lines(), "q"))
    await asyncio.sleep(0.6)
    assert not ask.done()  # never cut in between closely spaced utterances

    stop.set()
    await speaking
    answer, _meta = await asyncio.wait_for(ask, 2)
    assert answer.answer == "ok"


async def test_ask_stops_waiting_for_a_pause_after_max_wait(monkeypatch):
    _meeting(monkeypatch, {"on": True})
    monkeypatch.setattr(notes_service, "MAX_WAIT_S", 0.3)
    lock = shared_mlx_lock()
    stop = asyncio.Event()

    async def captions() -> None:
        while not stop.is_set():
            async with lock:
                await asyncio.sleep(0.05)
            await asyncio.sleep(0.05)

    speaking = asyncio.create_task(captions())
    try:
        await asyncio.wait_for(notes_service.ask(_lines(), "q"), 2)
    finally:
        stop.set()
        await speaking


async def test_ask_hands_the_gpu_back_to_captions_mid_reply(monkeypatch):
    _meeting(monkeypatch, {"on": True})
    lock = shared_mlx_lock()
    loop = asyncio.get_running_loop()
    replying = asyncio.Event()
    caption_queued = threading.Event()
    captioned: list[bool] = []

    def generate(m, *, max_tokens, pause=None):
        loop.call_soon_threadsafe(replying.set)
        caption_queued.wait(2)
        pause()
        return json.dumps({"answer": "yes" if captioned else "no", "line_ids": [1]})

    async def caption() -> None:
        caption_queued.set()
        async with lock:
            captioned.append(True)

    monkeypatch.setattr(notes_llm, "generate", generate)
    ask = asyncio.create_task(notes_service.ask(_lines(), "q"))
    await replying.wait()
    asyncio.create_task(caption())
    answer, _meta = await asyncio.wait_for(ask, 2)
    assert answer.answer == "yes"  # the caption ran before the reply finished


async def test_automatic_notes_pause_mid_reply_when_a_meeting_starts(monkeypatch):
    live = {"on": False}
    _meeting(monkeypatch, live)
    loop = asyncio.get_running_loop()
    paused = asyncio.Event()

    def generate(m, *, max_tokens, pause=None):
        live["on"] = True
        loop.call_soon_threadsafe(paused.set)
        pause()
        return json.dumps({"summary_en": "s", "summary_ja": "s"})

    monkeypatch.setattr(notes_llm, "generate", generate)
    notes = asyncio.create_task(
        notes_service.notes_for(_lines(), priority=notes_service.Priority.AFTER_MEETING)
    )
    await paused.wait()
    await asyncio.sleep(0.4)
    assert not notes.done()
    assert not shared_mlx_lock().locked()  # captions have the GPU for the whole meeting

    live["on"] = False
    await asyncio.wait_for(notes, 2)


async def test_ask_waits_behind_a_queued_final_translation(monkeypatch):
    _meeting(monkeypatch, {"on": True})
    monkeypatch.setattr(_models, "cat_final_waiting", 1)
    ask = asyncio.create_task(notes_service.ask(_lines(), "q"))
    await asyncio.sleep(0.5)
    assert not ask.done()

    monkeypatch.setattr(_models, "cat_final_waiting", 0)
    await asyncio.wait_for(ask, 2)


async def test_automatic_notes_wait_until_the_meeting_ends(monkeypatch):
    live = {"on": True}
    _meeting(monkeypatch, live)
    monkeypatch.setattr(
        notes_llm,
        "generate",
        lambda m, *, max_tokens, pause=None: json.dumps({"summary_en": "s", "summary_ja": "s"}),
    )
    notes = asyncio.create_task(
        notes_service.notes_for(_lines(), priority=notes_service.Priority.AFTER_MEETING)
    )
    await asyncio.sleep(0.6)  # the lock is free the whole time
    assert not notes.done()

    live["on"] = False
    await asyncio.wait_for(notes, 2)


async def test_ask_loads_the_model_before_taking_the_lock(monkeypatch):
    _meeting(monkeypatch, {"on": True})
    lock = shared_mlx_lock()
    held_during_load: list[bool] = []
    monkeypatch.setattr(notes_llm, "load", lambda *a, **k: held_during_load.append(lock.locked()))

    await asyncio.wait_for(notes_service.ask(_lines(), "q"), 2)
    assert held_during_load and not any(held_during_load)


async def test_with_no_meeting_ask_runs_straight_away(monkeypatch):
    _meeting(monkeypatch, {"on": False})
    monkeypatch.setattr(notes_service, "QUIET_GAP_S", 30)
    await asyncio.wait_for(notes_service.ask(_lines(), "q"), 1)


async def test_meeting_start_unloads_an_idle_notes_model(monkeypatch):
    monkeypatch.setattr(notes_llm, "_model", object())
    monkeypatch.setattr(notes_llm, "_tokenizer", object())
    monkeypatch.setattr(notes_llm, "_pins", 0)
    await notes_service.release_for_meeting()
    assert not notes_llm.is_loaded()


async def test_meeting_start_keeps_the_model_while_ask_is_running(monkeypatch):
    _meeting(monkeypatch, {"on": False})
    model = object()
    monkeypatch.setattr(notes_llm, "_model", model)
    monkeypatch.setattr(notes_llm, "_tokenizer", object())
    monkeypatch.setattr(notes_llm, "_pins", 0)
    started = asyncio.Event()
    release = asyncio.Event()
    loop = asyncio.get_running_loop()

    def slow_generate(m, *, max_tokens, pause=None):
        loop.call_soon_threadsafe(started.set)
        asyncio.run_coroutine_threadsafe(release.wait(), loop).result()
        return json.dumps({"answer": "ok", "line_ids": [1]})

    monkeypatch.setattr(notes_llm, "generate", slow_generate)
    ask = asyncio.create_task(notes_service.ask(_lines(), "q"))
    await started.wait()
    assert notes_service.release_for_meeting() is None
    assert notes_llm._model is model  # a running Ask keeps its model

    release.set()
    await asyncio.wait_for(ask, 2)


async def test_ask_queues_after_max_wait_even_behind_a_translation_backlog(monkeypatch):
    _meeting(monkeypatch, {"on": True})
    monkeypatch.setattr(notes_service, "MAX_WAIT_S", 0.3)
    monkeypatch.setattr(_models, "cat_final_waiting", 3)  # captions running behind
    await asyncio.wait_for(notes_service.ask(_lines(), "q"), 2)


async def test_an_abandoned_reply_never_takes_the_lock_back(monkeypatch):
    _meeting(monkeypatch, {"on": True})
    monkeypatch.setattr(notes_service, "QUIET_GAP_S", 0)
    loop = asyncio.get_running_loop()
    replying = asyncio.Event()
    cancelled = threading.Event()
    stopped = threading.Event()

    def generate(m, *, max_tokens, pause=None):
        loop.call_soon_threadsafe(replying.set)
        cancelled.wait(2)
        try:
            pause()
        finally:
            stopped.set()
        return "{}"

    monkeypatch.setattr(notes_llm, "generate", generate)
    llm = notes_service._LockedLLM(loop, notes_service.Priority.WHEN_IDLE)
    call = asyncio.create_task(llm._one([{"role": "user", "content": "q"}], 8))
    await replying.wait()
    call.cancel()
    await asyncio.sleep(0.05)
    cancelled.set()
    await loop.run_in_executor(None, stopped.wait, 2)
    await asyncio.sleep(0.05)
    assert not shared_mlx_lock().locked()
    assert not notes_service.holding_gpu()


async def test_idle_unload_waits_for_a_paused_reply(monkeypatch):
    monkeypatch.setattr(notes_service, "_in_reply", 1)
    monkeypatch.setattr(notes_llm, "_model", object())
    monkeypatch.setattr(notes_llm, "_tokenizer", object())
    monkeypatch.setattr(notes_llm, "_pins", 0)
    assert notes_service.release_for_meeting() is None
    await notes_service._schedule_unload(asyncio.get_running_loop(), 0)
    assert notes_llm.is_loaded()
