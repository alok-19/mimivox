import MeetingTranslatorCore
import SwiftUI

/// Every screen and state the snapshot tool renders. Add a `SnapshotScreen` here
/// when a new screen or state ships, then rerun the tool.
@MainActor
enum SnapshotCatalog {
    private static let window = MainWindow.defaultSize
    private static let minimumWindow = MainWindow.minSize

    static var all: [SnapshotSpec] {
        (home + live + firstRun + meetings + learn + translate + settings + hud + info).flatMap { $0.specs() }
    }

    private typealias Fixtures = SnapshotFixtures
    private static let ready = HelperPhase.ready(port: 8765)

    private struct TranslateText {
        var direction: TranslationDirection = .jaToEn
        var input = ""
        var output = ""
        var backTranslation = ""
    }

    private static func app(
        _ name: String, _ section: AppSection, size: CGSize = window, state: SnapshotState,
        search: String = "", translate: TranslateText = TranslateText(), preferences: [String: Any] = [:]
    ) -> SnapshotScreen {
        SnapshotScreen(name: name, size: size, preferences: preferences) {
            AnyView(
                RootView(
                    selection: .constant(section),
                    quickTranslate: QuickTranslateDraft(
                        direction: translate.direction, input: translate.input,
                        output: translate.output, backTranslation: translate.backTranslation
                    ),
                    meetingsSearch: MeetingsSearch(query: search)
                )
                .environmentObject(TranslatorStore.snapshotStore(state))
            )
        }
    }

    private static func liveScreens(_ name: String, state: SnapshotState) -> [SnapshotScreen] {
        [
            app(name, .live, state: state),
            app("\(name)-min", .live, size: minimumWindow, state: state),
        ]
    }

    private static var home: [SnapshotScreen] {
        let idle = SnapshotState(status: Fixtures.status("idle"), setup: .ready, helperPhase: ready)
        return [
            app("home-ready", .home, state: idle),
            app("home-ready-min", .home, size: minimumWindow, state: idle),
            app(
                "home-setup", .home,
                state: SnapshotState(
                    status: Fixtures.status("idle", readiness: Fixtures.missingReadiness),
                    setup: .needsDownload(totalBytes: 1_714_868_108, freeDiskBytes: 48_300_000_000),
                    helperPhase: ready
                )
            ),
            app(
                "home-helper-failed", .home,
                state: SnapshotState(
                    helperPhase: .failed(.exited(code: 1, stderrTail: "ModuleNotFoundError: No module named 'mimivox'"))
                )
            ),
        ]
    }

    private static var live: [SnapshotScreen] {
        let healthy = Fixtures.health(label: "Translating")
        let behind = LatencyHealth(
            state: .behind, backlogSeconds: 9.5,
            reasons: [LatencyReason(code: "translate_slow", message: "Translation is running behind speech")],
            catchUpAvailable: true, recommendedMode: "fast"
        )
        let screens: [(String, SnapshotState)] = [
            (
                "live-idle",
                SnapshotState(
                    status: Fixtures.status("idle"), settings: Fixtures.settings(), devices: Fixtures.devices,
                    helperPhase: ready
                )
            ),
            ("live-connecting", SnapshotState(helperPhase: .starting(port: 8765))),
            (
                "live-starting",
                SnapshotState(
                    status: Fixtures.status(
                        "loading_models",
                        health: Fixtures.health(label: "Loading models", audio: false, asr: false, translation: false)),
                    devices: Fixtures.devices, helperPhase: ready
                )
            ),
            (
                "live-captions",
                SnapshotState(
                    status: Fixtures.status("listening", health: healthy), devices: Fixtures.devices,
                    helperPhase: ready, liveEvents: Fixtures.liveEvents(), pinned: [3]
                )
            ),
            (
                "live-captions-learning",
                SnapshotState(
                    status: Fixtures.status("listening", health: healthy, learning: true),
                    settings: Fixtures.settings(learning: true),
                    devices: Fixtures.devices, helperPhase: ready,
                    liveEvents: Array(Fixtures.liveEvents(learning: true).prefix(5))
                )
            ),
            (
                "live-paused",
                SnapshotState(
                    status: Fixtures.status(
                        "paused", health: Fixtures.health(label: "Paused", asr: false, translation: false)),
                    devices: Fixtures.devices, helperPhase: ready, liveEvents: Fixtures.liveEvents(withPartial: false)
                )
            ),
            (
                "live-behind-catch-up",
                SnapshotState(
                    status: Fixtures.status("listening", health: Fixtures.health(latency: behind)),
                    devices: Fixtures.devices, helperPhase: ready, liveEvents: Fixtures.liveEvents()
                )
            ),
            (
                "live-runtime-error",
                SnapshotState(
                    status: Fixtures.status(
                        "degraded", health: Fixtures.health(degraded: true),
                        error: "Audio input stopped: device disconnected"),
                    devices: Fixtures.devices,
                    helperPhase: ready, liveEvents: Fixtures.liveEvents(withPartial: false)
                )
            ),
            (
                "live-silent-system-audio",
                SnapshotState(
                    status: Fixtures.status("listening", health: Fixtures.health(label: "Listening")),
                    devices: Fixtures.devices,
                    notice: .warning(
                        "No sound yet. If a call is playing, check System Audio permission.",
                        recovery: .openPrivacySettings(.systemAudio)),
                    helperPhase: ready,
                    liveEvents: [MeetingEvent(type: "SessionStartedEvent", message: "Session started")]
                )
            ),
            (
                "live-helper-failed",
                SnapshotState(
                    helperPhase: .failed(.exited(code: 1, stderrTail: "ModuleNotFoundError: No module named 'mimivox'"))
                )
            ),
            (
                "live-session-ended",
                SnapshotState(
                    status: Fixtures.status(
                        "completed", health: Fixtures.health(audio: false, asr: false, translation: false)),
                    devices: Fixtures.devices, helperPhase: ready, liveEvents: Fixtures.liveEvents(withPartial: false)
                )
            ),
        ]
        let asked = SnapshotState(
            status: Fixtures.status("completed", health: Fixtures.health(audio: false, asr: false, translation: false)),
            devices: Fixtures.devices, helperPhase: ready, liveEvents: Fixtures.liveEvents(withPartial: false),
            askAnswer: AskAnswer(
                answer: "The new quote from Tanaka is 20 percent higher than last month, and a reply is due next week.",
                lineIDs: [1, 2, 3])
        )
        var unanswered = asked
        unanswered.askAnswer = AskAnswer(
            answer: "The transcript does not say what the budget is.",
            askInJapanese: JapanesePhrase(ja: "予算はいくらですか？", romaji: "yosan wa ikura desu ka?"))
        let askScreens = [
            app("live-ask-answer", .live, state: asked, preferences: ["live.showsInspector": true]),
            app(
                "live-ask-the-room", .live, state: unanswered, preferences: ["live.showsInspector": true]),
            app(
                "live-ask-answer-min", .live, size: minimumWindow, state: asked,
                preferences: ["live.showsInspector": true]),
        ]
        return screens.flatMap { liveScreens($0.0, state: $0.1) } + askScreens
    }

    // MARK: First run

    private static var firstRun: [SnapshotScreen] {
        func firstRun(_ name: String, _ setup: SetupState?, helper: HelperPhase = ready)
            -> [SnapshotScreen]
        {
            liveScreens(
                name,
                state: SnapshotState(
                    status: setup == nil ? nil : Fixtures.status("idle", readiness: Fixtures.missingReadiness),
                    setup: setup, devices: Fixtures.devices, helperPhase: helper
                ))
        }
        let total: Int64 = 1_714_868_108
        func downloading(eta: Double?) -> SetupState {
            .downloading(.init(doneBytes: 612_000_000, totalBytes: total, bytesPerSecond: 23_400_000, etaSeconds: eta))
        }
        return [
            firstRun("first-run-getting-ready", nil, helper: .starting(port: 8765)),
            firstRun("first-run-needs-download", .needsDownload(totalBytes: total, freeDiskBytes: 48_300_000_000)),
            firstRun("first-run-downloading", downloading(eta: 47)),
            firstRun("first-run-downloading-no-eta", downloading(eta: nil)),
            firstRun("first-run-loading", .loading),
            firstRun("first-run-offline", .failed(.init(reason: .offline, message: "LocalEntryNotFoundError"))),
            firstRun("first-run-disk-full", .failed(.init(reason: .diskFull, message: "No space left on device"))),
            firstRun(
                "first-run-error", .failed(.init(reason: .error, message: "Checksum mismatch for model.safetensors"))),
        ].flatMap { $0 }
    }

    // MARK: Meetings

    private static func inspector(_ tab: MeetingInspectorTab) -> [String: Any] {
        ["meetings.showsInspector": true, "meetings.inspectorTab": tab.rawValue]
    }

    private static var meetings: [SnapshotScreen] {
        let idle = Fixtures.status("idle")
        func state(
            withGap: Bool = false, library: LibrarySearchResponse = Fixtures.librarySearch(query: ""),
            focusLine: Int? = nil
        ) -> SnapshotState {
            SnapshotState(
                status: idle, settings: Fixtures.settings(), helperPhase: ready, archivedSessions: Fixtures.archived,
                librarySearch: library, review: Fixtures.reviewDetail(withGap: withGap),
                vocabulary: Fixtures.vocabulary, reviewFocusLine: focusLine
            )
        }
        func both(_ name: String, state: SnapshotState, search: String = "", preferences: [String: Any] = [:])
            -> [SnapshotScreen]
        {
            [
                app(name, .meetings, state: state, search: search, preferences: preferences),
                app(
                    "\(name)-min", .meetings, size: minimumWindow, state: state, search: search,
                    preferences: preferences),
            ]
        }
        let noResults = LibrarySearchResponse(
            query: "zzz", totalSessions: 3, results: [], speakers: ["Tanaka", "Suzuki"], tags: ["weekly"])
        return [
            both(
                "meetings-empty",
                state: SnapshotState(
                    status: idle, helperPhase: ready, librarySearch: Fixtures.emptyLibrary
                )),
            both("meetings-list", state: state()),
            both("meetings-focused-line", state: state(focusLine: 7)),
            both("meetings-recover-skipped", state: state(withGap: true)),
            both("meetings-searching", state: state(library: Fixtures.librarySearch()), search: "cause"),
            both("meetings-search-no-results", state: state(library: noResults), search: "zzz"),
            both("meetings-inspector-notes", state: state(), preferences: inspector(.notes)),
            both("meetings-inspector-notes-empty", state: state(withGap: true), preferences: inspector(.notes)),
            both("meetings-inspector-words", state: state(), preferences: inspector(.words)),
            both("meetings-inspector-info", state: state(), preferences: inspector(.info)),
        ].flatMap { $0 }
    }

    private static var learn: [SnapshotScreen] {
        let empty = SnapshotState(status: Fixtures.status("idle"), helperPhase: ready)
        var saved = empty
        saved.archivedSessions = Fixtures.archived
        saved.review = Fixtures.reviewDetail(withGap: false)
        saved.vocabulary = Fixtures.vocabulary
        return [
            app("learn-empty", .learn, state: empty),
            app("learn-review", .learn, state: saved),
            app("learn-review-min", .learn, size: minimumWindow, state: saved),
        ]
    }

    // MARK: Translate

    private static var translate: [SnapshotScreen] {
        let state = SnapshotState(status: Fixtures.status("idle"), helperPhase: ready)
        func both(_ name: String, _ text: TranslateText = TranslateText()) -> [SnapshotScreen] {
            [
                app(name, .translate, state: state, translate: text),
                app("\(name)-min", .translate, size: minimumWindow, state: state, translate: text),
            ]
        }
        return [
            both("translate-empty"),
            both(
                "translate-ja-en",
                TranslateText(
                    input: "来週の月曜日までに修正版を出せますか。",
                    output: "Can you get a fix out by next Monday?"
                )),
            both("translate-en-ja-empty", TranslateText(direction: .enToJa)),
            both(
                "translate-en-ja-result",
                TranslateText(
                    direction: .enToJa,
                    input: "Thanks for the update. I'll send the quote by tomorrow.",
                    output: "ご連絡ありがとうございます。見積もりは明日までにお送りします。",
                    backTranslation: "Thank you for contacting me. I will send the quote by tomorrow."
                )),
        ].flatMap { $0 }
    }

    // MARK: Settings

    private static func settingsScreen(_ name: String, _ tab: SettingsTab, state: SnapshotState) -> SnapshotScreen {
        SnapshotScreen(name: name, size: CGSize(width: SettingsPage.width, height: tab.height)) {
            AnyView(
                SettingsPage(tab: tab)
                    .background(Color(nsColor: .windowBackgroundColor))
                    .environmentObject(TranslatorStore.snapshotStore(state))
            )
        }
    }

    private static var settings: [SnapshotScreen] {
        let installed = SnapshotState(
            status: Fixtures.status("idle"), settings: Fixtures.settings(), devices: Fixtures.devices,
            helperPhase: ready
        )
        let total: Int64 = 1_714_868_108
        let needsDownload = SnapshotState(
            status: Fixtures.status("idle", readiness: Fixtures.missingReadiness),
            setup: .needsDownload(totalBytes: total, freeDiskBytes: 48_300_000_000),
            devices: Fixtures.devices, helperPhase: ready
        )
        let downloading = SnapshotState(
            status: Fixtures.status("idle", readiness: Fixtures.missingReadiness),
            setup: .downloading(
                .init(doneBytes: 612_000_000, totalBytes: total, bytesPerSecond: 23_400_000, etaSeconds: 47)),
            devices: Fixtures.devices, helperPhase: ready
        )
        let saveFailed = SnapshotState(
            status: Fixtures.status("idle"), settings: Fixtures.settings(), devices: Fixtures.devices,
            notice: .error("Settings not saved: the server did not answer.", command: "Save settings"),
            helperPhase: ready
        )
        let noGlossary = SnapshotState(status: Fixtures.status("idle"), devices: Fixtures.devices, helperPhase: ready)
        return [
            settingsScreen("settings-general", .general, state: installed),
            settingsScreen("settings-general-save-failed", .general, state: saveFailed),
            settingsScreen("settings-audio", .audio, state: installed),
            settingsScreen("settings-meeting-apps", .meetingApps, state: installed),
            settingsScreen("settings-glossary", .glossary, state: installed),
            settingsScreen("settings-glossary-empty", .glossary, state: noGlossary),
            settingsScreen("settings-models", .models, state: installed),
            settingsScreen("settings-models-needs-download", .models, state: needsDownload),
            settingsScreen("settings-models-downloading", .models, state: downloading),
            settingsScreen("settings-privacy", .privacy, state: installed),
        ]
    }

    // MARK: Floating caption HUD

    private static func hudScreen(
        _ name: String, mode: String, size: String = "compact", events: [MeetingEvent],
        status: MeetingStatusResponse? = nil,
        noStatus: Bool = false, phase: HelperPhase? = nil, hovering: Bool = false, highContrast: Bool = false,
        panel: CGSize = CGSize(width: 836, height: 336)
    ) -> SnapshotScreen {
        let live =
            noStatus ? nil : (status ?? Fixtures.status("listening", health: Fixtures.health(label: "Translating")))
        return SnapshotScreen(
            name: name, size: panel,
            preferences: [HUDPreferences.displayModeKey: mode, HUDPreferences.sizeKey: size]
        ) {
            AnyView(
                ZStack {
                    HUDBackdrop()
                    makeCaptionHUDSnapshotView(
                        store: TranslatorStore.snapshotStore(
                            SnapshotState(status: live, helperPhase: phase ?? ready, liveEvents: events)
                        ), hovering: hovering, highContrast: highContrast)
                }
            )
        }
    }

    private static var info: [SnapshotScreen] {
        [
            SnapshotScreen(name: "info-about", size: CGSize(width: 340, height: 300)) {
                AnyView(AboutView().background(Color(nsColor: .windowBackgroundColor)))
            },
            SnapshotScreen(name: "info-help", size: CGSize(width: 620, height: 2600)) { AnyView(HelpView()) },
            SnapshotScreen(name: "info-changelog", size: CGSize(width: 560, height: 1100)) { AnyView(ChangelogView()) },
        ]
    }

    private static var hud: [SnapshotScreen] {
        let events = Fixtures.liveEvents()
        let behind = LatencyHealth(
            state: .behind, backlogSeconds: 9.5,
            reasons: [LatencyReason(code: "translate_slow", message: "Translation is running behind speech")],
            catchUpAvailable: true
        )
        return [
            hudScreen("hud-bilingual", mode: "bilingual", events: events),
            hudScreen("hud-english-only", mode: "englishOnly", events: events),
            hudScreen("hud-japanese-only", mode: "japaneseOnly", events: events),
            hudScreen(
                "hud-bilingual-large", mode: "bilingual", size: "large", events: events,
                panel: CGSize(width: 1000, height: 480)),
            hudScreen("hud-waiting-for-captions", mode: "bilingual", events: []),
            hudScreen(
                "hud-behind", mode: "bilingual", events: events,
                status: Fixtures.status("listening", health: Fixtures.health(latency: behind))
            ),
            hudScreen(
                "hud-behind-hover", mode: "bilingual", events: events,
                status: Fixtures.status("listening", health: Fixtures.health(latency: behind)), hovering: true
            ),
            hudScreen("hud-hover", mode: "bilingual", events: events, hovering: true),
            hudScreen("hud-high-contrast", mode: "bilingual", events: events, highContrast: true),
            hudScreen("hud-paused", mode: "bilingual", events: events, status: Fixtures.status("paused")),
            hudScreen(
                "hud-failed", mode: "bilingual", events: events,
                status: Fixtures.status("failed", error: "The speech model stopped responding.")
            ),
            hudScreen("hud-connecting", mode: "bilingual", events: [], noStatus: true, phase: .starting(port: 8765)),
            hudScreen(
                "hud-loading-models", mode: "bilingual", events: [],
                status: Fixtures.status(
                    "loading_models", health: Fixtures.health(audio: false, asr: false, translation: false))
            ),
        ]
    }
}

/// Light and dark desktop stand-ins for the always-dark HUD: the `-light`
/// snapshots draw it over a pale desktop, the `-dark` ones over a near-black one.
private struct HUDBackdrop: View {
    @Environment(\.colorScheme) private var scheme

    var body: some View {
        LinearGradient(
            colors: scheme == .dark
                ? [Color(red: 0.12, green: 0.13, blue: 0.17), Color(red: 0.03, green: 0.04, blue: 0.06)]
                : [Color(red: 1, green: 1, blue: 1), Color(red: 0.93, green: 0.95, blue: 0.99)],
            startPoint: .top, endPoint: .bottom
        )
    }
}
