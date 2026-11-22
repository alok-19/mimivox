import AppKit
import Combine
import Foundation
import MeetingTranslatorCore
import Observation

struct TranslationPane: Equatable {
    var input = ""
    var output = ""
    var backTranslation = ""
}

@MainActor
@Observable
final class QuickTranslateDraft {
    var direction: TranslationDirection
    private var panes: [TranslationDirection: TranslationPane]
    private var translating: Set<TranslationDirection> = []

    init(
        direction: TranslationDirection = .jaToEn, input: String = "", output: String = "", backTranslation: String = ""
    ) {
        self.direction = direction
        panes = [direction: TranslationPane(input: input, output: output, backTranslation: backTranslation)]
    }

    var input: String {
        get { panes[direction]?.input ?? "" }
        set { panes[direction, default: TranslationPane()].input = newValue }
    }

    var output: String {
        get { panes[direction]?.output ?? "" }
        set { panes[direction, default: TranslationPane()].output = newValue }
    }

    var backTranslation: String { panes[direction]?.backTranslation ?? "" }
    var isTranslating: Bool { translating.contains(direction) }
    var trimmedInput: String { input.trimmingCharacters(in: .whitespacesAndNewlines) }

    func swapDirection() {
        let previous = panes[direction]
        direction = direction == .jaToEn ? .enToJa : .jaToEn
        guard let previous, !previous.output.isEmpty else { return }
        panes[direction] = TranslationPane(input: previous.output, output: previous.input)
    }

    func translate(using store: TranslatorStore) async {
        let direction = direction
        let text = trimmedInput
        guard !text.isEmpty, !translating.contains(direction) else { return }
        translating.insert(direction)
        defer { translating.remove(direction) }
        guard let response = await store.quickTranslate(text, direction: direction) else { return }
        panes[direction, default: TranslationPane()].output = response.translatedText
        panes[direction, default: TranslationPane()].backTranslation = response.backTranslation ?? ""
    }
}

/// Owns everything that must outlive the main window: the store, its event
/// stream, the floating caption panel, status polling and meeting autostart.
@MainActor
final class AppModel: ObservableObject {
    let store = TranslatorStore()
    let quickTranslate = QuickTranslateDraft()
    let meetingsSearch = MeetingsSearch(query: ProcessInfo.processInfo.environment["MIMIVOX_LIBRARY_QUERY"] ?? "")
    @Published var selectedSection: AppSection
    @Published var settingsTab: SettingsTab
    private var opensSettingsAtLaunch: Bool

    private let captionPanel = FloatingCaptionPanelController()
    private var autostartMonitor: MeetingAutostartMonitor?
    private var timers: [Timer] = []
    private var cancellables: Set<AnyCancellable> = []

    /// What the menus need, republished only when it changes so menus don't
    /// re-evaluate on every caption event.
    @Published private(set) var canStartCaptions = false
    @Published private(set) var canStopCaptions = false
    @Published private(set) var canPauseCaptions = false
    @Published private(set) var captionsPaused = false
    @Published private(set) var showsCaptionPanel = false

    init() {
        let target = ProcessInfo.processInfo.environment["MIMIVOX_OPEN_SECTION"].flatMap(LaunchTarget.init)
        let demoCaptions = HUDDemo.replayWAV != nil
        selectedSection = target?.section ?? (demoCaptions ? .live : .home)
        settingsTab = target?.settingsTab ?? .general
        opensSettingsAtLaunch = target?.settingsTab != nil
    }

    /// True once per launch, so reopening the main window doesn't reopen Settings.
    func takeSettingsLaunch() -> Bool {
        defer { opensSettingsAtLaunch = false }
        return opensSettingsAtLaunch
    }

    func start() {
        store.objectWillChange
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in self?.syncMenuState() }
            .store(in: &cancellables)
        store.$status
            .receive(on: DispatchQueue.main)
            .sink { [weak self] _ in
                guard let self, !store.isCaptionStreamActive else { return }
                captionPanel.close()
            }
            .store(in: &cancellables)
        captionPanel.$isVisible
            .receive(on: DispatchQueue.main)
            .sink { [weak self] in self?.showsCaptionPanel = $0 }
            .store(in: &cancellables)
        NotificationCenter.default.publisher(for: .toggleHUDRequested)
            .sink { [weak self] _ in MainActor.assumeIsolated { self?.toggleCaptionPanel() } }
            .store(in: &cancellables)

        // Slow pulls while idle, fast pulls while live so lag flags stay near realtime.
        timers = [
            Timer(timeInterval: 5, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated { self?.refresh(whileActive: false) }
            },
            Timer(timeInterval: 1, repeats: true) { [weak self] _ in
                MainActor.assumeIsolated { self?.refresh(whileActive: true) }
            },
        ]
        // .common keeps polling while a menu is open or the window is resizing.
        for timer in timers { RunLoop.main.add(timer, forMode: .common) }

        autostartMonitor = MeetingAutostartMonitor(store: store)
        autostartMonitor?.start()

        Task {
            await store.startHelper()
            await runDemoIfRequested()
        }
        store.startEventStream()
    }

    func startCaptions() {
        Task { await store.startLiveSession() }
    }

    func stopCaptions() {
        Task { await store.stopSession() }
    }

    func togglePauseCaptions() {
        Task { await store.togglePauseResume() }
    }

    func toggleCaptionPanel() {
        guard store.isCaptionStreamActive else {
            captionPanel.close()
            return
        }
        captionPanel.toggle(store: store)
    }

    private func syncMenuState() {
        if canStartCaptions != store.canStartSession { canStartCaptions = store.canStartSession }
        if canStopCaptions != store.canEndSession { canStopCaptions = store.canEndSession }
        if canPauseCaptions != store.canPauseResumeSession { canPauseCaptions = store.canPauseResumeSession }
        if captionsPaused != store.isSessionPaused { captionsPaused = store.isSessionPaused }
    }

    private func refresh(whileActive: Bool) {
        guard store.isSessionActive == whileActive else { return }
        Task { try? await store.refresh() }
    }

    /// Debug hooks (see HUDDemo): once the models are ready, replay a WAV, once, and open the HUD.
    private func runDemoIfRequested() async {
        if HUDDemo.downloadsModels {
            for _ in 0..<20 {
                if case .needsDownload = store.setup {
                    await store.downloadModels()
                    break
                }
                try? await Task.sleep(nanoseconds: 500_000_000)
            }
        }
        guard HUDDemo.replayWAV != nil else { return }
        guard await modelsBecameReady() else { return }
        if let wav = HUDDemo.replayWAV {
            store.settings.replayAudioPath = wav
            // Long enough that the session is still live when the HUD is captured.
            store.settings.replayDurationSeconds = 36
            await store.replayWAV()
            try? await Task.sleep(nanoseconds: 500_000_000)
            captionPanel.show(store: store)
        }
    }

    private func modelsBecameReady() async -> Bool {
        for _ in 0..<600 {
            if store.isSetupReady { return true }
            try? await Task.sleep(nanoseconds: 500_000_000)
        }
        return false
    }
}

struct LaunchTarget {
    var section: AppSection?
    var settingsTab: SettingsTab?

    init?(_ value: String) {
        if let section = AppSection(rawValue: value) {
            self.section = section
            return
        }
        let name = value == "glossary" ? "settings-glossary" : value
        if name == "settings" {
            settingsTab = .general
        } else if name.hasPrefix("settings-"),
            let tab = SettingsTab(rawValue: String(name.dropFirst("settings-".count)))
        {
            settingsTab = tab
        } else {
            return nil
        }
    }
}
