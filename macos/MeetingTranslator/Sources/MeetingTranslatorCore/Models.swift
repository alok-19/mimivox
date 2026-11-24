import Foundation

public struct MeetingStatusResponse: Codable, Equatable, Sendable {
    public var state: String
    public var sessionID: String?
    public var settings: MeetingSettings
    public var modelReadiness: ModelReadiness
    public var privacy: PrivacyStatus?
    public var health: RuntimeHealth
    public var performanceSummary: SessionPerformanceSummary?
    public var error: String?
    /// Nil from a server that predates first-run setup, which is always ready.
    public var setup: SetupState?

    public init(
        state: String,
        sessionID: String? = nil,
        settings: MeetingSettings = MeetingSettings(),
        modelReadiness: ModelReadiness = ModelReadiness(),
        privacy: PrivacyStatus? = nil,
        health: RuntimeHealth = RuntimeHealth(),
        performanceSummary: SessionPerformanceSummary? = nil,
        error: String? = nil,
        setup: SetupState? = nil
    ) {
        self.state = state
        self.setup = setup
        self.privacy = privacy
        self.sessionID = sessionID
        self.settings = settings
        self.modelReadiness = modelReadiness
        self.health = health
        self.performanceSummary = performanceSummary
        self.error = error
    }

    private enum CodingKeys: String, CodingKey {
        case state
        case sessionID = "session_id"
        case settings
        case modelReadiness = "model_readiness"
        case privacy
        case health
        case performanceSummary = "performance_summary"
        case error
        case setup
    }
}

/// How long transcripts are kept. Raw values match the server's `retention_policy`.
public enum RetentionPolicy: String, CaseIterable, Sendable {
    case forever
    case days30 = "30d"
    case days7 = "7d"
    case day1 = "1d"
    case none

    public var label: String {
        switch self {
        case .forever: return "Keep forever"
        case .days30: return "Delete after 30 days"
        case .days7: return "Delete after 7 days"
        case .day1: return "Delete after 1 day"
        case .none: return "Don't save transcripts"
        }
    }

    /// Short form for the privacy pill.
    public var shortLabel: String {
        switch self {
        case .forever: return "kept forever"
        case .days30: return "kept 30 days"
        case .days7: return "kept 7 days"
        case .day1: return "kept 1 day"
        case .none: return "not saved"
        }
    }

    /// Unknown values from a newer server fall back to the safe display of "forever".
    public init(serverValue: String) {
        self = RetentionPolicy(rawValue: serverValue) ?? .forever
    }

    /// Whether switching from `other` to this policy deletes meetings that are saved now.
    public func keepsLess(than other: RetentionPolicy) -> Bool {
        keptDays < other.keptDays
    }

    private var keptDays: Int {
        switch self {
        case .forever: return .max
        case .days30: return 30
        case .days7: return 7
        case .day1: return 1
        case .none: return 0
        }
    }
}

/// The server's `privacy` block on /api/status.
public struct PrivacyStatus: Codable, Equatable, Sendable {
    public var onDevice: Bool
    public var retentionPolicy: String
    public var offlineLocked: Bool
    public var networkEgressEnabled: Bool
    public var egressPaths: [String]

    public init(
        onDevice: Bool = true,
        retentionPolicy: String = "forever",
        offlineLocked: Bool = false,
        networkEgressEnabled: Bool = true,
        egressPaths: [String] = []
    ) {
        self.onDevice = onDevice
        self.retentionPolicy = retentionPolicy
        self.offlineLocked = offlineLocked
        self.networkEgressEnabled = networkEgressEnabled
        self.egressPaths = egressPaths
    }

    private enum CodingKeys: String, CodingKey {
        case onDevice = "on_device"
        case retentionPolicy = "retention_policy"
        case offlineLocked = "offline_locked"
        case networkEgressEnabled = "network_egress_enabled"
        case egressPaths = "egress_paths"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        onDevice = try container.decodeIfPresent(Bool.self, forKey: .onDevice) ?? true
        retentionPolicy = try container.decodeIfPresent(String.self, forKey: .retentionPolicy) ?? "forever"
        offlineLocked = try container.decodeIfPresent(Bool.self, forKey: .offlineLocked) ?? false
        networkEgressEnabled = try container.decodeIfPresent(Bool.self, forKey: .networkEgressEnabled) ?? true
        egressPaths = try container.decodeIfPresent([String].self, forKey: .egressPaths) ?? []
    }

    public var retention: RetentionPolicy { RetentionPolicy(serverValue: retentionPolicy) }

    /// "On-device · offline" when locked, otherwise says a model download path is open.
    public var indicatorText: String {
        offlineLocked ? "On-device · offline" : "On-device · downloads allowed"
    }
}

/// What a session hears and which language it captions in. Mirrors
/// `mimivox.languages.LanguagePair`; the server rejects anything outside this set.
public struct LanguagePair: Equatable, Hashable, Sendable, Identifiable {
    public let source: String
    public let target: String
    public let label: String

    public var id: String { "\(source)-\(target)" }

    public static let japaneseToEnglish = LanguagePair(source: "ja", target: "en", label: "日本語 → English")
    public static let supported: [LanguagePair] = [
        .japaneseToEnglish,
        LanguagePair(source: "zh", target: "en", label: "中文 → English"),
        LanguagePair(source: "ko", target: "en", label: "한국어 → English"),
        LanguagePair(source: "en", target: "ja", label: "English → 日本語"),
    ]

    /// Unknown stored codes fall back to Japanese → English, like the server.
    public static func resolve(source: String, target: String) -> LanguagePair {
        supported.first { $0.source == source && $0.target == target } ?? .japaneseToEnglish
    }

    /// Only Japanese → English uses the bundled CAT model; every other pair
    /// needs the optional on-device Qwen model downloaded in Settings → Models.
    public var needsTranslationModel: Bool { self != .japaneseToEnglish }
}

public struct MeetingSettings: Codable, Equatable, Sendable {
    public var sourceLang: String
    public var targetLang: String
    public var microphoneDevice: String
    public var systemAudioDevice: String
    public var glossaryPath: String
    public var glossaryTerms: [GlossaryTerm]
    public var glossaryProfiles: [GlossaryProfile]
    public var activeGlossaryProfile: String
    public var privacyMode: String
    public var retentionPolicy: String
    public var latencyQuality: String
    /// `off`, `manual` or `auto`: what to do when captions fall behind.
    public var catchUpMode: String
    public var cloudProviderEnabled: Bool
    public var cloudProviderConsent: Bool
    public var replayAudioPath: String
    public var replayDurationSeconds: Double
    public var modelPaths: ModelPaths
    /// Furigana, word glosses and a vocabulary list for Japanese learners.
    public var learningMode: Bool

    public var languagePair: LanguagePair {
        get { .resolve(source: sourceLang, target: targetLang) }
        set {
            sourceLang = newValue.source
            targetLang = newValue.target
        }
    }

    public init(
        sourceLang: String = "ja",
        targetLang: String = "en",
        microphoneDevice: String = "",
        systemAudioDevice: String = "",
        glossaryPath: String = "",
        glossaryTerms: [GlossaryTerm] = [],
        glossaryProfiles: [GlossaryProfile] = [],
        activeGlossaryProfile: String = "",
        privacyMode: String = "local-only",
        retentionPolicy: String = "forever",
        latencyQuality: String = "fast",
        catchUpMode: String = "manual",
        cloudProviderEnabled: Bool = false,
        cloudProviderConsent: Bool = false,
        replayAudioPath: String = "data/fixtures/meeting-recording.wav",
        replayDurationSeconds: Double = 20,
        modelPaths: ModelPaths = ModelPaths(),
        learningMode: Bool = false
    ) {
        self.sourceLang = sourceLang
        self.targetLang = targetLang
        self.microphoneDevice = microphoneDevice
        self.systemAudioDevice = systemAudioDevice
        self.glossaryPath = glossaryPath
        self.glossaryTerms = glossaryTerms
        self.glossaryProfiles = glossaryProfiles
        self.activeGlossaryProfile = activeGlossaryProfile
        self.privacyMode = privacyMode
        self.retentionPolicy = retentionPolicy
        self.latencyQuality = latencyQuality
        self.catchUpMode = catchUpMode
        self.cloudProviderEnabled = cloudProviderEnabled
        self.cloudProviderConsent = cloudProviderConsent
        self.replayAudioPath = replayAudioPath
        self.replayDurationSeconds = replayDurationSeconds
        self.modelPaths = modelPaths
        self.learningMode = learningMode
    }

    private enum CodingKeys: String, CodingKey {
        case sourceLang = "source_lang"
        case targetLang = "target_lang"
        case microphoneDevice = "microphone_device"
        case systemAudioDevice = "system_audio_device"
        case glossaryPath = "glossary_path"
        case glossaryTerms = "glossary_terms"
        case glossaryProfiles = "glossary_profiles"
        case activeGlossaryProfile = "active_glossary_profile"
        case privacyMode = "privacy_mode"
        case retentionPolicy = "retention_policy"
        case latencyQuality = "latency_quality"
        case catchUpMode = "catch_up_mode"
        case cloudProviderEnabled = "cloud_provider_enabled"
        case cloudProviderConsent = "cloud_provider_consent"
        case replayAudioPath = "replay_audio_path"
        case replayDurationSeconds = "replay_duration_seconds"
        case modelPaths = "model_paths"
        case learningMode = "learning_mode"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        sourceLang = try container.decodeIfPresent(String.self, forKey: .sourceLang) ?? "ja"
        targetLang = try container.decodeIfPresent(String.self, forKey: .targetLang) ?? "en"
        microphoneDevice = try container.decodeIfPresent(String.self, forKey: .microphoneDevice) ?? ""
        systemAudioDevice = try container.decodeIfPresent(String.self, forKey: .systemAudioDevice) ?? ""
        glossaryPath = try container.decodeIfPresent(String.self, forKey: .glossaryPath) ?? ""
        glossaryTerms = try container.decodeIfPresent([GlossaryTerm].self, forKey: .glossaryTerms) ?? []
        glossaryProfiles = try container.decodeIfPresent([GlossaryProfile].self, forKey: .glossaryProfiles) ?? []
        activeGlossaryProfile = try container.decodeIfPresent(String.self, forKey: .activeGlossaryProfile) ?? ""
        privacyMode = try container.decodeIfPresent(String.self, forKey: .privacyMode) ?? "local-only"
        retentionPolicy = try container.decodeIfPresent(String.self, forKey: .retentionPolicy) ?? "forever"
        latencyQuality = try container.decodeIfPresent(String.self, forKey: .latencyQuality) ?? "fast"
        catchUpMode = try container.decodeIfPresent(String.self, forKey: .catchUpMode) ?? "manual"
        cloudProviderEnabled = try container.decodeIfPresent(Bool.self, forKey: .cloudProviderEnabled) ?? false
        cloudProviderConsent = try container.decodeIfPresent(Bool.self, forKey: .cloudProviderConsent) ?? false
        replayAudioPath =
            try container.decodeIfPresent(String.self, forKey: .replayAudioPath)
            ?? "data/fixtures/meeting-recording.wav"
        replayDurationSeconds = try container.decodeIfPresent(Double.self, forKey: .replayDurationSeconds) ?? 20
        modelPaths = try container.decodeIfPresent(ModelPaths.self, forKey: .modelPaths) ?? ModelPaths()
        learningMode = try container.decodeIfPresent(Bool.self, forKey: .learningMode) ?? false
    }
}

public struct GlossaryTerm: Codable, Equatable, Sendable, Identifiable {
    public var source: String
    public var target: String

    public init(source: String = "", target: String = "") {
        self.source = source
        self.target = target
    }

    public var id: String {
        "\(source)->\(target)"
    }

    /// A Japanese target is an ASR spelling fix; a Latin target is a term
    /// (biased into Whisper's prompt and substituted before translation).
    public var isASRFix: Bool {
        target.unicodeScalars.contains { scalar in
            (0x3040...0x30FF).contains(scalar.value) || (0x3400...0x9FFF).contains(scalar.value)
        }
    }

    /// Parse pasted `source,target` (or tab-separated) lines; blank or
    /// incomplete lines are skipped.
    public static func parse(pasted text: String) -> [GlossaryTerm] {
        let lines = text.split(whereSeparator: \.isNewline).filter {
            !$0.trimmingCharacters(in: .whitespaces).isEmpty
        }
        return lines.compactMap { line in
            let delimiter: Character = line.contains("\t") ? "\t" : ","
            let parts = line.split(separator: delimiter, maxSplits: 1, omittingEmptySubsequences: false)
            guard parts.count == 2 else { return nil }
            let source = parts[0].trimmingCharacters(in: .whitespaces)
            let target = parts[1].trimmingCharacters(in: .whitespaces)
            return source.isEmpty || target.isEmpty ? nil : GlossaryTerm(source: source, target: target)
        }
    }
}

/// A named per-meeting glossary, persisted with settings.
public struct GlossaryProfile: Codable, Equatable, Sendable, Identifiable {
    public var name: String
    public var entries: [GlossaryTerm]

    public init(name: String = "", entries: [GlossaryTerm] = []) {
        self.name = name
        self.entries = entries
    }

    public var id: String { name }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        name = try container.decodeIfPresent(String.self, forKey: .name) ?? ""
        entries = try container.decodeIfPresent([GlossaryTerm].self, forKey: .entries) ?? []
    }

    private enum CodingKeys: String, CodingKey {
        case name, entries
    }
}

public struct ModelPaths: Codable, Equatable, Sendable {
    public var whisper: String
    public var catTranslate: String

    public init(whisper: String = "", catTranslate: String = "") {
        self.whisper = whisper
        self.catTranslate = catTranslate
    }

    private enum CodingKeys: String, CodingKey {
        case whisper
        case catTranslate = "cat_translate"
    }
}

public struct ModelReadiness: Codable, Equatable, Sendable {
    public var ready: Bool
    public var nextSteps: [String]
    public var whisper: ModelReadinessItem?
    public var asrJa: ModelReadinessItem?
    public var catTranslate: ModelReadinessItem?
    public var glossary: ModelReadinessItem?
    /// Optional on-device LLM for meeting notes / Ask; never part of `ready`.
    public var notesLlm: ModelReadinessItem?

    public init(
        ready: Bool = false,
        nextSteps: [String] = [],
        whisper: ModelReadinessItem? = nil,
        asrJa: ModelReadinessItem? = nil,
        catTranslate: ModelReadinessItem? = nil,
        glossary: ModelReadinessItem? = nil,
        notesLlm: ModelReadinessItem? = nil
    ) {
        self.ready = ready
        self.nextSteps = nextSteps
        self.whisper = whisper
        self.asrJa = asrJa
        self.catTranslate = catTranslate
        self.glossary = glossary
        self.notesLlm = notesLlm
    }

    private enum CodingKeys: String, CodingKey {
        case ready
        case nextSteps = "next_steps"
        case whisper
        case asrJa = "asr_ja"
        case catTranslate = "cat_translate"
        case glossary
        case notesLlm = "notes_llm"
    }
}

public struct ModelReadinessItem: Codable, Equatable, Sendable {
    public var ready: Bool?
    public var message: String?
    public var path: String?
    public var modelID: String?
    public var displayName: String?
    public var termCount: Int?

    public init(
        ready: Bool? = nil,
        message: String? = nil,
        path: String? = nil,
        modelID: String? = nil,
        displayName: String? = nil,
        termCount: Int? = nil
    ) {
        self.ready = ready
        self.message = message
        self.path = path
        self.modelID = modelID
        self.displayName = displayName
        self.termCount = termCount
    }

    private enum CodingKeys: String, CodingKey {
        case ready
        case message
        case path
        case modelID = "model_id"
        case displayName = "display_name"
        case termCount = "term_count"
    }
}

public struct RuntimeHealth: Codable, Equatable, Sendable {
    public var audioInputActive: Bool
    public var asrWorkerActive: Bool
    public var translationWorkerActive: Bool
    public var queueLagSeconds: Double
    public var realtimeLagSeconds: Double
    public var droppedStaleChunks: Int
    public var droppedSilenceChunks: Int
    public var activeSpeech: Bool
    public var modelReady: Bool
    public var degraded: Bool
    public var sessionLabel: String?
    public var audioLabel: String?
    public var asrLabel: String?
    public var translationLabel: String?
    public var catchingUp: Bool
    public var stopped: Bool
    public var latency: LatencyHealth

    public init(
        audioInputActive: Bool = false,
        asrWorkerActive: Bool = false,
        translationWorkerActive: Bool = false,
        queueLagSeconds: Double = 0,
        realtimeLagSeconds: Double = 0,
        droppedStaleChunks: Int = 0,
        droppedSilenceChunks: Int = 0,
        activeSpeech: Bool = false,
        modelReady: Bool = false,
        degraded: Bool = false,
        sessionLabel: String? = nil,
        audioLabel: String? = nil,
        asrLabel: String? = nil,
        translationLabel: String? = nil,
        catchingUp: Bool = false,
        stopped: Bool = false,
        latency: LatencyHealth = LatencyHealth()
    ) {
        self.audioInputActive = audioInputActive
        self.asrWorkerActive = asrWorkerActive
        self.translationWorkerActive = translationWorkerActive
        self.queueLagSeconds = queueLagSeconds
        self.realtimeLagSeconds = realtimeLagSeconds
        self.droppedStaleChunks = droppedStaleChunks
        self.droppedSilenceChunks = droppedSilenceChunks
        self.activeSpeech = activeSpeech
        self.modelReady = modelReady
        self.degraded = degraded
        self.sessionLabel = sessionLabel
        self.audioLabel = audioLabel
        self.asrLabel = asrLabel
        self.translationLabel = translationLabel
        self.catchingUp = catchingUp
        self.stopped = stopped
        self.latency = latency
    }

    private enum CodingKeys: String, CodingKey {
        case audioInputActive = "audio_input_active"
        case asrWorkerActive = "asr_worker_active"
        case translationWorkerActive = "translation_worker_active"
        case queueLagSeconds = "queue_lag_s"
        case realtimeLagSeconds = "realtime_lag_s"
        case droppedStaleChunks = "dropped_stale_chunks"
        case droppedSilenceChunks = "dropped_silence_chunks"
        case activeSpeech = "active_speech"
        case modelReady = "model_ready"
        case degraded
        case sessionLabel = "session_label"
        case audioLabel = "audio_label"
        case asrLabel = "asr_label"
        case translationLabel = "translation_label"
        case catchingUp = "catching_up"
        case stopped
        case latency
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        audioInputActive = try container.decodeIfPresent(Bool.self, forKey: .audioInputActive) ?? false
        asrWorkerActive = try container.decodeIfPresent(Bool.self, forKey: .asrWorkerActive) ?? false
        translationWorkerActive = try container.decodeIfPresent(Bool.self, forKey: .translationWorkerActive) ?? false
        queueLagSeconds = try container.decodeIfPresent(Double.self, forKey: .queueLagSeconds) ?? 0
        realtimeLagSeconds = try container.decodeIfPresent(Double.self, forKey: .realtimeLagSeconds) ?? 0
        droppedStaleChunks = try container.decodeIfPresent(Int.self, forKey: .droppedStaleChunks) ?? 0
        droppedSilenceChunks = try container.decodeIfPresent(Int.self, forKey: .droppedSilenceChunks) ?? 0
        activeSpeech = try container.decodeIfPresent(Bool.self, forKey: .activeSpeech) ?? false
        modelReady = try container.decodeIfPresent(Bool.self, forKey: .modelReady) ?? false
        degraded = try container.decodeIfPresent(Bool.self, forKey: .degraded) ?? false
        sessionLabel = try container.decodeIfPresent(String.self, forKey: .sessionLabel)
        audioLabel = try container.decodeIfPresent(String.self, forKey: .audioLabel)
        asrLabel = try container.decodeIfPresent(String.self, forKey: .asrLabel)
        translationLabel = try container.decodeIfPresent(String.self, forKey: .translationLabel)
        catchingUp = try container.decodeIfPresent(Bool.self, forKey: .catchingUp) ?? false
        stopped = try container.decodeIfPresent(Bool.self, forKey: .stopped) ?? false
        latency = (try? container.decodeIfPresent(LatencyHealth.self, forKey: .latency)) ?? LatencyHealth()
    }
}

public struct SessionPerformanceSummary: Codable, Equatable, Sendable {
    public var asrCount: Int
    public var translationCount: Int
    public var emptyAsrCount: Int
    public var staleDrops: Int
    public var lateTranslations: Int
    public var displayLatencySeconds: LatencyPercentileSummary
    public var asrBackend: String

    public init(
        asrCount: Int = 0,
        translationCount: Int = 0,
        emptyAsrCount: Int = 0,
        staleDrops: Int = 0,
        lateTranslations: Int = 0,
        displayLatencySeconds: LatencyPercentileSummary = LatencyPercentileSummary(),
        asrBackend: String = "unknown"
    ) {
        self.asrCount = asrCount
        self.translationCount = translationCount
        self.emptyAsrCount = emptyAsrCount
        self.staleDrops = staleDrops
        self.lateTranslations = lateTranslations
        self.displayLatencySeconds = displayLatencySeconds
        self.asrBackend = asrBackend
    }

    private enum CodingKeys: String, CodingKey {
        case asrCount = "asr_count"
        case translationCount = "translation_count"
        case emptyAsrCount = "empty_asr_count"
        case staleDrops = "stale_drops"
        case lateTranslations = "late_translations"
        case displayLatencySeconds = "display_latency_s"
        case asrBackend = "asr_backend"
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        asrCount = try container.decodeIfPresent(Int.self, forKey: .asrCount) ?? 0
        translationCount = try container.decodeIfPresent(Int.self, forKey: .translationCount) ?? 0
        emptyAsrCount = try container.decodeIfPresent(Int.self, forKey: .emptyAsrCount) ?? 0
        staleDrops = try container.decodeIfPresent(Int.self, forKey: .staleDrops) ?? 0
        lateTranslations = try container.decodeIfPresent(Int.self, forKey: .lateTranslations) ?? 0
        displayLatencySeconds =
            try container.decodeIfPresent(LatencyPercentileSummary.self, forKey: .displayLatencySeconds)
            ?? LatencyPercentileSummary()
        asrBackend = try container.decodeIfPresent(String.self, forKey: .asrBackend) ?? "unknown"
    }
}

public struct LatencyPercentileSummary: Codable, Equatable, Sendable {
    public var p50: Double?
    public var p90: Double?

    public init(p50: Double? = nil, p90: Double? = nil) {
        self.p50 = p50
        self.p90 = p90
    }
}

public struct AudioDevice: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var name: String

    public init(id: String, name: String) {
        self.id = id
        self.name = name
    }
}

public struct ArchivedSessionSummary: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var startedAt: String
    public var label: String
    public var eventCount: Int
    public var durationSeconds: Double?
    public var headline: String?

    public init(
        id: String,
        startedAt: String = "",
        label: String = "",
        eventCount: Int = 0,
        durationSeconds: Double? = nil,
        headline: String? = nil
    ) {
        self.id = id
        self.startedAt = startedAt
        self.label = label
        self.eventCount = eventCount
        self.durationSeconds = durationSeconds
        self.headline = headline
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case startedAt = "started_at"
        case label
        case eventCount = "event_count"
        case durationSeconds = "duration_seconds"
        case headline
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        id = try container.decode(String.self, forKey: .id)
        startedAt = try container.decodeIfPresent(String.self, forKey: .startedAt) ?? ""
        label = try container.decodeIfPresent(String.self, forKey: .label) ?? ""
        eventCount = try container.decodeIfPresent(Int.self, forKey: .eventCount) ?? 0
        durationSeconds = try container.decodeIfPresent(Double.self, forKey: .durationSeconds)
        let text = try container.decodeIfPresent(String.self, forKey: .headline)
        headline = text?.isEmpty == false ? text : nil
    }
}

public struct ArchivedSessionDetail: Codable, Equatable, Sendable {
    public var id: String
    public var startedAt: String
    public var label: String
    public var eventCount: Int
    public var events: [MeetingEvent]
    public var settings: MeetingSettings?
    public var performanceSummary: SessionPerformanceSummary?
    /// Indexes of the lines the viewer pinned in the HUD (absent in older archives).
    public var pinnedIDs: [Int]
    /// Cached meeting notes, present once generated for this session.
    public var notes: MeetingNotes?
    public var tags: [String]

    public init(
        id: String,
        startedAt: String = "",
        label: String = "",
        eventCount: Int = 0,
        events: [MeetingEvent] = [],
        settings: MeetingSettings? = nil,
        performanceSummary: SessionPerformanceSummary? = nil,
        notes: MeetingNotes? = nil,
        tags: [String] = [],
        pinnedIDs: [Int] = []
    ) {
        self.tags = tags
        self.pinnedIDs = pinnedIDs
        self.id = id
        self.startedAt = startedAt
        self.label = label
        self.eventCount = eventCount
        self.events = events
        self.settings = settings
        self.performanceSummary = performanceSummary
        self.notes = notes
    }

    private enum CodingKeys: String, CodingKey {
        case id
        case startedAt = "started_at"
        case label
        case eventCount = "event_count"
        case events
        case settings
        case performanceSummary = "performance_summary"
        case pinnedIDs = "pinned_ids"
        case notes
        case tags
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        pinnedIDs = try container.decodeIfPresent([Int].self, forKey: .pinnedIDs) ?? []
        tags = try container.decodeIfPresent([String].self, forKey: .tags) ?? []
        id = try container.decode(String.self, forKey: .id)
        startedAt = try container.decodeIfPresent(String.self, forKey: .startedAt) ?? ""
        label = try container.decodeIfPresent(String.self, forKey: .label) ?? ""
        eventCount = try container.decodeIfPresent(Int.self, forKey: .eventCount) ?? 0
        events = try container.decodeIfPresent([MeetingEvent].self, forKey: .events) ?? []
        settings = try container.decodeIfPresent(MeetingSettings.self, forKey: .settings)
        performanceSummary = try container.decodeIfPresent(SessionPerformanceSummary.self, forKey: .performanceSummary)
        notes = try container.decodeIfPresent(MeetingNotes.self, forKey: .notes)
    }
}

public struct ArchivedSessionsResponse: Codable, Equatable, Sendable {
    public var sessions: [ArchivedSessionSummary]

    public init(sessions: [ArchivedSessionSummary] = []) {
        self.sessions = sessions
    }
}

/// One run of a search snippet; `hit` segments are the matched text.
public struct SnippetSegment: Codable, Equatable, Sendable {
    public var text: String
    public var hit: Bool

    public init(text: String, hit: Bool = false) {
        self.text = text
        self.hit = hit
    }

    private enum CodingKeys: String, CodingKey {
        case text = "t"
        case hit
    }
}

public struct LibraryLineHit: Codable, Equatable, Identifiable, Sendable {
    public var lineID: Int
    public var start: Double
    public var speaker: String?
    public var ja: String
    public var en: String
    public var jaSnippet: [SnippetSegment]
    public var enSnippet: [SnippetSegment]

    public var id: Int { lineID }

    private enum CodingKeys: String, CodingKey {
        case lineID = "line_id"
        case start, speaker, ja, en
        case jaSnippet = "ja_snippet"
        case enSnippet = "en_snippet"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        lineID = try c.decode(Int.self, forKey: .lineID)
        start = try c.decodeIfPresent(Double.self, forKey: .start) ?? 0
        speaker = try c.decodeIfPresent(String.self, forKey: .speaker)
        ja = try c.decodeIfPresent(String.self, forKey: .ja) ?? ""
        en = try c.decodeIfPresent(String.self, forKey: .en) ?? ""
        jaSnippet = try c.decodeIfPresent([SnippetSegment].self, forKey: .jaSnippet) ?? []
        enSnippet = try c.decodeIfPresent([SnippetSegment].self, forKey: .enSnippet) ?? []
    }
}

public struct LibrarySessionHit: Codable, Equatable, Identifiable, Sendable {
    public var id: String
    public var label: String
    public var startedAt: String
    public var lineCount: Int
    public var tags: [String]
    public var matchCount: Int
    public var matchedIn: [String]
    public var lines: [LibraryLineHit]

    private enum CodingKeys: String, CodingKey {
        case id, label, tags, lines
        case startedAt = "started_at"
        case lineCount = "line_count"
        case matchCount = "match_count"
        case matchedIn = "matched_in"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        id = try c.decode(String.self, forKey: .id)
        label = try c.decodeIfPresent(String.self, forKey: .label) ?? ""
        startedAt = try c.decodeIfPresent(String.self, forKey: .startedAt) ?? ""
        lineCount = try c.decodeIfPresent(Int.self, forKey: .lineCount) ?? 0
        tags = try c.decodeIfPresent([String].self, forKey: .tags) ?? []
        matchCount = try c.decodeIfPresent(Int.self, forKey: .matchCount) ?? 0
        matchedIn = try c.decodeIfPresent([String].self, forKey: .matchedIn) ?? []
        lines = try c.decodeIfPresent([LibraryLineHit].self, forKey: .lines) ?? []
    }
}

public struct LibrarySearchResponse: Codable, Equatable, Sendable {
    public var query: String
    public var totalSessions: Int
    public var results: [LibrarySessionHit]
    public var speakers: [String]
    public var tags: [String]

    public init(
        query: String = "", totalSessions: Int = 0, results: [LibrarySessionHit] = [], speakers: [String] = [],
        tags: [String] = []
    ) {
        self.query = query
        self.totalSessions = totalSessions
        self.results = results
        self.speakers = speakers
        self.tags = tags
    }

    private enum CodingKeys: String, CodingKey {
        case query, results, speakers, tags
        case totalSessions = "total_sessions"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        query = try c.decodeIfPresent(String.self, forKey: .query) ?? ""
        totalSessions = try c.decodeIfPresent(Int.self, forKey: .totalSessions) ?? 0
        results = try c.decodeIfPresent([LibrarySessionHit].self, forKey: .results) ?? []
        speakers = try c.decodeIfPresent([String].self, forKey: .speakers) ?? []
        tags = try c.decodeIfPresent([String].self, forKey: .tags) ?? []
    }
}

public struct SessionTagsResponse: Codable, Equatable, Sendable {
    public var sessionID: String
    public var tags: [String]

    private enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case tags
    }
}

public struct SessionTitleResponse: Codable, Equatable, Sendable {
    public var sessionID: String
    public var headline: String?

    private enum CodingKeys: String, CodingKey {
        case sessionID = "session_id"
        case headline
    }
}

public struct SessionTagsRequest: Codable, Equatable, Sendable {
    public var add: [String]
    public var remove: [String]

    public init(add: [String] = [], remove: [String] = []) {
        self.add = add
        self.remove = remove
    }
}

public struct DevicesResponse: Codable, Equatable, Sendable {
    public var inputDevices: [AudioDevice]

    public init(inputDevices: [AudioDevice]) {
        self.inputDevices = inputDevices
    }

    private enum CodingKeys: String, CodingKey {
        case inputDevices = "input_devices"
    }
}

/// Who said a caption line, as labelled by the on-device speaker tracker.
public struct SpeakerTag: Codable, Equatable, Hashable, Sendable {
    public var id: String
    public var label: String

    public init(id: String, label: String) {
        self.id = id
        self.label = label
    }

    /// 1-based number parsed from ids like "speaker-3"; used for a stable colour.
    public var number: Int {
        Int(id.filter(\.isNumber)) ?? 1
    }
}

/// How far to trust one caption, as scored on-device from the ASR and
/// translation token probabilities and the pipeline's repair heuristics.
public struct CaptionConfidence: Codable, Equatable, Hashable, Sendable {
    public enum Level: String, Codable, Sendable {
        case high, medium, low
    }

    public var level: Level
    public var reasons: [String]
    public var asr: Double?
    public var mt: Double?

    public init(level: Level, reasons: [String] = [], asr: Double? = nil, mt: Double? = nil) {
        self.level = level
        self.reasons = reasons
        self.asr = asr
        self.mt = mt
    }

    /// Only medium and low are worth a marker; high is the unremarkable default.
    public var isFlagged: Bool { level != .high }

    /// Tooltip / accessibility text, e.g. "Low confidence: Speech recognition was unsure".
    public var help: String {
        let head = "\(level.rawValue.capitalized) confidence"
        return reasons.isEmpty ? head : "\(head): " + reasons.joined(separator: "; ")
    }

    /// An unknown level from a newer server decodes as high rather than failing the event.
    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        level = Level(rawValue: try container.decode(String.self, forKey: .level)) ?? .high
        reasons = try container.decodeIfPresent([String].self, forKey: .reasons) ?? []
        asr = try container.decodeIfPresent(Double.self, forKey: .asr)
        mt = try container.decodeIfPresent(Double.self, forKey: .mt)
    }

    private enum CodingKeys: String, CodingKey {
        case level, reasons, asr, mt
    }
}

public struct MeetingEvent: Codable, Equatable, Identifiable, Sendable {
    public var type: String
    public var index: Int?
    public var start: Double?
    public var end: Double?
    public var text: String?
    public var source: String?
    public var message: String?
    public var level: Double?
    public var timestampSeconds: Double?
    public var sourceIndexes: [Int]?
    public var reason: String?
    public var skippedIndex: Int?
    public var displayLatencySeconds: Double?
    public var isLate: Bool
    public var speaker: SpeakerTag?
    /// Revised JA for a `TranscriptRevisionEvent` (the merged sentence).
    public var japanese: String?
    /// Rows folded into `index` by a `TranscriptRevisionEvent`.
    public var mergedIndexes: [Int]?
    /// Confidence of a final or revised caption; nil from servers that do not send it.
    public var confidence: CaptionConfidence?
    /// Why a caption has no translation: set to `catch_up` on an ASR event whose EN was skipped.
    public var translationSkipped: String?
    /// Set on an ASR event whose line finished with no translation, so no translation is still coming.
    public var translationEmpty: Bool?
    /// For a `CaptionGapEvent`: whether the skipped audio was archived so Review can recover it.
    public var recoverable: Bool?
    /// Carried by a `LatencyHealthEvent` only.
    public var health: LatencyHealth?
    /// Word breakdown of a JA caption (learning mode only); nil otherwise.
    public var tokens: [LearningToken]?
    /// Carried by a `SetupStateEvent` only.
    public var setup: SetupState?
    /// Set locally by `LiveTranscript` on a row whose text was rewritten; never decoded.
    public var isRevised: Bool = false

    public var id: String {
        [
            type,
            index.map(String.init) ?? "none",
            start.map { String(format: "%.3f", $0) } ?? "none",
            text ?? message ?? source ?? "",
        ].joined(separator: "-")
    }

    public init(
        type: String,
        index: Int? = nil,
        start: Double? = nil,
        end: Double? = nil,
        text: String? = nil,
        source: String? = nil,
        message: String? = nil,
        level: Double? = nil,
        timestampSeconds: Double? = nil,
        sourceIndexes: [Int]? = nil,
        reason: String? = nil,
        skippedIndex: Int? = nil,
        displayLatencySeconds: Double? = nil,
        isLate: Bool = false,
        speaker: SpeakerTag? = nil,
        japanese: String? = nil,
        mergedIndexes: [Int]? = nil,
        confidence: CaptionConfidence? = nil,
        translationSkipped: String? = nil,
        translationEmpty: Bool? = nil,
        recoverable: Bool? = nil,
        health: LatencyHealth? = nil,
        tokens: [LearningToken]? = nil,
        setup: SetupState? = nil
    ) {
        self.type = type
        self.setup = setup
        self.index = index
        self.start = start
        self.end = end
        self.text = text
        self.source = source
        self.message = message
        self.level = level
        self.timestampSeconds = timestampSeconds
        self.sourceIndexes = sourceIndexes
        self.reason = reason
        self.skippedIndex = skippedIndex
        self.displayLatencySeconds = displayLatencySeconds
        self.isLate = isLate
        self.speaker = speaker
        self.japanese = japanese
        self.mergedIndexes = mergedIndexes
        self.confidence = confidence
        self.translationSkipped = translationSkipped
        self.translationEmpty = translationEmpty
        self.recoverable = recoverable
        self.health = health
        self.tokens = tokens
    }

    private enum CodingKeys: String, CodingKey {
        case type
        case index
        case start
        case end
        case text
        case source
        case message
        case level
        case timestampSeconds = "timestamp_s"
        case sourceIndexes = "source_indexes"
        case reason
        case skippedIndex = "skipped_index"
        case displayLatencySeconds = "display_latency_s"
        case isLate = "is_late"
        case speaker
        case japanese = "ja"
        case mergedIndexes = "merged_indexes"
        case confidence
        case translationSkipped = "translation_skipped"
        case translationEmpty = "translation_empty"
        case recoverable
        case health
        case tokens
        case setup
    }

    public init(from decoder: Decoder) throws {
        let container = try decoder.container(keyedBy: CodingKeys.self)
        type = try container.decode(String.self, forKey: .type)
        index = try container.decodeIfPresent(Int.self, forKey: .index)
        start = try container.decodeIfPresent(Double.self, forKey: .start)
        end = try container.decodeIfPresent(Double.self, forKey: .end)
        text = try container.decodeIfPresent(String.self, forKey: .text)
        source = try container.decodeIfPresent(String.self, forKey: .source)
        message = try container.decodeIfPresent(String.self, forKey: .message)
        level = try container.decodeIfPresent(Double.self, forKey: .level)
        timestampSeconds = try container.decodeIfPresent(Double.self, forKey: .timestampSeconds)
        sourceIndexes = try container.decodeIfPresent([Int].self, forKey: .sourceIndexes)
        reason = try container.decodeIfPresent(String.self, forKey: .reason)
        skippedIndex = try container.decodeIfPresent(Int.self, forKey: .skippedIndex)
        displayLatencySeconds = try container.decodeIfPresent(Double.self, forKey: .displayLatencySeconds)
        isLate = try container.decodeIfPresent(Bool.self, forKey: .isLate) ?? false
        speaker = try container.decodeIfPresent(SpeakerTag.self, forKey: .speaker)
        japanese = try container.decodeIfPresent(String.self, forKey: .japanese)
        mergedIndexes = try container.decodeIfPresent([Int].self, forKey: .mergedIndexes)
        confidence = try? container.decodeIfPresent(CaptionConfidence.self, forKey: .confidence)
        translationSkipped = try? container.decodeIfPresent(String.self, forKey: .translationSkipped)
        translationEmpty = try? container.decodeIfPresent(Bool.self, forKey: .translationEmpty)
        recoverable = try? container.decodeIfPresent(Bool.self, forKey: .recoverable)
        health = try? container.decodeIfPresent(LatencyHealth.self, forKey: .health)
        tokens = try? container.decodeIfPresent([LearningToken].self, forKey: .tokens)
        setup = try? container.decodeIfPresent(SetupState.self, forKey: .setup)
    }
}

/// One word of a JA caption in learning mode. `reading` (hiragana) exists only
/// for kanji; `base` and `gloss` only for content words the lexicon knows.
public struct LearningToken: Codable, Equatable, Sendable {
    public var surface: String
    public var reading: String?
    public var base: String?
    public var gloss: String?

    public init(surface: String, reading: String? = nil, base: String? = nil, gloss: String? = nil) {
        self.surface = surface
        self.reading = reading
        self.base = base
        self.gloss = gloss
    }

    private enum CodingKeys: String, CodingKey { case surface, reading, base, gloss }

    /// Furigana over this word: kanji only, and only when the reading adds something.
    public var rubyText: String? {
        guard let reading, !reading.isEmpty, reading != surface else { return nil }
        return reading
    }
}

extension Array where Element == LearningToken {
    /// Tokens only if they spell the caption exactly; a revised or stale
    /// breakdown must never show furigana over text it does not match.
    public func matching(_ text: String) -> [LearningToken]? {
        isEmpty || map(\.surface).joined() != text ? nil : self
    }
}

extension Array where Element == MeetingEvent {
    public var bilingualTranscriptRows: [BilingualTranscriptRow] {
        LiveTranscript(events: self).rows
    }
}

public struct BilingualTranscriptRow: Equatable, Identifiable, Sendable {
    public var index: Int
    public var japanese: MeetingEvent?
    public var english: MeetingEvent?
    /// Set for a row that stands for speech a catch-up skipped.
    public var gap: MeetingEvent?

    /// Stable while the row's translation lands or is revised.
    public var id: String {
        gap.map { "gap-\($0.id)" } ?? Self.lineID(index)
    }

    public static func lineID(_ index: Int) -> String {
        "line-\(index)"
    }

    public var start: Double {
        japanese?.start ?? english?.start ?? gap?.start ?? 0
    }

    public var end: Double? {
        [japanese?.end, english?.end, gap?.end].compactMap { $0 }.max()
    }

    /// Why this row lacks (some of) its text because a catch-up skipped it, else nil.
    public var skipNote: String? {
        if let gap {
            return gap.message ?? "Speech skipped while catching up"
        }
        if english == nil, japanese?.translationSkipped != nil {
            return "Translation skipped while catching up"
        }
        return nil
    }

    /// True once the line finished and the translator produced nothing for it.
    public var hasNoTranslation: Bool {
        english == nil && japanese?.translationEmpty == true
    }

    public var speaker: SpeakerTag? {
        japanese?.speaker ?? english?.speaker
    }

    /// True when a later phrase continued this sentence and its text was rewritten.
    public var isRevised: Bool {
        english?.isRevised == true || japanese?.isRevised == true
    }

    /// The caption's confidence when it is worth flagging (medium or low).
    public var flaggedConfidence: CaptionConfidence? {
        [english?.confidence, japanese?.confidence].compactMap { $0 }.first(where: \.isFlagged)
    }

    public init(index: Int, japanese: MeetingEvent?, english: MeetingEvent?, gap: MeetingEvent? = nil) {
        self.index = index
        self.japanese = japanese
        self.english = english
        self.gap = gap
    }
}

/// A word collected from a session ("Words from this meeting").
public struct VocabularyWord: Codable, Equatable, Sendable, Identifiable {
    public var word: String
    public var reading: String
    public var gloss: String
    public var count: Int
    public var lineID: Int?
    public var exampleJapanese: String?
    public var exampleEnglish: String?
    public var id: String { word }

    private enum CodingKeys: String, CodingKey {
        case word, reading, gloss, count
        case lineID = "line_id"
        case exampleJapanese = "example_ja"
        case exampleEnglish = "example_en"
    }
}

public struct VocabularyResponse: Codable, Equatable, Sendable {
    public var words: [VocabularyWord]
    public var count: Int
}

public struct VocabularyExportResponse: Codable, Equatable, Sendable {
    public var message: String
    public var path: String?

    public init(message: String, path: String? = nil) {
        self.message = message
        self.path = path
    }
}

/// `/api/dictate`: what was said, and in which language ("ja" or "en").
public struct DictationResponse: Codable, Equatable, Sendable {
    public var text: String
    public var language: String

    public init(text: String, language: String) {
        self.text = text
        self.language = language
    }
}
