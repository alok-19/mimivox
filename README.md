# MimiVox — Real-time Meeting Translator

On-device speech translation for meetings, on Apple Silicon via MLX. Japanese to English is the
main path; Chinese and Korean to English and English to Japanese also work. No audio or text
leaves the Mac once the models are downloaded.

## What it does

1. Captures audio from a WAV file, the microphone, or system audio (macOS app)
2. Segments speech with **Silero VAD**
3. Transcribes with **mlx-whisper** (`LibraxisAI/whisper-large-v3-turbo-mlx-q8`)
4. Translates JA→EN with **CAT-Translate** (`hotchpotch/CAT-Translate-1.4b-mlx-q4`), using the
   previous three lines as context; other language pairs use Qwen3-4B
5. Streams live previews and final captions to the macOS app, the web UI or the terminal

## Run the macOS app

Build the app once, then open it:

```bash
scripts/build_app.sh          # -> dist/MimiVox.app (needs Xcode 26+ and uv)
open dist/MimiVox.app
```

Building needs the full Xcode app, version 26 or later. The Command Line Tools alone are not
enough: SwiftUI's macros (`@State`, `@Entry`) compile through a plugin that only Xcode ships.
Select it with `sudo xcode-select -s /Applications/Xcode.app`.

The app ships its own Python and starts its own server on a free local port, so you need
nothing else running and other programs on port 8765 do not get in its way. The server exits
with the app. Set `MIMIVOX_PORT` to pin the port; the app then reuses a MimiVox server already
there. Server output goes to `logs/helper.log` in the data folder
(`~/Library/Application Support/MimiVox`). The server answers at once. If the models are
not cached it waits for a download request (`POST /api/setup/download`, about 1.7 GB into the
Hugging Face cache) and reports progress in `/api/status` under `setup`. The bundle is not notarized (#37), so macOS blocks a downloaded copy. Run this once
after moving it to Applications:

```bash
xattr -dr com.apple.quarantine /Applications/MimiVox.app
```

Or open it once, then allow it under System Settings → Privacy & Security → Open Anyway.
Build details are in `macos/README.md`.

To try it without a meeting, generate the sample recording and use the **Replay** source:

```bash
python scripts/make_meeting_fixture.py   # -> data/fixtures/meeting-recording.wav
```

## Run from source

```bash
brew install ffmpeg
pip install -e .              # runtime only
pip install -e ".[dev]"       # adds pytest, ruff, and the eval dependencies
```

**Tests and style**

```bash
pytest
ruff check && ruff format --check
```

**Server and web UI**

```bash
mimivox-server                # open http://localhost:8765
```

**macOS app against that server**

```bash
cd macos/MeetingTranslator && swift run MeetingTranslator
```

**CLI**

```bash
mimivox data/fixtures/meeting-recording.wav                 # WAV file
mimivox --live                                              # microphone
mimivox meeting.wav --asr-only                              # transcript only
mimivox meeting.wav --glossary terms.csv                    # with a glossary
mimivox meeting.wav --source-lang zh                        # Chinese to English
```

## Features

**Captions**

- **Context translation.** CAT gets the previous three lines, so pronouns and dropped subjects
  resolve. Japanese words that CAT would otherwise leave in the output (部長, product names) are
  blocked, and numbers are checked against the source with one retry.
- **Revisions.** When the VAD splits one sentence in two, the first caption is rewritten in
  place as a `revision` event.
- **Speaker labels.** "Speaker 1", "Speaker 2" from an optional WeSpeaker model (download it
  from Models). Rename with `POST /api/session/speakers/rename {"id", "label"}`.
- **Confidence flags.** Each caption carries `confidence` (`high|medium|low` with reasons).
  Medium and low captions get a marker; exports include it. Recalibrate with
  `scripts/confidence_report.py`.
- **Quiet speakers.** The VAD gates on the room's noise floor, so soft voices are captioned.

**Glossaries.** Profiles of `source,target` entries in Settings, or a CSV/TSV/JSON file. A
Japanese target fixes what Whisper hears; a Latin target is a term substituted before
translation. Pick one per meeting (`glossary_profile` on `/api/session/start`, or
`--glossary-profile` in the CLI).

**Language pairs.** `ja→en` (default), `zh→en`, `ko→en` and `en→ja`, chosen in the app's
source menu, the web picker, `/api/session/start` (`source_lang`, `target_lang`) or the CLI.
Pairs other than JA→EN need the Qwen model (see Meeting notes). See `docs/language-pairs.md`.

**Reply in Japanese.** Write a reply in English and get Japanese plus a back-translation to
check it (Live Meeting → Reply in Japanese, or the EN→JA toggle in Quick Translate).
`POST /api/translate {"text", "direction": "en-ja"}`.

**Floating captions (HUD).** Font size; English, bilingual or Japanese; click a line to see its
other language; star to pin lines, then copy or export them (`<id>.pinned.md`). Read-aloud
speaks captions through the default output (use headphones), off on every launch. See
`docs/hud-readout.md`.

**Falling behind.** A health pill shows when captions lag and why (GPU busy, slow ASR, heat).
**Catch up** either skips translating queued lines (`fast`) or skips queued audio (`skip`), and
the transcript marks every skip. After the meeting, Review Sessions → **Recover skipped captions** fills
them in. Setting `catch_up_mode`: `off`, `manual` or `auto`. See `docs/latency-health.md`.

**Meeting auto-start (macOS).** When Zoom, Teams, Webex, Slack, FaceTime or a browser starts
playing call audio, the app offers to start captions. Settings → Meeting apps sets, per app,
whether to offer and whether its audio is captured (Default, Only this, Never capture).

**Meeting notes and Ask.** On-device with Qwen3-4B (mlx_lm). Download it with
`POST /api/models/download {"notes_llm": true}`; it loads on first use and unloads after 5 idle
minutes.

- `POST /api/session/notes {"session_id"?, "regenerate"?}` returns English and Japanese
  summaries, decisions and action items, each citing transcript lines.
- `POST /api/session/ask {"question", "session_id"?}` answers from the transcript so far.

**Meeting library.** Search past meetings by Japanese or English text, speaker, tag and date
(macOS: Review Sessions → Meeting library). Clicking a result opens the meeting at that line.
`GET /api/sessions/search?q&since&until&speaker&tag`, `POST /api/sessions/{id}/tags`.

**Learning mode.** For people studying Japanese; off by default (Settings, or
`POST /api/settings {"learning_mode": true}`). Japanese captions get furigana and a hover gloss
from a packaged JMdict subset. Review lists "Words from this meeting", exportable as CSV or an
Anki deck (`POST /api/session/vocabulary/export?format=csv|anki`).

## Privacy

- **Offline lock (opt-in).** Model downloads always work by default. Start the server with
  `MIMIVOX_OFFLINE=1` to lock it: once the Whisper and translation models are cached, it sets
  `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` and refuses model downloads. `/api/status`
  reports `privacy.offline_locked`.
- **Retention.** Setting `retention_policy`: `forever`, `30d`, `7d`, `1d` or `none` (save no
  transcripts). Expired sessions, exports, logs and saved audio are deleted on start, hourly
  and when the policy changes. `DELETE /api/sessions` deletes all history.
- **Audio.** MimiVox saves no meeting audio, with one exception: speech skipped by catch-up
  `skip` mode is kept with the session (`<id>.gaps/`) so it can be recovered later. It is not
  saved under `none` and is deleted with the session.
- **Capture.** System-audio capture can exclude apps (Settings → Meeting apps → Never capture).
- **For new features.** Anything derived from a session (an index, a cache) should register
  `session_archive.register_delete_hook(fn)` so deletes and expiry reach it.

Data lives in `data/` when run from source, and in `~/Library/Application Support/MimiVox` for
the app. `MIMIVOX_DATA_DIR` overrides both.

## Evaluation

`scripts/eval/` measures the real pipeline on public speech (Google FLEURS, CC-BY-4.0): CER,
chrF, latency, speaker accuracy, revisions and missed lines, over 100 clips and 5 two-speaker
mock meetings.

```bash
python scripts/eval/fetch_fleurs.py         # once; cached in ~/Library/Caches/mimivox/eval/
python scripts/eval/build_meetings.py -n 5
python scripts/eval/run_eval.py --clips 100 # ~20 min; report in data/eval/reports/<time>/
```

Run it before and after a change and compare. Details in `docs/eval.md`.

## Project layout

```
src/mimivox/      — Python package (pipeline, server, API, models)
  pipeline.py     — async orchestration (workers, queues, sessions)
  translate.py    — CAT-Translate, context, post-processing
  api_adapter.py  — /api/* surface used by the macOS app
  server.py       — FastAPI server and web UI SSE
  static/         — web UI
macos/            — SwiftUI app (see macos/README.md)
scripts/          — app build, fixtures, eval, diagnostics
tests/            — pytest suite
docs/             — architecture and feature notes
```

## Models

| Component | Model |
|-----------|-------|
| Speech recognition | `LibraxisAI/whisper-large-v3-turbo-mlx-q8` |
| JA→EN translation | `hotchpotch/CAT-Translate-1.4b-mlx-q4` |
| Notes, Ask, other language pairs (optional) | Qwen3-4B-Instruct-2507, 4-bit MLX |
| Voice activity | Silero VAD |
| Speaker labels (optional) | WeSpeaker ResNet34-LM ONNX, CPU (CC-BY-4.0, see `THIRD_PARTY_NOTICES.md`) |
| Learning mode | SudachiPy and a JMdict subset |

## Architecture

See `docs/architecture.md` for the streaming pipeline diagram.
