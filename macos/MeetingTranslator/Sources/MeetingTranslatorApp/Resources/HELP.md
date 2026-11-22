# MimiVox Help

MimiVox captions meetings live and translates them, entirely on this Mac. Audio,
transcripts, notes, and glossaries never leave it.

## Getting started

- On **Home**, check the model status. If the caption models are missing, select
  **Download models**. The setup card shows the download progress.
- When the models are ready, open **Live** and start captions.
- If the background server stops, select **Retry**.
- macOS asks for microphone or system-audio permission the first time you use that source.

## Live captions

- On **Live** (⌘1), pick a **Source** and a **Language** pair, then click **Start**
  (⌘R). **Stop** (⌘.) ends the meeting and saves it to Meetings.
- **System Audio** captions whatever the Mac plays: Zoom, Meet, Teams, a video.
  **Microphone** captions people in the room. **Replay** plays a recorded file.
- Language pairs: Japanese, Chinese, or Korean to English, and English to Japanese.
- Pause and resume with ⌘P. Click a speaker label to rename that speaker.
- Pick a **Glossary** in the source menu so names and terms are always written the
  same way. Edit glossaries in Settings → Glossary.
- **Ask the Meeting** answers questions about what has been said so far. It needs the
  notes model from Settings → Models. During a meeting it answers in a pause in speech,
  so live captions keep up; the answer may take a little longer.

## Floating captions

- ⌘⇧C, or **Floating Captions** in the Live toolbar, shows captions in a window that
  stays above your meeting app. Drag it from any empty spot.
- Choose English only, bilingual, or the source language only, and the text size.
  Click a line to see its other language.
- Star a line to pin it; pinned lines can be copied or exported later.
- **Transparency** sets how much of the window behind shows through.
- The floating captions' options can read English captions aloud, with a choice of
  voice and speed.

## When captions fall behind

- The health pill in the toolbar and the floating captions shows when translation is
  running behind speech. **Catch up** skips ahead to the present.
- Settings → General → **When behind** chooses what happens automatically.
- Skipped speech is kept. Open the meeting in Meetings and click
  **Recover Skipped Captions** to translate it afterwards.

## Meetings

- **Meetings** (⌘2) lists past meetings by day. Search them, and filter by date,
  speaker, or tag.
- The inspector shows each meeting's **Notes**, generated when the meeting stops.
  **Write Again** regenerates them, and **Ask the Meeting** works here too.
- Export the notes as Markdown or PDF from **Notes**.
- Select a speaker label to change its name in the saved meeting.
- In the glossary, sort terms by frequency. Add a term to a glossary profile to use it in future meetings.
- **Export…** saves a meeting as Markdown, SRT or VTT subtitles, plain text, CSV, or
  JSON Lines, or exports only the pinned lines.

## Translate

- **Translate** (⌘3) translates typed or pasted text. Pick the direction in the toolbar,
  or swap it.
- Dictate into the source pane with the microphone, or fill it with the latest caption.
- English to Japanese shows a back-translation so you can check your reply, and can
  read the Japanese aloud.

## Study meeting terms

- Open **Learn** (⌘5) and select a saved meeting. Reveal answers as you review the cards.
- Export flashcards as CSV or Anki text. In Anki, choose **File → Import** to open the text file.

- Settings → General → **Learning mode** adds furigana and word glosses to Japanese captions.
- Meetings → inspector → **Words** lists the vocabulary from a meeting, with export to
  CSV or an Anki deck.

## Meeting auto-start

- MimiVox can offer to start captions when a meeting app starts playing audio. Turn
  this on or off in Settings → Meeting Apps.
- Each app can be set to **Never capture**, which leaves it out of system audio, or
  **Only this**, which captures that app alone.

## Settings

Open Settings with ⌘,.

- **General**: app language, language pair, speed, catch-up behavior, learning mode.
- **Audio**: microphone and system-audio devices.
- **Meeting Apps**: auto-start offers and per-app capture rules.
- **Glossary**: glossary profiles and their terms.
- **Models**: verify, download, update, or remove models, including the notes model.
  **Download Source** points downloads at a model mirror. Use HTTPS when the mirror needs
  an access token. HTTP mirrors work without a token.
- **Privacy**: how long transcripts are kept, and **Delete All History**.

## Keyboard shortcuts

- ⌘R: start captions
- ⌘.: stop captions
- ⌘P: pause or resume captions
- ⌘⇧C: show or hide floating captions
- ⌘1, ⌘2, ⌘3, ⌘4, ⌘5: Live, Meetings, Translate, Home, Learn
- ⌘,: Settings
- ⌘?: this help
- ⌘Q: quit. During a meeting it asks whether to end the meeting and quit, or end it and stay open; ending saves the transcript.

## Troubleshooting

- **System Audio captions stay empty**: open System Settings → Privacy & Security →
  Screen & System Audio Recording and turn MimiVox on. Until you do, macOS records
  silence. On macOS before 14.2, choose a loopback device such as BlackHole in
  Settings → Audio.
- **No microphone captions**: allow MimiVox in System Settings → Privacy & Security →
  Microphone, and check the device in Settings → Audio.
- **Server failed**: click **Retry**. The server log is `logs/helper.log` in
  `~/Library/Application Support/MimiVox`.
- **Notes or Ask unavailable**: download the notes model in Settings → Models.

## What's new

Choose **Help → Changelog**, or **View Changelog…** in About MimiVox, to see what
changed in each version.
