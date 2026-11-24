import AVFoundation
import Combine
import Foundation
import MeetingTranslatorCore
import SwiftUI

enum LiveSessionSource: String, CaseIterable, Identifiable {
    case microphone = "microphone"
    case systemAudio = "system-audio"

    var id: String { rawValue }

    var title: String {
        switch self {
        case .microphone: "Microphone"
        case .systemAudio: "System audio"
        }
    }

    var systemImage: String {
        switch self {
        case .microphone: "mic.fill"
        case .systemAudio: "speaker.wave.2.fill"
        }
    }
}

enum LiveControlCommand: Equatable {
    case start
    case pause
    case resume
    case end
}

@MainActor
final class TranslatorStore: ObservableObject {
    @Published var status: MeetingStatusResponse?
    /// First-run model setup; nil until the server has answered once.
    @Published private(set) var setup: SetupState?
    private var silenceWatch: Task<Void, Never>?
    @Published var settings: MeetingSettings = MeetingSettings()
    @Published var devices: [AudioDevice] = []
    @Published private(set) var notice: Notice?
    private var noticeExpiry: Task<Void, Never>?
    @Published var isBusy: Bool = false
    @Published var selectedLiveSource: LiveSessionSource = .systemAudio
    @Published private(set) var activeSource: LiveSessionSource?
    @Published var activeLiveCommand: LiveControlCommand?
    @Published var archivedSessions: [ArchivedSessionSummary] = []
    @Published private(set) var archiveLoaded = false
    @Published var librarySearch: LibrarySearchResponse?
    @Published var isSearchingLibrary: Bool = false
    @Published var hasSeenSessionThisRun: Bool = false
    /// Ask the Meeting in Live: questions about the current or most recent session.
    let liveAsk = AskThread()
    /// Set when a cited line is tapped; transcript lists scroll to `index`.
    @Published var scrollRequest: TranscriptScrollRequest?
    @Published var autostartPrefs: MeetingAutostartPrefs = MeetingAutostartPrefs.load() {
        didSet { autostartPrefs.save() }
    }
    let live = LiveSession()
    let review = ReviewSession()
    var transcriptEpoch: UInt64 { live.epoch }

    @Published private(set) var pinnedIndexes: Set<Int> = []
    /// Speaks finalized English captions. Off until the viewer switches it on.
    let readAloud = ReadAloudCoordinator(output: AVSpeechOutput())
    @Published var readAloudVoiceID: String {
        didSet {
            UserDefaults.standard.set(readAloudVoiceID, forKey: HUDPreferences.voiceKey)
            readAloud.voiceID = readAloudVoiceID.isEmpty ? nil : readAloudVoiceID
        }
    }
    @Published var readAloudRate: Double {
        didSet {
            UserDefaults.standard.set(readAloudRate, forKey: HUDPreferences.rateKey)
            readAloud.rate = Float(readAloudRate)
        }
    }
    private var readAloudSubscriptions: Set<AnyCancellable> = []

    /// The settings the server last confirmed; nil until the first load.
    private var syncedSettings: MeetingSettings?
    private var settingsAutosave: AnyCancellable?
    private var pendingGlossarySave: String?

    private let helper: HelperSupervisor
    private let disconnectedAPI = MeetingTranslatorAPIClient(baseURL: URL(fileURLWithPath: "/"))
    private var api: MeetingTranslatorAPIClient { helper.client ?? disconnectedAPI }
    /// Mirrors `helper.phase` for the UI.
    @Published private(set) var helperPhase: HelperPhase = .idle
    private var helperStartTask: Task<HelperPhase, Never>?
    /// Set by `applySnapshotState`: the store shows fixture state only, so the
    /// helper, the network and the event stream stay untouched.
    private var isSnapshotFixture = false
    private var sseTask: Task<Void, Never>?
    private var meetingLoad: Task<Void, Never>?
    /// Bumped on every detach/restart so a cancelled SSE consumer cannot append.
    private var sseStreamID = UUID()
    /// Stops the Core Audio tap for a native system-audio session and flushes
    /// its last upload. Nil when no native capture is running.
    private var nativeSystemAudioStop: (() async -> Void)?
    private var nativeSystemAudioID: UUID?
    /// Which apps the next native capture covers; set by `startLiveSession`.
    private var pendingCaptureScope: CaptureScope = .everything(excluding: [])

    convenience init() {
        self.init(helper: HelperSupervisor(configuration: .current()))
    }

    init(helper: HelperSupervisor) {
        self.helper = helper
        helper.launchEnvironment = {
            let source = ModelSource.current()
            return source.helperEnvironment(token: ModelSourceToken.read(for: source))
        }
        let defaults = UserDefaults.standard
        readAloudVoiceID = defaults.string(forKey: HUDPreferences.voiceKey) ?? ""
        let savedRate = defaults.double(forKey: HUDPreferences.rateKey)
        readAloudRate = savedRate > 0 ? savedRate : Double(AVSpeechUtteranceDefaultSpeechRate)
        readAloud.voiceID = readAloudVoiceID.isEmpty ? nil : readAloudVoiceID
        readAloud.rate = Float(readAloudRate)

        $status
            .sink { [weak self] status in
                self?.readAloud.setPaused(status?.state == "paused")
            }
            .store(in: &readAloudSubscriptions)
        settingsAutosave =
            $settings
            .dropFirst()
            .debounce(for: .milliseconds(600), scheduler: DispatchQueue.main)
            .sink { [weak self] _ in
                Task { @MainActor in await self?.autosaveSettings() }
            }
        // Read the supervisor's current phase so out-of-order hops cannot regress it.
        helper.onPhaseChange = { [weak self, helper] _ in
            Task { @MainActor in self?.helperPhase = helper.phase }
        }
    }

    /// Finalized English captions of the live session, oldest first. A revised
    /// line keeps its id, so the queue can tell a revision from a new line.
    private func readAloudLines() -> [ReadAloudLine] {
        live.rows.compactMap { row in
            guard let text = row.english?.text, !text.isEmpty else { return nil }
            return ReadAloudLine(id: "s\(transcriptEpoch)-\(row.index)", text: text)
        }
    }

    func setReadAloud(enabled: Bool) {
        readAloud.setEnabled(
            enabled,
            currentLines: readAloudLines(),
            paused: isSessionPaused
        )
    }

    /// Pin or unpin a line and mirror the set to the server so it is archived and exported.
    func togglePin(index: Int) {
        if pinnedIndexes.contains(index) {
            pinnedIndexes.remove(index)
        } else {
            pinnedIndexes.insert(index)
        }
        let ids = Array(pinnedIndexes)
        Task {
            do {
                _ = try await api.setPins(ids, sessionID: nil)
                clearNotice(command: "Save pin")
            } catch {
                post(.error("Pin not saved: \(error.localizedDescription)", command: "Save pin"))
            }
        }
    }

    func toggleReviewPin(index: Int) {
        guard let id = review.selectedID else { return }
        var pinned = review.pinned
        if pinned.contains(index) { pinned.remove(index) } else { pinned.insert(index) }
        review.setPinned(pinned)
        let ids = Array(pinned)
        Task {
            do {
                _ = try await api.setPins(ids, sessionID: id)
                clearNotice(command: "Save pin")
            } catch {
                post(.error("Pin not saved: \(error.localizedDescription)", command: "Save pin"))
            }
        }
    }

    /// Writes the pinned lines to a Markdown file under the server's transcripts folder.
    func exportPinned() async {
        await runCommand("Export pinned lines") {
            let response = try await api.export(
                format: "markdown", sessionID: isSessionActive ? nil : status?.sessionID, pinnedOnly: true)
            return response.path.map { .success("Exported pinned lines", path: $0) } ?? .info(response.message)
        }
    }

    var pinnedMarkdown: String {
        bilingualTranscriptRows.pinnedMarkdown(pinned: pinnedIndexes)
    }

    deinit {
        sseTask?.cancel()
        helper.stop()
    }

    var bilingualTranscriptRows: [BilingualTranscriptRow] {
        hasSeenSessionThisRun ? live.rows : []
    }

    var latestJapaneseText: String? {
        bilingualTranscriptRows.latestJapanese?.text
    }

    var latestEnglishText: String? {
        bilingualTranscriptRows.latestEnglish?.text
    }

    var isCaptionStreamActive: Bool {
        guard let status else {
            return false
        }
        if status.health.stopped {
            return false
        }
        switch status.state {
        case "preparing", "loading_models", "listening", "running", "processing", "degraded", "paused":
            return true
        default:
            return false
        }
    }

    var latestCaptionEnglishText: String? {
        isCaptionStreamActive ? latestEnglishText : nil
    }

    /// Live JA preview from AsrPartialEvent / TranslationStartedEvent after the last completed ASR.
    var interimJapaneseText: String? {
        isCaptionStreamActive ? live.interimJapanese : nil
    }

    var isTranslatingLive: Bool {
        if live.translationActive {
            return true
        }
        // Only trust worker flag while a session is actually running.
        guard isSessionActive else { return false }
        return status?.health.translationWorkerActive == true
    }

    var staleCaptionDropped: Bool {
        live.staleCaptionDropped
    }

    var latestCaptionText: String {
        if let text = latestCaptionEnglishText {
            return text
        }
        if !isCaptionStreamActive {
            return status?.modelReadiness.ready == true
                ? "Start a live session to see captions" : "Waiting for setup..."
        }
        if staleCaptionDropped {
            return "Catching up..."
        }
        if isTranslatingLive {
            return "Translating…"
        }
        if interimJapaneseText != nil {
            return "Listening…"
        }
        return "Waiting for translation..."
    }

    var isSetupReady: Bool { setup?.isReady == true }

    /// The setup card replaces the Live page until models are ready. Before the
    /// first answer it shows "Getting ready…", unless startup itself failed.
    var showsSetupCard: Bool {
        guard let setup else { return status == nil && !helperFailed }
        return !setup.isReady
    }

    var helperFailed: Bool {
        if case .failed = helperPhase { return true }
        return false
    }

    var liveStatus: LiveStatus {
        LiveStatus(helper: helperPhase, setup: setup, status: status, latency: latencyHealth)
    }

    var liveStatusDetail: String {
        switch liveStatus {
        case .live: "Captions are keeping up. Lag \(queueLagText)."
        case .catchingUp, .behind: latencyHealth.detailText
        case .failed: failureDetail ?? "Captioning ran into a problem."
        case .setupNeeded: "Finish setup to start captioning."
        case .starting: "MimiVox is getting ready."
        case .paused: "Captioning is paused."
        case .stopping: "Ending the meeting."
        case .idle: "Press Start to caption a meeting."
        }
    }

    var liveSubtitle: String {
        if showsSetupCard { return "Setting up" }
        if isSessionActive {
            let source = (activeSource ?? selectedLiveSource).title
            return status?.activeModels.map { "\(source) · \($0.summary)" } ?? source
        }
        if liveStatus == .failed { return "" }
        return hasEndedSession ? endedSummary : "Ready"
    }

    var endedSummary: String {
        let lines = bilingualTranscriptRows.filter { $0.gap == nil }.count
        let parts = [
            live.endedAt.map { "Ended \($0.formatted(date: .omitted, time: .shortened))" } ?? "Ended",
            live.transcriptDuration.map(MeetingFormat.duration),
            "\(lines) \(lines == 1 ? "line" : "lines")",
        ]
        return parts.compactMap { $0 }.joined(separator: " · ")
    }

    var hasEndedSession: Bool {
        liveStatus == .idle && !bilingualTranscriptRows.isEmpty
    }

    var showsPauseResume: Bool {
        switch status?.state {
        case "listening", "running", "processing", "degraded", "paused": true
        default: false
        }
    }

    var showsStop: Bool {
        isSessionActive && status?.state != "stopping"
    }

    var failureDetail: String? {
        if case .failed(let failure) = helperPhase { return failure.summary }
        if let error = status?.error, !error.isEmpty { return error }
        return nil
    }

    var helperLogURL: URL? {
        guard case .failed = helperPhase else { return nil }
        return helper.configuration.logURL
    }

    var latencyHealth: LatencyHealth {
        guard status?.health.stopped != true else { return LatencyHealth() }
        return live.latency ?? status?.health.latency ?? LatencyHealth()
    }

    func catchUp() async {
        await runCommand("Catch up") {
            let response = try await api.catchUp()
            if response.message == "already caught up" { return .info("Already caught up") }
            var parts: [String] = []
            if response.translationsSkipped > 0 {
                parts.append("\(response.translationsSkipped) lines left untranslated")
            }
            if response.gaps > 0 { parts.append("\(response.gaps) spans skipped") }
            return .info(
                "Caught up" + (parts.isEmpty ? "" : ": " + parts.joined(separator: ", "))
                    + ". Recover them later in Meetings.")
        }
    }

    func recoverSkippedCaptions(id: String) async {
        await runCommand("Recover skipped captions") {
            let response = try await api.recoverSkipped(sessionID: id)
            let detail = try await api.getSession(id: id)
            if review.selectedID == detail.id { review.replaceEvents(detail.events) }
            return .success(response.summary)
        }
    }

    var queueLagText: String {
        let health = status?.health
        return String(format: "%.2fs", max(health?.realtimeLagSeconds ?? 0, health?.queueLagSeconds ?? 0))
    }

    var isSessionActive: Bool {
        switch status?.state {
        case "preparing", "loading_models", "listening", "running", "processing", "degraded", "paused", "stopping":
            return true
        default:
            return false
        }
    }

    var canStartSession: Bool {
        status != nil && isSetupReady && !isBusy && !isSessionActive
    }

    var canPauseResumeSession: Bool {
        guard !isBusy else {
            return false
        }
        switch status?.state {
        case "listening", "running", "processing", "degraded", "paused":
            return true
        default:
            return false
        }
    }

    var canEndSession: Bool {
        !isBusy && hasActiveSession
    }

    var hasActiveSession: Bool {
        switch status?.state {
        case "preparing", "loading_models", "listening", "running", "processing", "degraded", "paused":
            return true
        default:
            return false
        }
    }

    var isSessionPaused: Bool {
        status?.state == "paused"
    }

    var pauseResumeTitle: String {
        isSessionPaused ? "Resume" : "Pause"
    }

    var pauseResumeSystemImage: String {
        isSessionPaused ? "playpause.fill" : "pause.fill"
    }

    func startHelper() async {
        guard !isSnapshotFixture else { return }
        guard case .ready = await startServer() else { return }
        do {
            try await loadSettings()
            try await loadDevices()
            try await refresh()
            if notice?.recovery == .retryServer { dismissNotice() }
            // Stop any session left running by a previous app instance.
            if isSessionActive {
                _ = try? await api.stopSession()
                try await refresh()
                startEventStream()
            }
        } catch {
            post(.error("MimiVox couldn't start: \(error.localizedDescription)", recovery: .retryServer))
        }
    }

    func refresh() async throws {
        guard !isSnapshotFixture else { return }
        let next = try await api.status()
        if next != status { status = next }
        applySetup(next.setup ?? .ready)
        if isSessionActive {
            live.noteTranslationWorker(active: next.health.translationWorkerActive)
        } else if ["completed", "idle", "failed"].contains(next.state) {
            live.clearPipelineFlags()
        }
    }

    private func applySetup(_ next: SetupState) {
        if setup != next { setup = next }
    }

    func downloadModels() async {
        do {
            try await ensureHelperReady()
            applySetup(try await api.startSetupDownload())
        } catch {
            post(.error("Couldn't start the download: \(error.localizedDescription)", command: "Model download"))
        }
    }

    func cancelModelDownload() async {
        do {
            applySetup(try await api.cancelSetupDownload())
        } catch {
            post(.error("Couldn't cancel the download: \(error.localizedDescription)", command: "Model download"))
        }
    }

    func post(_ notice: Notice) {
        self.notice = notice
        noticeExpiry?.cancel()
        guard let lifetime = notice.lifetime else { return }
        noticeExpiry = Task { [weak self] in
            try? await Task.sleep(for: lifetime)
            guard !Task.isCancelled, self?.notice?.id == notice.id else { return }
            self?.notice = nil
        }
    }

    func dismissNotice() {
        noticeExpiry?.cancel()
        notice = nil
    }

    /// Clears the error left by `command` once that command works again.
    private func clearNotice(command: String) {
        if notice?.command == command { dismissNotice() }
    }

    func replayWAV() async {
        await beginNewSession {
            await runCommand("Replay WAV") {
                // Push path/duration from UI so Replay does not use stale disk settings.
                settings = try await api.updateSettings(settings)
                let response = try await api.startSession(
                    source: "replay-file", glossaryProfile: sessionGlossaryProfile, languagePair: settings.languagePair)
                return startOutcome(response)
            }
        }
    }

    /// - Parameter app: capture only this app's audio (the "Start captions for
    ///   <App>?" prompt). Nil follows the include/exclude rules in Settings.
    /// - Parameter override: start this source without changing the picked one.
    func startLiveSession(scopedTo app: MeetingApp? = nil, source override: LiveSessionSource? = nil) async {
        guard isSetupReady else { return }
        if app != nil { selectedLiveSource = .systemAudio }
        let source = override ?? selectedLiveSource
        guard await hasCapturePermission(for: source) else { return }
        pendingCaptureScope = autostartPrefs.scope(forApp: app)
        await beginNewSession {
            await runCommand("Start Live", liveCommand: .start) {
                settings.cloudProviderEnabled = false
                settings.cloudProviderConsent = false
                settings = try await api.updateSettings(settings)
                if usesNativeSystemAudio(for: source) {
                    let response = try await api.startSession(
                        source: source.rawValue, capture: "native", glossaryProfile: sessionGlossaryProfile,
                        languagePair: settings.languagePair)
                    if response.message == "started", let sessionID = response.sessionID {
                        try await beginNativeSystemAudio(sessionID: sessionID)
                    }
                    return startOutcome(response)
                }
                let response = try await api.startSession(
                    source: source.rawValue, glossaryProfile: sessionGlossaryProfile,
                    languagePair: settings.languagePair)
                return startOutcome(response)
            }
        }
        guard isSessionActive else { return }
        activeSource = source
        watchForSilence(source: source)
    }

    /// The microphone is asked for the first time it is picked; a refusal ends in a
    /// link to System Settings. System Audio has no preflight API, so a revoked
    /// permission shows up through `watchForSilence`.
    private func hasCapturePermission(for source: LiveSessionSource) async -> Bool {
        guard source == .microphone,
            Bundle.main.object(forInfoDictionaryKey: "NSMicrophoneUsageDescription") != nil
        else { return true }
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized:
            return true
        case .notDetermined:
            if await AVCaptureDevice.requestAccess(for: .audio) { return true }
        default:
            break
        }
        post(.warning("Microphone access is off for MimiVox.", recovery: .openPrivacySettings(.microphone)))
        return false
    }

    /// A capture that stays silent for a few seconds after start is usually a missing
    /// permission, so say so instead of showing "Listening" forever. Clears itself
    /// when sound arrives or the session ends.
    private func watchForSilence(source: LiveSessionSource) {
        silenceWatch?.cancel()
        let text: String
        let pane: PrivacyPane
        switch source {
        case .systemAudio:
            text = "No sound yet. If a call is playing, check System Audio permission."
            pane = .systemAudio
        case .microphone:
            text = "No sound from the microphone. Check the permission and the input."
            pane = .microphone
        }
        let epoch = live.epoch
        silenceWatch = Task { [weak self] in
            try? await Task.sleep(for: CaptureSilence.grace)
            var posted: UUID?
            while !Task.isCancelled {
                guard let self else { return }
                let stillRunning = live.epoch == epoch && isSessionActive
                let silent = stillRunning && CaptureSilence.isSilent(peakLevel: live.peakLevel)
                if silent, posted == nil {
                    let warning = Notice.warning(text, recovery: .openPrivacySettings(pane))
                    posted = warning.id
                    post(warning)
                }
                if !silent {
                    if let posted, notice?.id == posted { dismissNotice() }
                    return
                }
                try? await Task.sleep(for: .seconds(1))
            }
        }
    }

    private func startOutcome(_ response: CommandResponse) -> Notice? {
        response.message == "started" ? nil : .info(response.message)
    }

    /// The picked glossary profile, or "" (none) when it no longer exists, so a
    /// stale name never makes the server reject the start.
    private var sessionGlossaryProfile: String {
        let name = settings.activeGlossaryProfile
        return settings.glossaryProfiles.contains { $0.name == name } ? name : ""
    }

    /// System Audio on "Built-in" uses a Core Audio process tap (macOS 14.2+).
    /// Choosing a specific input (BlackHole/Loopback) keeps the device path.
    var usesNativeSystemAudio: Bool { usesNativeSystemAudio(for: selectedLiveSource) }

    private func usesNativeSystemAudio(for source: LiveSessionSource) -> Bool {
        guard source == .systemAudio, settings.systemAudioDevice.isEmpty else {
            return false
        }
        if #available(macOS 14.2, *) {
            return true
        }
        return false
    }

    private func beginNativeSystemAudio(sessionID: String) async throws {
        guard #available(macOS 14.2, *) else { return }
        let api = self.api
        let id = UUID()
        let streamer = SystemAudioStreamer(
            send: { samples in
                do {
                    try await api.pushSessionAudio(sessionID: sessionID, samples: samples)
                    return true
                } catch MeetingTranslatorAPIError.httpStatus(409, _, _) {
                    return false  // session ended server-side
                }
            },
            onStopped: { [weak self] in
                Task { @MainActor in
                    await self?.endNativeSystemAudio(id: id)
                }
            }
        )
        let capture = SystemAudioCapture(scope: pendingCaptureScope)
        do {
            try capture.start { streamer.append($0) }
        } catch {
            streamer.cancel()
            _ = try? await api.stopSession()
            throw error
        }
        nativeSystemAudioID = id
        nativeSystemAudioStop = {
            capture.stop()
            streamer.finish()
            await streamer.waitUntilFinished()
        }
    }

    /// Stop native capture; with `id`, only if it is still the active capture.
    private func endNativeSystemAudio(id: UUID? = nil) async {
        if let id, id != nativeSystemAudioID {
            return
        }
        let stop = nativeSystemAudioStop
        nativeSystemAudioStop = nil
        nativeSystemAudioID = nil
        await stop?()
    }

    /// Blank transcript immediately, arm SSE to ignore events until the next
    /// SessionStartedEvent, keep the stream connected, then start the session.
    ///
    /// Keeping SSE up (instead of disconnect→start→reconnect) avoids both:
    /// - flash of prior-session rows from a late SSE replay / cancelled consumer
    /// - dropped TranslationStartedEvent / partials when connecting after start
    private func beginNewSession(_ start: () async -> Void) async {
        hasSeenSessionThisRun = true
        prepareFreshTranscript()
        ensureEventStreamRunning()
        await endNativeSystemAudio()
        await start()
    }

    /// Synchronous UI + SSE gate reset. Must run before any await on Live/Replay.
    private func prepareFreshTranscript() {
        silenceWatch?.cancel()
        live.beginNewSession()
        pinnedIndexes = []
        readAloud.reset()
    }

    private func ensureEventStreamRunning() {
        if sseTask != nil {
            return
        }
        startEventStream(resetEvents: false)
    }

    func stopSession() async {
        silenceWatch?.cancel()
        await runCommand("End", liveCommand: .end) {
            // Flush the last captured audio before the server drains the session.
            await endNativeSystemAudio()
            _ = try await api.stopSession()
            // Archive is written on session_end; refresh list after a short settle.
            try? await Task.sleep(nanoseconds: 400_000_000)
            archivedSessions = (try? await api.listSessions()) ?? archivedSessions
            return nil
        }
    }

    func pauseSession() async {
        await runCommand("Pause", liveCommand: .pause) {
            _ = try await api.pauseSession()
            return nil
        }
    }

    func resumeSession() async {
        await runCommand("Resume", liveCommand: .resume) {
            _ = try await api.resumeSession()
            return nil
        }
    }

    func togglePauseResume() async {
        if isSessionPaused {
            await resumeSession()
        } else {
            await pauseSession()
        }
    }

    func dictate(samples: Data, hint: TranslationDirection) async -> DictationResponse? {
        do {
            try await ensureHelperReady()
            return try await api.dictate(samples: samples, hint: hint == .jaToEn ? "ja" : "en")
        } catch {
            post(.error("Dictation failed: \(error.localizedDescription)"))
            return nil
        }
    }

    func quickTranslate(_ text: String, direction: TranslationDirection? = nil) async -> TranslateResponse? {
        isBusy = true
        defer { isBusy = false }
        do {
            try await ensureHelperReady()
            return try await api.translate(text: text, direction: direction)
        } catch {
            post(.error("Quick translation failed: \(error.localizedDescription)"))
            return nil
        }
    }

    func startEventStream(resetEvents: Bool = true) {
        guard !isSnapshotFixture else { return }
        sseTask?.cancel()
        let streamID = UUID()
        sseStreamID = streamID
        if resetEvents {
            // App-launch / explicit restart: do not show anything until a session starts.
            live.clear()
        }
        sseTask = Task { @MainActor in
            while !Task.isCancelled && sseStreamID == streamID {
                do {
                    for try await event in api.eventStream() {
                        guard !Task.isCancelled, sseStreamID == streamID else { return }
                        ingest(event)
                    }
                } catch {
                    guard !Task.isCancelled, sseStreamID == streamID else { return }
                    try? await Task.sleep(nanoseconds: 2_000_000_000)
                }
            }
        }
    }

    func ingest(_ event: MeetingEvent) {
        if event.type == "SetupStateEvent" {
            if let state = event.setup { applySetup(state) }
            return
        }
        if event.type == "NotesReadyEvent" {
            Task { await notesArrived(sessionID: event.message) }
            return
        }
        if live.ingest(event), readAloud.isEnabled {
            readAloud.update(lines: readAloudLines())
        }
    }

    /// Rename a speaker on the server (later lines, archive) and in the rows on screen.
    func renameSpeaker(id: String, label: String) async {
        let trimmed = label.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        do {
            _ = try await api.renameSpeaker(id: id, label: trimmed)
            live.renameSpeaker(SpeakerTag(id: id, label: trimmed))
            clearNotice(command: "Rename speaker")
        } catch {
            post(
                .error(
                    String(format: L10n.text("Rename failed: %@"), error.localizedDescription),
                    command: "Rename speaker"
                ))
        }
    }

    func renameArchivedSpeaker(id: String, label: String, sessionID: String) async {
        let trimmed = label.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }
        do {
            _ = try await api.renameArchivedSpeaker(id: id, label: trimmed, sessionID: sessionID)
            if review.selectedID == sessionID { openMeeting(id: sessionID) }
            archivedSessions = (try? await api.listSessions()) ?? archivedSessions
            clearNotice(command: "Rename speaker")
        } catch {
            post(
                .error(
                    String(format: L10n.text("Rename failed: %@"), error.localizedDescription),
                    command: "Rename speaker"
                ))
        }
    }

    func addGlossaryTerm(source: String, target: String) {
        guard syncedSettings != nil else {
            post(.error(L10n.text("Settings are still loading")))
            return
        }
        switch settings.addGlossaryTerm(
            source: source,
            target: target,
            fallbackProfileName: L10n.text("Meeting Terms")
        ) {
        case .added(let profileName):
            pendingGlossarySave = profileName
        case .alreadyExists(let profileName):
            post(.info(String(format: L10n.text("Already in %@"), profileName)))
        case nil:
            break
        }
    }

    var glossaryDestination: String {
        if !settings.activeGlossaryProfile.isEmpty,
            settings.glossaryProfiles.contains(where: { $0.name == settings.activeGlossaryProfile })
        {
            return settings.activeGlossaryProfile
        }
        return L10n.text("Meeting Terms")
    }

    func stopHelper() {
        helper.stop()
    }

    /// `stop()` waits up to a few seconds for the helper to exit, so quit runs it off the main thread.
    func stopHelperOffMain() async {
        await Task.detached { [helper] in helper.stop() }.value
    }

    func loadSettings() async throws {
        let loaded = try await api.settings()
        syncedSettings = loaded
        settings = loaded
    }

    /// Pushes edited settings to the server a moment after the last change, so
    /// no page needs a Save button. Skipped until the server's copy has loaded.
    private func autosaveSettings() async {
        guard !isSnapshotFixture, let synced = syncedSettings, settings != synced else { return }
        let sent = settings
        var outgoing = sent
        outgoing.cloudProviderEnabled = false
        outgoing.cloudProviderConsent = false
        do {
            let saved = try await api.updateSettings(outgoing)
            syncedSettings = saved
            // An edit made while the save was in flight wins; the next debounce sends it.
            if settings == sent {
                if saved != settings { settings = saved }
                if let profileName = pendingGlossarySave {
                    pendingGlossarySave = nil
                    post(.success(String(format: L10n.text("Added to %@"), profileName)))
                }
            }
            clearNotice(command: "Save settings")
        } catch {
            pendingGlossarySave = nil
            post(
                .error(
                    String(format: L10n.text("Settings not saved: %@"), error.localizedDescription),
                    command: "Save settings"
                ))
        }
    }

    func loadDevices() async throws {
        devices = try await api.devices()
    }

    /// The server drops a meeting's events when it ends, so an ended Live session is exported by id.
    func export(format: String, sessionID: String? = nil, pinnedOnly: Bool = false) async {
        let id = sessionID ?? (isSessionActive ? nil : status?.sessionID)
        await runCommand(pinnedOnly ? "Export pinned lines" : "Export \(format)") {
            let response = try await api.export(format: format, sessionID: id, pinnedOnly: pinnedOnly)
            return response.path.map { .success(pinnedOnly ? "Exported pinned lines" : "Exported \(format)", path: $0) }
                ?? .info(response.message)
        }
    }

    func refreshArchivedSessions() async {
        guard !isSnapshotFixture else { return }
        do {
            try await ensureHelperReady()
            archivedSessions = try await api.listSessions()
            archiveLoaded = true
        } catch {
            post(.error("Could not list meetings: \(error.localizedDescription)"))
        }
    }

    func openMeeting(id: String, focusLine: Int? = nil) {
        guard !isSnapshotFixture else { return }
        meetingLoad?.cancel()
        review.beginLoading(id: id, focusLine: focusLine)
        meetingLoad = Task { [weak self] in
            guard let self else { return }
            do {
                try await ensureHelperReady()
                let detail = try await api.getSession(id: id)
                guard !Task.isCancelled else { return }
                review.apply(detail)
                clearNotice(command: "Open meeting")
            } catch {
                guard !Task.isCancelled, review.openingID == id else { return }
                review.failLoading()
                post(.error("Couldn't open the meeting: \(error.localizedDescription)", command: "Open meeting"))
            }
        }
    }

    func openNewestMeetingIfNone() {
        guard review.highlightedID == nil, let newest = archivedSessions.first else { return }
        openMeeting(id: newest.id)
    }

    func searchLibrary(query: String, since: String?, until: String?, speaker: String?, tag: String?) async {
        guard !isSnapshotFixture else { return }
        isSearchingLibrary = true
        defer { isSearchingLibrary = false }
        do {
            try await ensureHelperReady()
            let response = try await api.searchSessions(
                query: query.trimmingCharacters(in: .whitespacesAndNewlines),
                since: since, until: until, speaker: speaker, tag: tag
            )
            guard !Task.isCancelled else { return }
            librarySearch = response
        } catch is CancellationError {
        } catch {
            post(.error("Search failed: \(error.localizedDescription)"))
        }
    }

    func updateReviewTags(add: [String] = [], remove: [String] = []) async {
        guard let id = review.selectedID else { return }
        do {
            let tags = try await api.updateSessionTags(id: id, add: add, remove: remove).tags
            if review.selectedID == id { review.tags = tags }
            clearNotice(command: "Update tags")
        } catch {
            post(.error("Tag update failed: \(error.localizedDescription)", command: "Update tags"))
        }
    }

    func renameMeeting(id: String, title: String) async {
        do {
            let headline = try await api.renameArchivedSession(
                id: id, title: title.trimmingCharacters(in: .whitespacesAndNewlines)
            ).headline
            if let index = archivedSessions.firstIndex(where: { $0.id == id }) {
                archivedSessions[index].headline = headline
            }
            clearNotice(command: "Rename meeting")
        } catch {
            post(.error("Rename failed: \(error.localizedDescription)", command: "Rename meeting"))
        }
    }

    func deleteMeeting(id: String) async {
        let neighbour = archivedSessions.neighbour(of: id)
        let wasOpen = review.selectedID == id
        await runCommand("Delete meeting") {
            _ = try await api.deleteArchivedSession(id: id)
            if wasOpen { review.clear() }
            if status?.sessionID == id, !isSessionActive { forgetLiveTranscript() }
            archivedSessions = try await api.listSessions()
            librarySearch?.results.removeAll { $0.id == id }
            return .success("Deleted the meeting")
        }
        if wasOpen, let neighbour, !archivedSessions.contains(where: { $0.id == id }) { openMeeting(id: neighbour) }
    }

    func deleteAllHistory() async {
        await runCommand("Delete all history") {
            let response = try await api.deleteAllHistory()
            review.clear()
            if !isSessionActive { forgetLiveTranscript() }
            archivedSessions = try await api.listSessions()
            librarySearch = nil
            return response.message.map { .success($0) }
        }
    }

    private func forgetLiveTranscript() {
        prepareFreshTranscript()
        liveAsk.reset()
        hasSeenSessionThisRun = false
    }

    /// Settings changes apply on the server immediately; refresh so the pill follows.
    var privacyStatus: PrivacyStatus {
        status?.privacy ?? PrivacyStatus(retentionPolicy: settings.retentionPolicy)
    }

    func loadVocabulary() async {
        guard !isSnapshotFixture, let id = review.selectedID, !review.isLoadingVocabulary else { return }
        review.isLoadingVocabulary = true
        review.vocabularyMessage = nil
        defer { if review.selectedID == id { review.isLoadingVocabulary = false } }
        do {
            try await ensureHelperReady()
            let result = try await api.vocabulary(sessionID: id)
            if review.selectedID == id { review.vocabulary = result.words }
        } catch {
            if review.selectedID == id { review.vocabularyMessage = error.localizedDescription }
        }
    }

    func exportVocabulary(format: String) async {
        guard let id = review.selectedID else { return }
        do {
            let result = try await api.exportVocabulary(format: format, sessionID: id)
            if review.selectedID == id {
                review.vocabularyMessage = result.path.map { "Saved \($0)" } ?? result.message
            }
        } catch {
            if review.selectedID == id { review.vocabularyMessage = error.localizedDescription }
        }
    }

    func studyVocabulary(sessionID: String) async throws -> [VocabularyWord] {
        if isSnapshotFixture {
            return review.selectedID == sessionID ? review.vocabulary ?? [] : []
        }
        try await ensureHelperReady()
        return try await api.vocabulary(sessionID: sessionID).words
    }

    func exportStudyCards(format: String, sessionID: String) async throws -> VocabularyExportResponse {
        if isSnapshotFixture {
            return VocabularyExportResponse(message: L10n.text("Export is unavailable in snapshot previews."))
        }
        try await ensureHelperReady()
        return try await api.exportVocabulary(format: format, sessionID: sessionID)
    }

    /// The server wrote notes for a meeting that just ended: show its new title,
    /// and its notes if that meeting is open.
    private func notesArrived(sessionID: String?) async {
        archivedSessions = (try? await api.listSessions()) ?? archivedSessions
        guard let sessionID, review.selectedID == sessionID, review.notes == nil, !review.isGeneratingNotes else {
            return
        }
        await generateNotes(regenerate: false)
    }

    func generateNotes(regenerate: Bool) async {
        guard let id = review.selectedID, !review.isGeneratingNotes else { return }
        review.isGeneratingNotes = true
        review.notesError = nil
        defer { if review.selectedID == id { review.isGeneratingNotes = false } }
        do {
            try await ensureHelperReady()
            let result = try await api.meetingNotes(sessionID: id, regenerate: regenerate)
            if review.selectedID == id { review.notes = result }
        } catch {
            if review.selectedID == id { review.notesError = error.localizedDescription }
        }
    }

    /// Answer a question about one session: `nil` is the current or most recent one.
    func ask(_ question: String, in thread: AskThread, sessionID: String?) async {
        let trimmed = question.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, !thread.isAsking else { return }
        let generation = thread.begin(trimmed)
        do {
            try await ensureHelperReady()
            let answer = try await api.ask(question: trimmed, sessionID: sessionID)
            thread.finish(generation, answer: answer)
        } catch {
            thread.finish(generation, error: error.localizedDescription)
        }
    }

    func scrollToLine(_ id: Int) {
        scrollRequest = TranscriptScrollRequest(index: id, nonce: (scrollRequest?.nonce ?? 0) + 1)
    }

    func timecode(forLine id: Int) -> String? {
        bilingualTranscriptRows.first(where: { $0.index == id })?.timecode
    }

    func downloadNotesModel() async {
        await runCommand("Download notes model") {
            .info(try await api.modelCommand("download", body: ["notes_llm": true]).message)
        }
    }

    func modelCommand(_ command: String) async {
        await runCommand("Model \(command)") {
            .info(try await api.modelCommand(command).message)
        }
    }

    func resetModelCache() async {
        await runCommand("Reset model cache") {
            .info(try await api.modelCommand("reset-cache", body: ["yes": true]).message)
        }
    }

    private func runCommand(
        _ label: String,
        liveCommand: LiveControlCommand? = nil,
        operation: () async throws -> Notice?
    ) async {
        isBusy = true
        if let liveCommand {
            activeLiveCommand = liveCommand
        }
        defer {
            if let liveCommand, activeLiveCommand == liveCommand {
                activeLiveCommand = nil
            }
            isBusy = false
        }
        do {
            try await ensureHelperReady()
            let outcome = try await operation()
            try await refresh()
            if let outcome {
                post(outcome)
            } else {
                clearNotice(command: label)
            }
        } catch {
            post(.error("\(label) failed: \(error.localizedDescription)", command: label))
        }
    }

    /// Stops the server this app owns and runs startup again.
    func retryHelper() async {
        helper.stop()
        await startHelper()
    }

    private func ensureHelperReady() async throws {
        if case .failed(let failure) = await startServer() {
            throw HelperStartError(failure: failure)
        }
    }

    private func startServer() async -> HelperPhase {
        guard !isSnapshotFixture else { return helperPhase }
        if case .ready = helper.phase {
            return helper.phase
        }
        let task = helperStartTask ?? Task.detached { [helper] in await helper.start() }
        helperStartTask = task
        let phase = await task.value
        helperStartTask = nil
        helperPhase = phase
        return phase
    }
}

struct HelperStartError: LocalizedError {
    var failure: HelperFailure
    var errorDescription: String? { failure.summary }
}

struct TranscriptScrollRequest: Equatable {
    var index: Int
    var nonce: Int
}

/// Fixture state for the UI snapshot tool (`MeetingTranslator --snapshot`).
struct SnapshotState {
    var status: MeetingStatusResponse?
    /// Defaults to the status's own setup, or ready once there is a status.
    var setup: SetupState?
    var settings = MeetingSettings()
    var devices: [AudioDevice] = []
    var notice: Notice?
    var helperPhase: HelperPhase = .idle
    var liveEvents: [MeetingEvent] = []
    var archivedSessions: [ArchivedSessionSummary] = []
    var librarySearch: LibrarySearchResponse?
    /// Opens this archived session for review, with its events, tags and notes.
    var review: ArchivedSessionDetail?
    var vocabulary: [VocabularyWord]?
    var pinned: Set<Int> = []
    var reviewFocusLine: Int?
    /// Live's Ask the Meeting, answered.
    var askAnswer: AskAnswer?
}

extension TranslatorStore {
    /// A store that never starts the helper or touches the network.
    static func snapshotStore(_ state: SnapshotState) -> TranslatorStore {
        let root = FileManager.default.temporaryDirectory.appendingPathComponent("mimivox-snapshot-unused")
        let store = TranslatorStore(
            helper: HelperSupervisor(configuration: HelperConfiguration(projectRoot: root, portPolicy: .fixed(1))))
        store.applySnapshotState(state)
        return store
    }

    private func applySnapshotState(_ state: SnapshotState) {
        isSnapshotFixture = true
        status = state.status
        setup = state.setup ?? state.status.map { $0.setup ?? .ready }
        settings = state.settings
        devices = state.devices
        helperPhase = state.helperPhase
        if let notice = state.notice { self.notice = notice }
        for event in state.liveEvents { live.ingest(event, at: SnapshotFixtures.referenceDate) }
        hasSeenSessionThisRun = !state.liveEvents.isEmpty
        archivedSessions = state.archivedSessions
        archiveLoaded = true
        librarySearch = state.librarySearch
        pinnedIndexes = state.pinned
        if let answer = state.askAnswer {
            liveAsk.finish(liveAsk.begin("What changed with the quote?"), answer: answer)
        }
        if let detail = state.review {
            review.beginLoading(id: detail.id)
            review.apply(detail)
            review.vocabulary = state.vocabulary
            if let line = state.reviewFocusLine { review.focus(line: line) }
        }
    }
}
