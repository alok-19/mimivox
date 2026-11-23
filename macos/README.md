# MimiVox macOS client

Native SwiftUI front end for MimiVox. It is a UI client: the models run in the Python server,
which the app talks to over HTTPS and SSE.

## Architecture

```
MimiVox.app ──HTTPS/SSE──▶ mimivox server (own loopback port)
                            ├─ /api/*        session control and data
                            └─ /api/events   live captions and status
```

## Run the app

Requirements: macOS 14+ (14.2+ for built-in system audio), Apple Silicon, and
[uv](https://docs.astral.sh/uv/) to run it. Building also needs Xcode 26 or later (the macOS 26
SDK); the Command Line Tools alone cannot compile SwiftUI's macros.

```bash
make app        # from the repo root: builds dist/MimiVox.app and opens it
```

The script builds the client in release mode, copies a uv-managed CPython 3.12 into
`Contents/Resources/python`, installs `mimivox` and its runtime dependencies there, and
ad-hoc signs the bundle. Rerun it after pulling changes; each run rebuilds the bundle. The
bundle is about 750 MB (MLX, numba and SudachiPy's dictionary ship inside). It is not
notarized (#37). A bundle you build runs as is. A copy downloaded from a release needs
`xattr -dr com.apple.quarantine /Applications/MimiVox.app` once, or Open Anyway under
System Settings → Privacy & Security.

At launch the app starts `python3 -m mimivox.server` from the bundle on a free loopback port
it picks itself, so whatever else listens on 8765 does not matter. It waits for as long as the
server process lives (the first launch downloads models) and shows "Server failed" with a
Retry button if the process exits. The app passes `--parent-pid`, so the server exits within
a second or two of the app, even after a crash. Set `MIMIVOX_PORT` to use a specific port.
The port must be free. The app verifies the helper's certificate and authenticates requests
with a token for that launch. Each app start replaces `logs/helper.log` in the data folder.

User data (settings, sessions, transcripts, the search index, logs) lives in
`~/Library/Application Support/MimiVox`; `MIMIVOX_DATA_DIR` overrides it for the app and the
server. Models stay in the Hugging Face cache and are downloaded on first use.

## Run from source (development)

```bash
make app-dev
```

The source build starts its own helper on a free loopback port with
`uv run python -m mimivox.server`. Set `MIMIVOX_PORT` to use a specific free port.
The helper stores data in the repo's `data/`.

Checks: `make swift-check`. Style: `swift format lint -r Sources`.

## Using it

| Action | How |
|---|---|
| Check readiness | Home (`⌘4`) → check model status or download the caption models |
| Replay a recording | Live → source **Replay** → Start. Default file: `data/fixtures/meeting-recording.wav` (`python scripts/make_meeting_fixture.py`) |
| Start / stop captions | Session menu: Start Captions `⌘R`, Stop Captions `⌘.`, Floating Captions `⌘⇧C` |
| Microphone | Live → source **Microphone** → Start |
| System audio (Zoom, Meet, …) | Live → source **System Audio** → Start |
| Language pair | Live source menu: JA→EN, ZH→EN, KO→EN, EN→JA |
| App language | Settings → General → App language |
| Glossary for this meeting | Live source menu → glossary; edit glossaries in Settings → Glossary |
| Floating captions | HUD toggle in the Live toolbar or the menu. Font size, English / bilingual / Japanese, click a line for its other language, star to pin, read-aloud |
| Captions falling behind | Health pill in the header and HUD, **Catch up** button; Settings → General → When behind |
| Reply in Japanese | Translate (`⌘3`) → English to Japanese, with a back-translation and read-aloud |
| Notes and Ask | Meetings → inspector → Notes; Live → Ask the meeting (needs the notes model, Settings → Models) |
| Export notes | Meetings → inspector → Notes → Markdown or PDF |
| Search past meetings | Meetings (`⌘2`) → search field; filter by date, speaker or tag |
| Recover skipped captions | Meetings → open a meeting → Recover Skipped Captions |
| Rename speakers | Select a speaker label on Live or in a saved meeting |
| Save meeting terms | Meetings → inspector → glossary → add a term to a glossary profile |
| Review flashcards | Learn (`⌘5`) → select a meeting; export to Anki text or CSV |
| Learning mode | Settings → General → Learning mode; Meetings → inspector → Words |
| Meeting auto-start | Settings → Meeting Apps: offer captions when a call starts, per-app capture rules |
| Privacy | Settings → Privacy (keep transcripts, delete all history) |

## Open a page at launch

`MIMIVOX_OPEN_SECTION` picks what opens first: `home`, `live`, `meetings`, `learn`, `translate`, `settings`, or
`settings-<tab>` with a tab of `general`, `audio`, `meeting-apps`, `glossary`, `models` or
`privacy`. `glossary` still works and opens Settings → Glossary. `MIMIVOX_LIBRARY_QUERY=<text>`
fills the Meetings search field. `MIMIVOX_APPEARANCE=dark` or `light` pins the windows to that
appearance without touching the system setting.

## System audio capture

With Settings → Audio → System audio on **Built-in** (default, macOS 14.2+), the app captures what the
Mac plays with a Core Audio process tap (`MeetingTranslatorCore/SystemAudioCapture.swift`),
resamples it to 16 kHz mono, and streams ~100 ms chunks to `/api/session/audio`. No virtual
driver is needed. Apps set to **Never capture** under Settings → Meeting Apps are left out of
the tap; **Only this** captures one app alone.

macOS asks once for **System Audio Recording** permission (Privacy & Security → Screen &
System Audio Recording); until it is granted the tap records silence. Under `swift run` the
permission belongs to your terminal app.

Choosing an input device (BlackHole, Loopback) uses the server-side device path instead; on
macOS before 14.2 that is the only option.

## Endpoints the app uses

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/status` | state, `setup`, models, privacy, `health.latency` |
| GET | `/api/events` | SSE event stream (first message and every change: `SetupStateEvent`) |
| POST | `/api/setup/download`, `cancel` | start or cancel the first-run model download; both return `{"setup": ...}`, and download is idempotent |
| GET/POST | `/api/settings` | settings (saved to `settings.json`) |
| GET | `/api/devices`, `/api/languages` | input devices, language pairs |
| POST | `/api/session/start`, `stop`, `pause`, `resume` | session control (`replay-file`, `microphone`, `system-audio`; `capture: "native"` for app-captured audio) |
| POST | `/api/session/audio?session_id=` | raw mono float32 LE @ 16 kHz from the app |
| POST | `/api/session/export`, `delete` | export (JSON/Markdown, `pinned_only`), delete |
| POST | `/api/session/speakers/rename`, `pins`, `catch-up` | speakers, pinned lines, catch up |
| POST | `/api/session/notes`, `ask` | meeting notes, Ask |
| POST | `/api/session/vocabulary`, `vocabulary/export` | learning-mode word list, CSV/Anki |
| POST | `/api/translate` | Quick Translate and Reply (`direction`: `ja-en`, `en-ja`) |
| GET/DELETE | `/api/sessions`, `/api/sessions/{id}` | archive list, one session, delete |
| GET | `/api/sessions/search` | meeting library search |
| POST | `/api/sessions/{id}/tags`, `/api/sessions/{id}/recover` | tags, gap recovery |
| POST | `/api/models/{name}` | `download`, `update`, `verify`, `unload-notes` |

### Model setup

The server binds its port first and sets up the speech and translation models in the
background. `/api/status` carries the state as `setup`, and `/api/events` pushes it as
`{"type": "SetupStateEvent", "setup": ...}`.

| `setup.phase` | Fields | Next |
|---|---|---|
| `needs_download` | `total_bytes`, `free_disk_bytes` | models not cached; waits for `POST /api/setup/download` |
| `downloading` | `done_bytes`, `total_bytes`, `bytes_per_s`, `eta_s` (null until a rate exists) | `loading`; `cancel` returns to `needs_download` |
| `loading` | none | `ready`; cached models enter here at startup with no user action |
| `ready` | none | |
| `failed` | `reason` (`offline`, `disk_full`, `error`), `message` | `POST /api/setup/download` retries |

Until `ready`, `POST /api/session/start`, `/api/translate`, `/api/session/notes` and
`/api/session/ask` return `409 {"error": "models_not_ready", "message": ..., "setup": {...}}`.
`/api/models/download` (without `notes_llm`) starts the same job while setup is not ready.

## Event mapping

| MimiVox `kind` | Swift `MeetingEvent.type` |
|---|---|
| `session_start` / `session_end` | `SessionStartedEvent` / `SessionEndedEvent` |
| `partial` | `AsrPartialEvent` (live only) |
| `partial_en_token` | `PartialEnEvent` (live only) |
| `translating` | `TranslationStartedEvent` |
| `final` (ja) / `final` (en) | `AsrCompletedEvent` / `TranslationCompletedEvent` |
| `revision` | `TranscriptRevisionEvent` |
| `gap` | `CaptionGapEvent` |
| `error` | `ErrorEvent` |
| (server) | `AudioLevelEvent`, `LatencyHealthEvent`, `SpeakerRenamedEvent` |

## Help and changelog

Both are Markdown files in `Sources/MeetingTranslatorApp/Resources/`, bundled into the app:

- `HELP.md` is the user guide shown by **Help → MimiVox Help** (`⌘?`). Update it when a
  user-facing feature, setting, or shortcut changes.
- `CHANGELOG.md` (Keep-a-Changelog style, newest first) is shown by **About MimiVox → View
  Changelog** and **Help → Changelog**. Add entries under `## [Unreleased]`; on release, rename
  that section to the version and set the same `CFBundleShortVersionString` in
  `BundleResources/Info.plist`. `swift run MeetingTranslatorCoreChecks` fails if they differ.

## Publish a release

Update the version in `pyproject.toml` and `BundleResources/Info.plist`. Run `uv lock` and
add the release entry to the in-app changelog. Push the commit, then push its stable
version tag (for example, `v1.1.2`). GitHub Actions builds the Apple silicon app, verifies
the ZIP and signed Sparkle appcast, then publishes both assets in the same GitHub release.
The appcast points to that release's versioned ZIP. GitHub serves the appcast at the stable
`https://github.com/alok-19/mimivox/releases/latest/download/appcast.xml` URL.

Set up the signing key before the next stable release. The manual workflow uses an ephemeral
key, verifies the app and signed appcast, and does not publish a release.

## Sparkle update setup

MimiVox uses Sparkle 2.10.1 and EdDSA signatures for updates. You do not need a paid Apple
Developer membership. The app and its helper keep their ad-hoc signatures, and the app is not
notarized. Downloaded release builds still need the Gatekeeper steps above.

The app shows **MimiVox → Check for Updates…** only when its bundle has both `SUFeedURL` and
`SUPublicEDKey`. `scripts/build_app.sh` reads these from `SPARKLE_FEED_URL` and
`SPARKLE_PUBLIC_KEY`. Set both values or leave both unset. The build script accepts only an
HTTPS feed URL. Local builds without the values have no update menu item.

Create one EdDSA key pair for stable releases. Run these commands from the repository root.
Sparkle stores the private key in your login Keychain. The GitHub CLI stores the private
key as an Actions secret and the public key as an Actions variable.

```bash
swift package --package-path macos/MeetingTranslator resolve
tools=macos/MeetingTranslator/.build/artifacts/sparkle/Sparkle/bin
"$tools/generate_keys" --account mimivox-release
(
  set -e
  key_dir="$(mktemp -d)"
  trap 'rm -f "$key_dir/private.key"; rmdir "$key_dir"' EXIT
  umask 077
  "$tools/generate_keys" --account mimivox-release -x "$key_dir/private.key"
  gh secret set SPARKLE_PRIVATE_KEY < "$key_dir/private.key"
  gh variable set SPARKLE_PUBLIC_KEY --body "$("$tools/generate_keys" --account mimivox-release -p)"
)
```

Keep a secure backup of the private key. Use the same key for subsequent releases.
The release workflow checks that the keys match before it builds the app.

Users of earlier versions must download the first release with Sparkle. For subsequent
updates, choose **MimiVox → Check for Updates…** in the app.
