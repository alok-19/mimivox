# Changelog

All notable changes to MimiVox are recorded here. This file is bundled into the
app and shown from **About MimiVox → View Changelog** and **Help → Changelog**.

Format: `## [version] - YYYY-MM-DD`, then `### Added` / `### Changed` / `### Fixed`
sections of `- ` bullets, newest release first. Collect upcoming changes under
`## [Unreleased]`; on release, rename it to the new version and set the same
version as `CFBundleShortVersionString` in `BundleResources/Info.plist`
(`MeetingTranslatorCoreChecks` fails if the two differ).

## [1.1.2] - 2026-11-24

### Added
- Check for and install updates from the MimiVox menu.

### Fixed
- Open the app when the display language is Japanese.
- Update the sidebar labels when you change the app language.
- Show the MimiVox name in macOS process lists and crash reports.

## [1.1.1] - 2026-11-23

### Changed
- MimiVox uses HTTPS and a new access token each time it connects to its local helper. If you run the server from a terminal, open the current link from that terminal to use the browser interface.
- Private model mirrors require HTTPS when you use an access token. Enter the token again in **Settings → Models → Download Source** after you upgrade.

### Fixed
- The local helper no longer writes captions or translations to its log during live meetings.
- MimiVox checks session IDs before it reads or writes meeting data.
- The helper rejects audio requests that exceed the size limit.
- The browser interface renders caption cards correctly.

## [1.1.0] - 2026-11-23

### Added
- **Home** page (⌘4) shows whether MimiVox is ready for a meeting. If the caption models are missing, download them there. It also links to Live, Meetings, Learn, and Translate.
- **Learn** page (⌘5). Pick a saved meeting and review its words as flashcards, in either direction (Japanese → English or English → Japanese). Reveal each answer with **Show answer**. Cards show the reading and an example line from the meeting.
- Japanese app language. Choose **App language** in Settings → General to show menus, Help, dates, and durations in Japanese. Help is available in Japanese too.
- Meeting notes export as **PDF** as well as Markdown, from the export button on **Notes**, and **Copy as Markdown** is there too.
- The **Words** tab in a saved meeting lists the words that came up, with readings and counts. Sort them by frequency or alphabetically, and add any word to a glossary profile to use it in future meetings.
- A **Glossary** section on the Words tab lists the glossary terms that came up in a saved meeting and how often. Jump to the first line that uses a term, or add a term to a glossary profile.
- Rename a speaker inside a saved meeting by selecting its label. The new name also updates search for that meeting.
- Export flashcards for Anki or as a spreadsheet (CSV). The Anki file has one reversed-card note per word, so you get both directions. Re-importing updates existing notes instead of duplicating them.

### Changed
- Ask the meeting, the glossary settings, and the Settings pages are available in Japanese, and dates and times follow the app language.
- Flashcard and vocabulary exports use the language pair the meeting was recorded with, so older meetings export correctly even after you change the current pair.
- The **Help** guide and the app README describe the new Home and Learn pages.

### Removed
- The bundled sample meeting and **Play a Sample Meeting**. Use a recording or a live meeting to try captions.

## [1.0.0] - 2026-11-22

### Changed
- First stable release. It is the same build as 1.0.0-rc.3 and includes everything from the three release candidates below.

## [1.0.0-rc.3] - 2026-11-22

### Added
- Rename a meeting by clicking its title, or with **Rename…** in the meeting list's context menu. Leave the title empty to go back to the generated one. Renamed meetings can be found by their new title in search.
- During a live meeting, when **Ask the meeting** finds no answer in the transcript, it shows the question in everyday business Japanese with romaji under it, so you can ask the room. Copy the Japanese with one click.

### Changed
- Meeting notes now include a short generated title naming the topic (for example "South-facing bedroom sunlight"), used in the meeting list instead of the summary's first sentence ("The group agrees that…"). Meetings with older notes get one when their notes are regenerated.

## [1.0.0-rc.2] - 2026-11-22

### Fixed
- Switching between meetings in Meetings no longer flashes a blank view. The open meeting stays on screen until the next one has loaded, then both panes switch at once.

## [1.0.0-rc.1] - 2026-11-22

### Added
- Help menu with an in-app guide to captions, meetings, translation, settings, shortcuts, and troubleshooting.
- About window with the app version and this changelog.
- ⌘Q asks before quitting. During a meeting you can end it and quit, end it and stay open, or cancel; ending saves the transcript. The idle prompt has "Don't ask again".

### Fixed
- Starting a meeting while notes, Ask, or dictation is running no longer runs two models on the GPU at once, which could stall captions on 16 GB Macs.
- Downloading the notes model or another language pair no longer fails with "Offline-locked" once the speech models are installed. Downloads are always allowed unless you start the server with `MIMIVOX_OFFLINE=1`.
- Notes and Ask no longer stall live captions. Automatic notes for a finished meeting wait until no meeting is live. Ask and the Notes button still work during a meeting: they start in a pause in speech (or after 20 seconds) and hand the GPU back to captions every few tokens, so an answer can take a little longer. Starting a meeting frees the notes model's memory when nothing is using it, and the banner for a running Ask is a calm notice.

## [0.1.0] - 2026-11-22

### Added
- Live captions and translation from system audio, the microphone, or a recording, all on this Mac.
- Built-in system-audio capture on macOS 14.2+, with per-app capture rules; no virtual driver needed.
- Language pairs: Japanese, Chinese, and Korean to English, and English to Japanese.
- Floating captions with English, bilingual, and source-only modes, pinned lines, transparency, and read-aloud.
- On-device speaker labels you can rename.
- Per-meeting glossaries for names and terms.
- Meetings library with search, filters, export, and on-device notes and Ask.
- Caption health indicator with Catch up, and recovery of skipped captions afterwards.
- Translate page with dictation, English-to-Japanese replies with back-translation, and read-aloud.
- Learning mode with furigana, word glosses, and vocabulary export to CSV or Anki.
- Offer to start captions when a meeting app starts playing audio.
- Transcript retention settings and Delete All History.
- First-run setup with a sample meeting.
- Self-contained MimiVox.app with its own Python server.

### Fixed
- Quiet speakers are captioned reliably.
- Translations no longer drop Japanese terms and numbers, or repeat earlier lines.
- Cmd+Q shows a Quitting panel instead of a spinning cursor.
