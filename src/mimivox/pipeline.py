"""mimivox.pipeline — Async orchestration: workers, queues, and session lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import datetime
import json
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from mimivox import pair_translate
from mimivox.asr import (
    _deduplicate_phrases,
    _do_asr_streaming,
    is_non_content_asr,
    load_asr_model,
    preload_asr_model,
    set_asr_language,
    set_asr_model,
)
from mimivox.audio import PushAudioSource, coalesce_asr_items, feed_live, feed_wav
from mimivox.catch_up import SKIP_REASON
from mimivox.confidence import AsrSignals, MtSignals, assess, merge_asr
from mimivox.config import (
    _MT_SIGNALS_CACHE,
    _TRANSLATION_CACHE,
    _TRANSLATION_CACHE_MAX,
    ASR_MODEL_FILE,
    ASR_MODEL_LIVE,
    ASR_Q_MAXSIZE,
    ASR_Q_STALE_WAIT_S,
    DATA_DIR,
    HEALTH_BEHIND_CLEAR_S,
    SAMPLE_RATE,
    ASRResult,
    _models,
    apply_live_vad_overrides,
    reset_translation_cache,
    shared_mlx_lock,
)
from mimivox.diarization import SpeakerTracker
from mimivox.languages import DEFAULT_PAIR, LanguagePair
from mimivox.latency_health import DebugDelay
from mimivox.revision import is_split_continuation
from mimivox.session_control import SessionControl
from mimivox.translate import (
    TranslationContext,
    _is_mostly_english,
    apply_glossary,
    get_translator,
    normalize_punctuation,
    translation_cache_key,
)
from mimivox.vad import load_vad_model


# ---------------------------------------------------------------------------
# Model loading
# ---------------------------------------------------------------------------
def load_all_models() -> None:
    load_vad_model()
    load_asr_model()
    translator = get_translator()
    translator.load()
    print()


def _with_default_signals(res: tuple, default: Any = None) -> tuple:
    """(a, b[, signals]) -> (a, b, signals); stubs may still return pairs."""
    return (res[0], res[1], res[2] if len(res) > 2 else default)


def _remember_mt_signals(key: str, signals: MtSignals | None) -> None:
    if signals is None:
        return
    _MT_SIGNALS_CACHE[key] = signals
    _MT_SIGNALS_CACHE.move_to_end(key)
    while len(_MT_SIGNALS_CACHE) > _TRANSLATION_CACHE_MAX:
        _MT_SIGNALS_CACHE.popitem(last=False)


# ---------------------------------------------------------------------------
# ASR worker — single-pass streaming
# ---------------------------------------------------------------------------
async def asr_worker(
    asr_q: asyncio.Queue,
    display_q: asyncio.Queue,
    session_start: float,
    loop: asyncio.AbstractEventLoop,
    asr_only: bool,
    control: SessionControl | None = None,
    *,
    drop_stale: bool = False,
    speakers: SpeakerTracker | None = None,
) -> None:
    debug_delay = DebugDelay.from_env()
    while True:
        item = await asr_q.get()
        if item is None:
            await display_q.put(None)
            asr_q.task_done()
            break

        if control is not None:
            control.queue_depth = asr_q.qsize()
            await control.wait_while_paused()

        if isinstance(item, tuple) and len(item) >= 3:
            audio, enqueue_t, audio_end_s = item[0], float(item[1]), float(item[2])
            vad_wait_s = float(item[3]) if len(item) >= 4 else 0.0
            skip_translation = bool(item[4]) if len(item) >= 5 else False
        else:
            audio, enqueue_t = item[0], float(item[1])
            audio_end_s = max(0.0, enqueue_t - session_start)
            vad_wait_s = 0.0
            skip_translation = False

        # Time the utterance sat in asr_q before we picked it up. With a single
        # ASR worker, this is non-zero only when the previous chunk's decode
        # overran the next utterance's arrival (ASR behind realtime).
        queue_wait_s = max(0.0, time.perf_counter() - enqueue_t)

        # Live catch-up only: if this chunk is already stale and fresher audio
        # is waiting, merge the ordered backlog into one larger Whisper pass.
        # This catches up without silently losing captions. File/replay already
        # blocks at the queue and therefore never needs this path.
        if (
            drop_stale
            and control is not None
            and queue_wait_s >= ASR_Q_STALE_WAIT_S
            and asr_q.qsize() > 0
        ):
            rest: list = []
            while True:
                try:
                    rest.append(asr_q.get_nowait())
                except asyncio.QueueEmpty:
                    break
            audio_rest = [x for x in rest if x is not None]
            sentinel = any(x is None for x in rest)
            if audio_rest:
                merged = coalesce_asr_items([item, *audio_rest])
                for _ in rest:
                    asr_q.task_done()
                if sentinel:
                    asr_q.put_nowait(None)
                if merged is not None:
                    item = merged
                    audio = item[0]
                    enqueue_t = float(item[1])
                    audio_end_s = float(item[2])
                    vad_wait_s = float(item[3]) if len(item) >= 4 else 0.0
                    skip_translation = bool(item[4]) if len(item) >= 5 else False
                    queue_wait_s = max(0.0, time.perf_counter() - enqueue_t)
            else:
                for x in rest:
                    asr_q.put_nowait(x)

        duration_s = float(len(audio)) / float(SAMPLE_RATE) if len(audio) else 0.0
        # Prefer audio-timeline start (end of chunk minus utterance length).
        timeline_s = max(0.0, float(audio_end_s) - duration_s)
        ts = f"{int(timeline_s // 60):02d}:{int(timeline_s % 60):02d}"
        wall = datetime.datetime.now().strftime("%H:%M:%S")

        seg_texts: list[str] = []

        def on_segment(
            seg_text: str,
            *,
            seg_texts: list[str] = seg_texts,
            ts: str = ts,
            wall: str = wall,
            duration_s: float = duration_s,
            timeline_s: float = timeline_s,
            vad_wait_s: float = vad_wait_s,
            queue_wait_s: float = queue_wait_s,
        ) -> None:
            seg_texts.append(seg_text)
            if asr_only:
                print(f"[{ts}] ⟳  {seg_text}", flush=True)
            else:
                result = ASRResult(
                    kind="partial",
                    text=seg_text,
                    audio_ts=ts,
                    wall_time=wall,
                    asr_latency=0.0,
                    duration_s=duration_s,
                    timeline_s=timeline_s,
                    vad_wait_s=vad_wait_s,
                    queue_wait_s=queue_wait_s,
                )

                def _put() -> None:
                    with contextlib.suppress(asyncio.QueueFull):
                        display_q.put_nowait(result)

                loop.call_soon_threadsafe(_put)

        # Speaker embedding is CPU onnxruntime: runs beside ASR, outside the
        # MLX lock. Awaited before the next utterance so clustering sees
        # utterances in order.
        speaker_task = (
            loop.run_in_executor(None, speakers.identify, audio) if speakers is not None else None
        )
        if control is not None:
            control.asr_busy = True
            control.monitor.vad_wait(vad_wait_s)
            control.monitor.asr_begin(duration_s, audio_end_s)
        t_asr = time.perf_counter()
        try:
            async with _models.asr_lock:
                await debug_delay.apply("asr")
                final_text, final_lat, asr_signals = _with_default_signals(
                    await loop.run_in_executor(None, _do_asr_streaming, audio, on_segment)
                )
        finally:
            if control is not None:
                control.asr_busy = False
                control.monitor.asr_end(duration_s, time.perf_counter() - t_asr)
        speaker = None
        if speaker_task is not None:
            try:
                speaker = await speaker_task
            except Exception:
                speaker = None

        if not final_text or is_non_content_asr(final_text):
            if control is not None:
                control.empty_asr_count += 1
            asr_q.task_done()
            await asyncio.sleep(0)
            continue

        if len(final_text) > 60 and len(set(final_text)) <= 8:
            if control is not None:
                control.empty_asr_count += 1
            asr_q.task_done()
            await asyncio.sleep(0)
            continue

        deduped = _deduplicate_phrases(final_text)
        if deduped != final_text:
            asr_signals = dataclasses.replace(asr_signals or AsrSignals(), repeats_collapsed=True)
        final_text = deduped
        if is_non_content_asr(final_text):
            if control is not None:
                control.empty_asr_count += 1
            asr_q.task_done()
            await asyncio.sleep(0)
            continue

        wall = datetime.datetime.now().strftime("%H:%M:%S")
        partial_joined = " ".join(seg_texts)

        if asr_only:
            print(f"[{ts}] 🕐 {wall}  ({final_lat:.2f}s)")
            print(f"  🇯🇵 : {final_text}")
        else:
            await display_q.put(
                ASRResult(
                    kind="final",
                    text=final_text,
                    audio_ts=ts,
                    wall_time=wall,
                    asr_latency=final_lat,
                    partial_text=partial_joined,
                    duration_s=duration_s,
                    timeline_s=timeline_s,
                    vad_wait_s=vad_wait_s,
                    queue_wait_s=queue_wait_s,
                    speaker=speaker.as_dict() if speaker else None,
                    speaker_ms=speakers.last_embed_ms if speaker and speakers else 0.0,
                    asr_signals=asr_signals,
                    skip_translation=skip_translation,
                )
            )

        asr_q.task_done()
        await asyncio.sleep(0)


# ---------------------------------------------------------------------------
# Partial-EN preview coordinator
# ---------------------------------------------------------------------------
class _PartialEnCoordinator:
    """Bounded, cancellable partial-JA -> EN preview translation.

    Streams EN tokens to the client *before* the final ASR lands so the user
    is not staring at Japanese during the ~3s ASR decode. Safety rails:

    * At most one in-flight partial translation at a time (no pile-up on
      the shared CAT lock).
    * Re-preview when substantially more JA arrives after a prior preview
      finishes, so the last preview JA ≈ final JA (cache hit → free final).
    * Successful previews seed ``_TRANSLATION_CACHE`` under a space-normalized
      key so the final path can skip a second CAT when text matches.
    * Monotonic version: once the final translation starts, stale partial
      tokens/results are dropped on the floor.
    * Preview uses ``priority="preview"`` (try-lock + short max_tokens) so it
      cannot block the final translation on ``cat_lock``.
    """

    MIN_JA_LEN = 8  # don't translate ultra-short/partial-filler previews
    # Prefer waiting for a clause boundary or enough context so preview JA
    # is close to the final (enables cache reuse, avoids throwaway CAT).
    PREFER_LEN = 20
    REPREVIEW_GROWTH = 12  # re-run preview if JA grew by this many chars

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        sse_broadcast: Callable[[dict], None] | None,
        enabled: bool = True,
        context: TranslationContext | None = None,
    ) -> None:
        self._context = context or TranslationContext()
        self._loop = loop
        self._sse = sse_broadcast
        self._enabled = enabled
        self._version = 0
        self._run_task: asyncio.Task | None = None
        self._final_started = False
        self._previewed_len = 0

    def cancel(self) -> None:
        """Invalidate any in-flight partial translation (don't cancel the task).

        MLX generate cannot be aborted mid-flight; preview path is short and
        try-lock based so final CAT is not stuck behind a long partial.
        """
        self._version += 1
        self._final_started = True

    def reset_utterance(self) -> None:
        """Allow previews again after a final is fully handled."""
        self._final_started = False
        self._run_task = None
        self._previewed_len = 0

    @staticmethod
    def _ja_key(ja: str) -> str:
        return translation_cache_key(apply_glossary(normalize_punctuation(ja)))

    def schedule(self, ja: str) -> None:
        if not self._enabled or self._sse is None or self._final_started:
            return
        if self._run_task is not None and not self._run_task.done():
            return  # at most one in-flight
        ja = ja.strip()
        if len(ja) < self.MIN_JA_LEN:
            return
        if _is_mostly_english(ja):
            return
        has_clause_end = any(p in ja for p in ("。", "？", "?", "！", "!"))
        if len(ja) < self.PREFER_LEN and not has_clause_end:
            return  # wait for more partial ASR before spending CAT
        key = self._ja_key(ja)
        if not key:
            return
        if key in _TRANSLATION_CACHE:
            return
        # Already previewed equal/longer JA this utterance — skip unless JA grew.
        if self._previewed_len and len(ja) < self._previewed_len + self.REPREVIEW_GROWTH:
            return
        self._version += 1
        ver = self._version
        self._run_task = asyncio.ensure_future(self._run(ja, key, ver))

    async def _run(self, ja: str, key: str, ver: int) -> None:
        if self._final_started or ver != self._version:
            return
        translator = get_translator()

        def on_token(t: str) -> None:
            if ver != self._version or self._final_started:
                return
            with contextlib.suppress(Exception):
                self._loop.call_soon_threadsafe(
                    self._sse, {"kind": "partial_en_token", "text": t, "v": ver}
                )

        try:
            # Context only changes after a final, and a final cancels previews,
            # so a preview seeded into the cache saw the same context the final will.
            # Same glossary-substituted text the final uses, so the cache entry
            # seeded below is the final's translation.
            ja_sub = apply_glossary(ja)
            en_text, _, mt_signals = _with_default_signals(
                await translator.translate(
                    ja_sub, on_token, priority="preview", context=self._context.pairs()
                )
            )
        except Exception:
            return
        if ver != self._version or self._final_started or not en_text:
            return
        self._previewed_len = len(ja)
        # Only seed cache when preview EN looks finished — truncated previews
        # must not become the final translation (accuracy).
        en_strip = en_text.strip()
        complete = len(ja) <= 25 or en_strip[-1:] in ".!?"
        if complete:
            _TRANSLATION_CACHE[key] = en_strip
            _TRANSLATION_CACHE.move_to_end(key)
            _remember_mt_signals(key, mt_signals)
            if len(_TRANSLATION_CACHE) > _TRANSLATION_CACHE_MAX:
                _TRANSLATION_CACHE.popitem(last=False)
        with contextlib.suppress(Exception):
            self._loop.call_soon_threadsafe(
                self._sse, {"kind": "partial_en", "en": en_text, "v": ver}
            )


@dataclasses.dataclass
class _Utterance:
    """The latest emitted caption row, kept so a split continuation can revise it."""

    id: int
    ja: str  # raw JA (merged halves concatenated)
    start_s: float
    end_s: float
    context_before: tuple[tuple[str, str], ...]  # TranslationContext before this row
    log_row: dict
    merged_ids: list[int] = dataclasses.field(default_factory=list)
    asr: AsrSignals | None = None


def _continues(prev: _Utterance, item: ASRResult) -> bool:
    if _is_mostly_english(prev.ja) or _is_mostly_english(item.text):
        return False
    return is_split_continuation(prev.ja, item.text, float(item.timeline_s) - prev.end_s)


# ---------------------------------------------------------------------------
# Display/translate worker — handles partial and final ASRResult
# ---------------------------------------------------------------------------
async def display_worker(
    display_q: asyncio.Queue,
    log_rows: list,
    loop: asyncio.AbstractEventLoop,
    sse_broadcast: Callable[[dict], None] | None = None,
    control: SessionControl | None = None,
    partial_en_enabled: bool = True,
    pair: LanguagePair = DEFAULT_PAIR,
) -> None:
    _partial_accum: str = ""
    _has_partial: bool = False
    context = pair_translate.context_for(pair)  # fresh per session
    translator = get_translator() if pair.uses_cat else pair_translate.translator_for(pair)
    partial_en_enabled = partial_en_enabled and pair_translate.supports_preview(pair)
    _partial_en = _PartialEnCoordinator(
        loop, sse_broadcast, enabled=partial_en_enabled, context=context
    )
    next_id = 1  # stable utterance id per session; events address rows by it
    prev: _Utterance | None = None
    debug_delay = DebugDelay.from_env()

    async def translate_line(
        ja_raw: str, ctx_pairs: tuple[tuple[str, str], ...]
    ) -> tuple[str, float, MtSignals | None]:
        """Passthrough / cache / CAT for one punctuation-normalized JA line."""
        ja_norm = apply_glossary(ja_raw)
        # Judge on the text before glossary substitution: a line that only
        # became English because of a term swap still needs CAT.
        if pair_translate.is_passthrough(pair, ja_raw):
            return ja_norm.rstrip("。"), 0.0, None
        ja_key = translation_cache_key(ja_norm)
        cached = _TRANSLATION_CACHE.get(ja_key)
        if cached is None:
            cached = _TRANSLATION_CACHE.get(ja_norm)
        if cached is not None:
            # Hit: seeded cache, prior final, or partial-EN preview.
            _TRANSLATION_CACHE.move_to_end(ja_key if ja_key in _TRANSLATION_CACHE else ja_norm)
            return cached, 0.0, _MT_SIGNALS_CACHE.get(ja_key)
        if sse_broadcast:

            def on_token(t: str) -> None:
                loop.call_soon_threadsafe(sse_broadcast, {"kind": "en_token", "text": t})
        else:

            def on_token(t: str) -> None:  # noqa: F811
                pass

        await debug_delay.apply("cat")
        try:
            en_text, tr_lat, mt_signals = _with_default_signals(
                await translator.translate(
                    ja_norm,
                    on_token,
                    priority="final",
                    context=ctx_pairs,
                )
            )
        except Exception as exc:
            if sse_broadcast:
                sse_broadcast({"kind": "error", "ts": "", "detail": f"translate failed: {exc}"})
            print(f"  ⚠️  translate failed: {exc}")
            return "", 0.0, None
        _TRANSLATION_CACHE[ja_key] = en_text
        _remember_mt_signals(ja_key, mt_signals)
        if ja_norm != ja_key:
            _TRANSLATION_CACHE[ja_norm] = en_text
        while len(_TRANSLATION_CACHE) > _TRANSLATION_CACHE_MAX:
            _TRANSLATION_CACHE.popitem(last=False)
        return en_text, tr_lat, mt_signals

    while True:
        item = await display_q.get()
        if item is None:
            display_q.task_done()
            break

        if control is not None:
            await control.wait_while_paused()

        assert isinstance(item, ASRResult)

        if item.kind == "partial":
            sep = " " if _partial_accum else ""
            _partial_accum += sep + item.text
            preview = _partial_accum[:80] + ("…" if len(_partial_accum) > 80 else "")
            if _has_partial:
                print("\033[1A\033[2K", end="")
            print(f"\033[2m[{item.audio_ts}] ⟳  {preview}\033[0m")
            _has_partial = True
            if sse_broadcast:
                # Full utterance accum so clients can use latest partial as JA preview.
                sse_broadcast(
                    {
                        "kind": "partial",
                        "ja": _partial_accum,
                        "ts": round(float(item.timeline_s), 3),
                        "duration_s": item.duration_s,
                    }
                )
            # Kick off a streamed EN preview so EN appears before the final.
            _partial_en.schedule(_partial_accum)
            display_q.task_done()
            continue

        ja_raw = pair_translate.normalize_for(pair, item.text)
        ja_norm = apply_glossary(ja_raw)
        if control is not None:
            utt_id = control.alloc_row_id()
        else:
            utt_id = next_id
            next_id += 1

        skip_tr = item.skip_translation
        if (
            not skip_tr
            and control is not None
            and control.catch_up_active
            and control.latency_snapshot().backlog_s >= HEALTH_BEHIND_CLEAR_S
        ):
            skip_tr = True
            control.skipped_translations += 1
        if skip_tr:
            prev = None

        # Always mark translation phase (incl. cache / passthrough) so the UI pill
        # can leave Ready — otherwise only cold model calls ever look "Live".
        if control is not None and not skip_tr:
            control.translate_busy = True
            control.monitor.cat_begin(
                float(item.duration_s or 0.0),
                float(item.timeline_s) + float(item.duration_s or 0.0),
            )
        t_cat = time.perf_counter()
        # Final translation is starting: suppress + invalidate partial previews
        # so they cannot schedule ahead of (or block) final CAT.
        _partial_en.cancel()
        if sse_broadcast and not skip_tr:
            sse_broadcast(
                {
                    "kind": "translating",
                    "ts": round(float(item.timeline_s), 3),
                    "ja": item.text,
                }
            )

        merged_into: _Utterance | None = None
        try:
            en_text = ""
            tr_lat = 0.0
            mt_signals = None
            if skip_tr:
                pass
            elif (
                prev is not None
                and pair_translate.supports_revision(pair)
                and _continues(prev, item)
            ):
                # The VAD split one sentence: translate the joined JA as one
                # line, with the context as it was before the first half.
                joined_raw = pair_translate.normalize_for(pair, prev.ja + item.text)
                en_text, tr_lat, mt_signals = await translate_line(joined_raw, prev.context_before)
                if en_text:
                    merged_into = prev
                    ja_norm = apply_glossary(joined_raw)
            if merged_into is None and not skip_tr:
                en_text, tr_lat, mt_signals = await translate_line(ja_raw, context.pairs())
        finally:
            if control is not None and not skip_tr:
                control.translate_busy = False
                control.monitor.cat_end(float(item.duration_s or 0.0), time.perf_counter() - t_cat)
            _partial_en.reset_utterance()

        if merged_into is not None:
            context.restore(merged_into.context_before)
        context_before = context.pairs()
        if not skip_tr:
            context.add(ja_norm, en_text)
        total = item.asr_latency + tr_lat

        if merged_into is not None:
            # Revise the earlier caption in place; the later half gets no row.
            end_s = float(item.timeline_s) + float(item.duration_s or 0.0)
            merged_into.merged_ids.append(utt_id)
            merged_into.asr = merge_asr(merged_into.asr, item.asr_signals)
            confidence = assess(merged_into.asr, mt_signals, en_text).as_dict()
            merged_into.ja += item.text
            merged_into.end_s = end_s
            span_s = end_s - merged_into.start_s
            if _has_partial:
                print("\033[1A\033[2K", end="")
            print(f"[{item.audio_ts}] 🔧 revised #{merged_into.id}")
            print(f"  🇯🇵 JA : {merged_into.ja}")
            print(f"  🇬🇧 EN : {en_text}")
            _has_partial = False
            _partial_accum = ""
            if control is not None:
                control.record_final(
                    total,
                    had_ja=True,
                    had_en=bool(en_text),
                    is_late=total > 2.0,
                )
            if sse_broadcast:
                sse_broadcast(
                    {
                        "kind": "revision",
                        "target_id": merged_into.id,
                        "merged_ids": list(merged_into.merged_ids),
                        "reason": "merged_fragment",
                        "ts": round(merged_into.start_s, 3),
                        "ja": merged_into.ja,
                        "en": en_text,
                        "duration_s": round(span_s, 3),
                        "translate_ms": round(tr_lat * 1000),
                        "total_ms": round(total * 1000),
                        "confidence": confidence,
                    }
                )
            row = merged_into.log_row
            row.update(
                {
                    "ja": merged_into.ja,
                    "en": en_text,
                    "merged_ids": list(merged_into.merged_ids),
                    "translate_latency_s": round(row["translate_latency_s"] + tr_lat, 3),
                    "total_latency_s": round(row["total_latency_s"] + total, 3),
                    "duration_s": round(span_s, 3),
                    "confidence": confidence,
                }
            )
            display_q.task_done()
            continue

        if _has_partial:
            print("\033[1A\033[2K", end="")
        _has_partial = False
        _partial_accum = ""

        print(f"[{item.audio_ts}] 🕐 {item.wall_time}")
        if item.speaker:
            print(f"  🗣  SPK: {item.speaker['label']}  (embed {item.speaker_ms:.0f}ms)")
        print(f"  🇯🇵 JA : {item.text}")
        print(
            f"  🇬🇧 EN : {en_text if en_text else ('(skipped: catching up)' if skip_tr else '(empty)')}"
        )
        print(f"  ⏱️  ASR={item.asr_latency:.2f}s | CAT={tr_lat:.2f}s | Total={total:.2f}s")

        confidence = assess(item.asr_signals, mt_signals, en_text).as_dict()
        if control is not None:
            control.record_final(
                total,
                had_ja=bool(item.text),
                had_en=bool(en_text),
                is_late=total > 2.0,
            )

        if sse_broadcast:
            sse_broadcast(
                {
                    "kind": "final",
                    "id": utt_id,
                    # Numeric audio-origin seconds so the UI can show real timecodes.
                    "ts": round(float(item.timeline_s), 3),
                    "ja": item.text,
                    "en": en_text or "",
                    "asr_ms": round(item.asr_latency * 1000),
                    "translate_ms": round(tr_lat * 1000),
                    "total_ms": round(total * 1000),
                    "vad_wait_ms": round(item.vad_wait_s * 1000),
                    "queue_wait_ms": round(item.queue_wait_s * 1000),
                    "duration_s": item.duration_s,
                    "confidence": confidence,
                    **({"speaker": item.speaker} if item.speaker else {}),
                    **({"translation_skipped": SKIP_REASON} if skip_tr else {}),
                }
            )

        log_row = {
            "id": utt_id,
            "audio_ts": item.audio_ts,
            "timeline_s": round(float(item.timeline_s), 3),
            "wall_time": item.wall_time,
            "ja": item.text,
            "en": en_text,
            "partial_ja": item.partial_text,
            "asr_latency_s": round(item.asr_latency, 3),
            "translate_latency_s": round(tr_lat, 3),
            "total_latency_s": round(total, 3),
            "vad_wait_s": round(item.vad_wait_s, 3),
            "queue_wait_s": round(item.queue_wait_s, 3),
            "duration_s": item.duration_s,
            "speaker": item.speaker,
            "speaker_ms": round(item.speaker_ms, 1),
            "confidence": confidence,
            **({"translation_skipped": SKIP_REASON} if skip_tr else {}),
        }
        log_rows.append(log_row)
        prev = (
            None
            if skip_tr
            else _Utterance(
                id=utt_id,
                ja=item.text,
                start_s=float(item.timeline_s),
                end_s=float(item.timeline_s) + float(item.duration_s or 0.0),
                context_before=context_before,
                log_row=log_row,
                asr=item.asr_signals,
            )
        )
        display_q.task_done()


_cache_pair: LanguagePair = DEFAULT_PAIR


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------
async def run(
    wav_path: str | None,
    live: bool,
    asr_only: bool,
    audio_device: int | str | None = None,
    sse_broadcast: Callable[[dict], None] | None = None,
    control: SessionControl | None = None,
    max_duration_s: float | None = None,
    asr_model_id: str | None = None,
    partial_en_enabled: bool = True,
    realtime_feed: bool | None = None,
    audio_push: PushAudioSource | None = None,
    speaker_tracker: SpeakerTracker | None = None,
    save_log: bool = True,
    pair: LanguagePair = DEFAULT_PAIR,
) -> None:
    # Both models execute on Metal. Concurrent inference causes severe memory
    # contention on 16 GB machines; with clause-sized ASR work, serialization
    # has better and far more predictable end-to-end throughput.
    shared_mlx_lock()

    # Select ASR model for this session. Live and file both default to
    # turbo-q8; live may override via settings.asr_model_live / asr_model_id.
    if asr_model_id:
        model_id = asr_model_id
    elif live:
        model_id = ASR_MODEL_LIVE
    else:
        model_id = ASR_MODEL_FILE
    set_asr_model(model_id)
    set_asr_language(pair.whisper_language)
    # The translation cache is keyed by source text alone: a different pair
    # (zh vs ja share kanji strings) must not read the previous pair's entries.
    global _cache_pair
    if pair != _cache_pair:
        reset_translation_cache()
        _cache_pair = pair
    llm = None if pair.uses_cat else pair_translate.translator_for(pair)
    if llm is not None:
        print(f"⏳ Translator ({pair.code}): {pair_translate.NOTES_MODEL_ID}")
        await asyncio.get_running_loop().run_in_executor(None, llm.load)
        print("   ✅ ready")
    # Preload if it isn't the boot-loaded file model (first live use of an
    # alternate live model would otherwise stall on download/load).
    if model_id != ASR_MODEL_FILE:
        loop_pre = asyncio.get_running_loop()
        print(f"⏳ Preload ASR (live): {model_id}")
        await loop_pre.run_in_executor(None, preload_asr_model, model_id)
        print("   ✅ ready")

    # Live sessions tighten VAD endpointing to cut perceived latency.
    if live:
        apply_live_vad_overrides()

    # Yield so POST /api/session/start can flush its response before capture/ASR work.
    await asyncio.sleep(0)

    loop = asyncio.get_running_loop()
    asr_q: asyncio.Queue = asyncio.Queue(maxsize=ASR_Q_MAXSIZE)
    display_q: asyncio.Queue = asyncio.Queue(maxsize=16)
    log_rows: list = []
    session_start = time.perf_counter()
    if control is not None:
        control.session_start_perf = session_start
        control.asr_q = asr_q
        control.display_q = display_q
        control.broadcast = sse_broadcast

    # Replay/UI (control set): pace WAV at 1× wall clock so latency metrics
    # match live. CLI benches (no control): dump as fast as ASR allows.
    if realtime_feed is None:
        realtime_feed = control is not None

    producer = (
        feed_live(
            asr_q,
            session_start,
            loop,
            device=audio_device,
            control=control,
            push_source=audio_push,
        )
        if live
        else feed_wav(
            wav_path,
            asr_q,
            session_start,
            control=control,
            max_duration_s=max_duration_s,
            realtime=bool(realtime_feed),
        )
    )

    # Speaker labels are optional: no model/runtime means no speaker field.
    if speaker_tracker is None:
        speaker_tracker = SpeakerTracker.create()

    workers = [
        producer,
        asr_worker(
            asr_q,
            display_q,
            session_start,
            loop,
            asr_only,
            control=control,
            drop_stale=live,
            speakers=speaker_tracker,
        ),
    ]
    if not asr_only:
        workers.append(
            display_worker(
                display_q,
                log_rows,
                loop,
                sse_broadcast=sse_broadcast,
                control=control,
                partial_en_enabled=partial_en_enabled,
                pair=pair,
            )
        )

    try:
        await asyncio.gather(*workers)
    except KeyboardInterrupt:
        pass
    finally:
        if llm is not None:
            llm.close()

    # Summary
    elapsed = time.perf_counter() - session_start
    print(f"\n{'=' * 60}")
    print(f"✅  {len(log_rows)} chunks  |  wall time {elapsed:.1f}s")

    if log_rows:
        a = [r["asr_latency_s"] for r in log_rows]
        t = [r["translate_latency_s"] for r in log_rows]
        o = [r["total_latency_s"] for r in log_rows]
        v = [r.get("vad_wait_s", 0.0) for r in log_rows]
        q = [r.get("queue_wait_s", 0.0) for r in log_rows]
        # Perceived latency = what the user actually waits from speech end to
        # EN on screen: VAD endpointing + queue backlog + ASR + CAT.
        p = [
            r.get("vad_wait_s", 0.0)
            + r.get("queue_wait_s", 0.0)
            + r["asr_latency_s"]
            + r["translate_latency_s"]
            for r in log_rows
        ]

        def fmt(v: list[float]) -> str:
            return f"avg={sum(v) / len(v):.2f}s  min={min(v):.2f}s  max={max(v):.2f}s"

        print("\n📊 Latency (single-pass streaming):")
        print(f"  ASR         {fmt(a)}")
        print(f"  CAT         {fmt(t)}")
        print(f"  VAD wait    {fmt(v)}")
        print(f"  Queue wait  {fmt(q)}")
        print(f"  Total (log) {fmt(o)}")
        print(f"  Perceived   {fmt(p)}   ← speech-end → EN on screen")
        if control is not None and control.stale_drops:
            print(f"  Stale drops {control.stale_drops}   ← live queue catch-up")

        partial_hits = sum(1 for r in log_rows if r.get("partial_ja"))
        partial_pct = 100 * partial_hits / len(log_rows)
        print(
            f"\n⚡ Partial ASR coverage: {partial_hits}/{len(log_rows)} chunks ({partial_pct:.0f}%)"
        )

        required = {
            "audio_ts",
            "wall_time",
            "ja",
            "en",
            "asr_latency_s",
            "translate_latency_s",
            "total_latency_s",
        }
        missing_fields = [r for r in log_rows if not required.issubset(r.keys())]
        empty_en = [r for r in log_rows if not r.get("en", "").strip()]
        leak_markers = [
            "Translate the following",
            "Please translate",
            "Your message",
            "natural conversational English",
            "Please keep",
            "English:",
        ]
        leaked = [r for r in log_rows if any(m in r.get("en", "") for m in leak_markers)]
        chunk_count = len(log_rows)

        print("\n🔍 Log validation:")
        print(f"  Chunk count : {chunk_count}  (target 8–15)")
        print(f"  Fields OK   : {'✅' if not missing_fields else f'❌ {len(missing_fields)} rows'}")
        print(f"  Empty EN    : {'✅ none' if not empty_en else f'⚠️  {len(empty_en)}'}")
        print(f"  Prompt leaks: {'✅ none' if not leaked else f'❌ {len(leaked)}'}")
        print(f"  Unicode JA  : {'✅' if all(r.get('ja') for r in log_rows) else '❌'}")

        in_range = 8 <= chunk_count <= 15
        avg_lat = sum(o) / len(o)
        ok_partial = partial_pct >= 50
        parts = []
        parts.append("log ✅" if not missing_fields and not leaked else "log ⚠️")
        parts.append(f"chunks {chunk_count}/8–15 {'✅' if in_range else '⚠️'}")
        parts.append(f"latency {avg_lat:.2f}s {'✅' if avg_lat < 2.0 else '⚠️'}")
        parts.append(f"partials {partial_pct:.0f}% {'✅' if ok_partial else '⚠️'}")
        print(f"\n  PHASE 3 SINGLE-PASS VERDICT: {' | '.join(parts)}")

    if not save_log:
        return
    stem = Path(wav_path).stem if wav_path else "live"
    log_dir = Path(DATA_DIR) / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    log_out = log_dir / f"{stem}_v3_log.jsonl"
    with open(log_out, "w", encoding="utf-8") as f:
        for row in log_rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")
    print(f"\n💾  Log: {log_out}")
