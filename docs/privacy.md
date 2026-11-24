# Privacy

No audio or text leaves the Mac once the models are downloaded. This page covers what
MimiVox stores, for how long, and how to lock it offline.

- **Offline lock (opt-in).** Model downloads always work by default. Start the server with
  `MIMIVOX_OFFLINE=1` to lock it: once the Whisper, Haas-ja and translation models are cached, it sets
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
