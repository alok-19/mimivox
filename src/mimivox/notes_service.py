"""mimivox.notes_service — Run meeting-notes / Ask on the shared MLX lock.

``meeting_notes`` is pure; ``notes_llm`` owns the model. This module is the
seam: every LLM call takes the shared MLX lock that Whisper and CAT use, so
notes never run concurrently with live inference.

Live captions come first. Automatic notes wait until no meeting is live. During
a meeting, Ask and the Notes button load the model outside the lock, start in a
pause in speech, and hand the lock back every few tokens, so captions wait at
most a few tokens rather than a whole reply.
"""

from __future__ import annotations

import asyncio
import os
import time
import weakref
from collections.abc import Callable
from enum import Enum
from typing import Any

from mimivox import meeting_notes, notes_llm
from mimivox.config import _models, shared_mlx_lock

IDLE_UNLOAD_S = float(os.environ.get("MIMIVOX_NOTES_IDLE_UNLOAD_S") or 300)
QUIET_GAP_S = 1.5  # how long the lock must sit free before Ask starts mid-meeting
MAX_WAIT_S = 20.0  # after this, Ask queues for the lock instead of waiting for a pause
POLL_S = 0.05


class Priority(Enum):
    WHEN_IDLE = "when_idle"  # Ask / Notes button: in pauses during a meeting
    AFTER_MEETING = "after_meeting"  # automatic notes: once no meeting is live


# Set by the API layer; tests replace it.
meeting_is_live: Callable[[], bool] = lambda: False  # noqa: E731

_idle_task: asyncio.Task | None = None
_gpu_holders = 0
_asks_running = 0
_in_reply = 0  # replies started and not finished, paused ones included
# One notes reply at a time per loop: a paused reply keeps MLX state (its KV
# cache, the wired-memory limit) that a second concurrent reply would disturb.
_reply_locks: weakref.WeakKeyDictionary[asyncio.AbstractEventLoop, asyncio.Lock] = (
    weakref.WeakKeyDictionary()
)


def holding_gpu() -> bool:
    """True while a notes/ask LLM call holds the lock live captions share."""
    return _gpu_holders > 0


async def _acquire(priority: Priority, *, resuming: bool = False) -> asyncio.Lock:
    """Take the shared MLX lock once live captions can spare it.

    With no meeting live, take it in turn. During one, automatic notes wait for
    the meeting to end and Ask waits for a pause in speech with no final
    translation queued. After ``MAX_WAIT_S``, or when resuming a reply it
    paused, Ask just queues behind the captions already waiting.
    """
    lock = shared_mlx_lock()
    started = time.monotonic()
    quiet_since: float | None = None
    while meeting_is_live():
        now = time.monotonic()
        if priority is Priority.AFTER_MEETING or _models.cat_final_waiting or lock.locked():
            quiet_since = None
        else:
            quiet_since = quiet_since or now
        if priority is Priority.WHEN_IDLE:
            if resuming or now - started >= MAX_WAIT_S:
                break
            if quiet_since is not None and now - quiet_since >= QUIET_GAP_S:
                break
        await asyncio.sleep(POLL_S)
    await lock.acquire()
    return lock


class _LockedLLM:
    """A ``Generator`` for worker threads: each call holds the MLX lock."""

    def __init__(self, loop: asyncio.AbstractEventLoop, priority: Priority) -> None:
        self.loop = loop
        self.priority = priority
        self.calls = 0
        self.retries = 0
        self._lock: asyncio.Lock | None = None
        self._in_call = False

    def __call__(self, messages: list[dict[str, str]], *, max_tokens: int) -> str:
        self.calls += 1
        if len(messages) > 2:  # the validator's repair turn
            self.retries += 1
        return asyncio.run_coroutine_threadsafe(self._one(messages, max_tokens), self.loop).result()

    async def _one(self, messages: list[dict[str, str]], max_tokens: int) -> str:
        global _in_reply
        reply_lock = _reply_locks.setdefault(self.loop, asyncio.Lock())
        async with reply_lock:
            if self.priority is Priority.WHEN_IDLE and meeting_is_live():
                # A cold load takes seconds; do it before queueing for the GPU.
                await self.loop.run_in_executor(None, notes_llm.load)
            await self._take()
            self._in_call = True
            _in_reply += 1
            try:
                return await self.loop.run_in_executor(
                    None,
                    lambda: notes_llm.generate(messages, max_tokens=max_tokens, pause=self._pause),
                )
            finally:
                # Cancelled mid-reply, the worker thread runs on; _hand_back
                # then stops it rather than taking the lock again.
                self._in_call = False
                _in_reply -= 1
                self._give()

    async def _take(self, *, resuming: bool = False) -> None:
        global _gpu_holders
        self._lock = await _acquire(self.priority, resuming=resuming)
        _gpu_holders += 1

    def _give(self) -> None:
        global _gpu_holders
        if self._lock is not None:
            _gpu_holders -= 1
            self._lock.release()
            self._lock = None

    def _pause(self) -> None:
        """Mid-reply, on the worker thread: let queued live captions use the GPU."""
        if meeting_is_live():
            asyncio.run_coroutine_threadsafe(self._hand_back(), self.loop).result()

    async def _hand_back(self) -> None:
        self._give()  # a queued caption, already woken, goes before our re-acquire
        if not self._in_call:
            raise RuntimeError("notes reply abandoned")
        await self._take(resuming=True)


async def _run(fn: Any, priority: Priority) -> tuple[Any, dict[str, Any]]:
    global _asks_running
    loop = asyncio.get_running_loop()
    llm = _LockedLLM(loop, priority)
    t0 = time.perf_counter()
    user_job = priority is Priority.WHEN_IDLE
    _asks_running += user_job
    if user_job and _idle_task is not None:
        _idle_task.cancel()  # the idle timer must not unload the model this Ask is about to use
    try:
        result = await loop.run_in_executor(None, lambda: fn(llm))
    finally:
        _asks_running -= user_job
        _schedule_unload(loop, IDLE_UNLOAD_S)
    meta = {
        "model": notes_llm.loaded_id(),
        "latency_s": round(time.perf_counter() - t0, 2),
        "llm_calls": llm.calls,
        "retries": llm.retries,
    }
    return result, meta


async def notes_for(
    lines: list[meeting_notes.Line],
    budget: int | None = None,
    priority: Priority = Priority.WHEN_IDLE,
):
    b = budget or meeting_notes.CHUNK_TOKEN_BUDGET
    return await _run(lambda llm: meeting_notes.generate_notes(llm, lines, budget=b), priority)


async def ask(lines: list[meeting_notes.Line], question: str, *, live: bool = False):
    return await _run(
        lambda llm: meeting_notes.answer_question(llm, lines, question, suggest_phrase=live),
        Priority.WHEN_IDLE,
    )


async def unload() -> bool:
    async with shared_mlx_lock():
        return notes_llm.unload()


def release_for_meeting() -> asyncio.Task | None:
    """Free the notes model's memory as a meeting starts, unless an Ask is running.

    Returns the unload task, or None when the model stays. A pinned model
    (a pair that translates with it) also stays: ``notes_llm.unload`` refuses.
    """
    if _asks_running or _in_reply:
        return None  # a paused reply still holds the model; unloading frees nothing
    return _schedule_unload(asyncio.get_running_loop(), 0)


def _schedule_unload(loop: asyncio.AbstractEventLoop, delay_s: float) -> asyncio.Task:
    """Evict the model after ``delay_s``, replacing any pending eviction."""
    global _idle_task
    if _idle_task is not None and not _idle_task.done():
        _idle_task.cancel()

    async def _later() -> None:
        await asyncio.sleep(delay_s)
        if not (_asks_running or _in_reply):
            await unload()

    _idle_task = loop.create_task(_later())
    return _idle_task
