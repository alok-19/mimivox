# Latency health and catch-up

When the Mac is busy, captions can drift tens of seconds behind the speaker. MimiVox now says so, says why, and lets the viewer skip ahead without losing speech.

## Health snapshot

`GET /api/status` carries it as `health.latency`; the same object streams on `/api/events` once a second as `{"type": "LatencyHealthEvent", "health": {...}}` (not stored in the transcript). Shape (`mimivox.latency_health.HealthSnapshot`):

| field | meaning |
|---|---|
| `state` | `ok`, `behind` (backlog >= 4 s, clears under 2 s) or `overloaded` (>= 10 s, clears under 6 s) |
| `backlog_s` | age of the oldest unfinished speech on the audio clock: now minus the end of the oldest utterance still queued or in ASR/CAT. A long utterance that is only being processed is not counted as behind |
| `stages` | `vad`, `asr`, `cat`: `queue_depth`, `queued_audio_s`, smoothed `latency_s`, `realtime_factor` (processing seconds per audio second) |
| `reasons` | why, when not `ok`: `notes_gpu` (an Ask or Notes request holds the shared GPU lock; it hands the lock back every few tokens, and the app shows it as info unless captions are overloaded), `thermal` (`pmset -g therm` CPU speed limit under 100), `asr_slow` (ASR slower than real time, or its queue is the biggest), `translate_slow`, else `backlog` |
| `catching_up`, `catch_up_available` | catch-up mode is on; a catch-up would do something now |
| `recommended_mode` | `skip` when ASR runs slower than real time, else `fast` |
| `skipped_translations`, `gaps`, `gap_audio_s` | what catch-ups left behind this session |

Thresholds are constants in `config.py` (`HEALTH_*`). `health.queue_lag_s` now uses the backlog instead of the old 0.5 s per queued item guess.

## Catch up

`POST /api/session/catch-up` with optional `{"mode": "fast" | "skip"}` (default: the recommendation). It does nothing when health is `ok`.

- **fast**: every queued utterance keeps its Japanese, but only the newest is translated. The others become lines with `translation_skipped: "catch_up"` (ASR event, no translation event). Nothing is lost except time spent translating.
- **skip**: as fast, and queued audio older than the newest utterance is dropped. Each dropped span becomes a `CaptionGapEvent` row (`index`, `start`, `end`, `recoverable`, `message`) and its audio is written to `data/sessions/<session id>.gaps/<index>.wav`. A replay feed also jumps to real time; skipped silence is not reported.

Catch-up mode stays on until health returns to `ok`; while it is on, a caption that still has 2 s or more of backlog behind it skips translation. The live item already inside ASR or CAT always finishes. Catch-up is offered (`catch_up_available`) only while something is queued: the item inside a stage cannot be aborted.

Setting `catch_up_mode`: `manual` (default, the Catch up button), `auto` (pressed once per overload when state reaches `overloaded`), `off` (no button, no auto).

## Never silently lost

Skipped work shows up as rows in the Live transcript, the HUD, and every export (markdown, text, jsonl rows carry `skipped: "gap" | "translation"`; srt, vtt and csv show `[Skipped 5s of speech while catching up]`). Gap audio is only written when the session is saved (retention is not "none") and is deleted with the session, by retention expiry, and by "delete all history".

`POST /api/sessions/<id>/recover` (optional `{"index": n}`; refused with 409 while a session runs) transcribes and translates each recoverable gap, replaces the gap event with ASR and translation events under the same index, and translates untranslated lines. Review shows a Recover skipped captions button when the loaded session has any. A model failure keeps the gap; a gap whose audio transcribes to nothing is removed.

## Debug fault injection

`MIMIVOX_DEBUG_DELAY=<stage>:<seconds>[:<count>]` with `stage` of `asr` or `cat` sleeps inside that stage, for the first `count` items (every item if omitted). `scripts/latency_health_evidence.py` uses it to drive a replay session behind and record state, catch-up and recovery.
