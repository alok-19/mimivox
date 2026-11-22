# Caption HUD controls and read-aloud

Hover the floating caption HUD to show its controls.

- **Size**: smaller/larger (three steps).
- **Display**: English, English + Japanese, or Japanese. In a single-language mode, click a line to show its other language; click again to hide it.
- **Pin**: the star on a line pins it. The star menu copies the pinned lines as Markdown, exports them to `data/transcripts/<session>.pinned.md`, or unpins one. Pins are stored with the session (`pinned_ids` in the archive and in JSON exports; `POST /api/session/pins`, `POST /api/session/export?pinned_only=true`).
- **Read aloud**: speaks each finished English caption with `AVSpeechSynthesizer` through the default output device. Voice and speed are in the speaker menu. It starts off on every launch and reads only captions that arrive after you switch it on. It never queues more than two lines behind (older ones are skipped and the HUD says how many), does not re-speak a line a later revision rewrites, and pauses when the session pauses. Use headphones: a speaker can be picked up by the meeting microphone.

Font size, display mode, voice and speed are per-device preferences (`UserDefaults` keys in `HUDPreferences`).

## Debug hook (replay only)

`MIMIVOX_DEMO_REPLAY=<absolute wav>` starts a replay-file session of that WAV on launch and opens the HUD. It never uses the microphone or system audio and never switches read-aloud on. `MIMIVOX_DEMO_HUD=pin-first,reveal-first` pins the first line and reveals its other language. Launch arguments such as `-hud.displayMode englishOnly -hud.size medium` override the stored preferences for one run. Use with `MIMIVOX_PORT=<port>` against a server you started.
