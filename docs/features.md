# Features

What MimiVox does beyond plain captions, and the API or setting behind each feature. The macOS
app's own how-to table is in [macos/README.md](../macos/README.md#using-it).

## Captions

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
