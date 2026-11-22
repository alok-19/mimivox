import Foundation

/// Whether captions are keeping up with the speaker (server `HealthState`).
public enum LatencyState: String, Codable, Equatable, Sendable {
    case ok
    case behind
    case overloaded
}

/// One cause behind a non-ok state, e.g. `notes_gpu`, `thermal`, `asr_slow`, `translate_slow`.
public struct LatencyReason: Codable, Equatable, Sendable {
    public var code: String
    public var message: String

    public init(code: String, message: String) {
        self.code = code
        self.message = message
    }
}

/// The server's health snapshot (`health.latency` in `/api/status`, and each
/// `LatencyHealthEvent` on SSE). Decoding is forgiving: a server without the
/// field, or with an unknown state, reads as `ok`.
public struct LatencyHealth: Codable, Equatable, Sendable {
    public var state: LatencyState
    /// Seconds of speech captured but not yet on screen.
    public var backlogSeconds: Double
    public var reasons: [LatencyReason]
    public var catchingUp: Bool
    public var catchUpAvailable: Bool
    /// "fast" (skip translation of queued lines) or "skip" (also drop audio ASR cannot keep up with).
    public var recommendedMode: String
    public var skippedTranslations: Int
    public var gaps: Int
    public var gapAudioSeconds: Double

    public init(
        state: LatencyState = .ok,
        backlogSeconds: Double = 0,
        reasons: [LatencyReason] = [],
        catchingUp: Bool = false,
        catchUpAvailable: Bool = false,
        recommendedMode: String = "fast",
        skippedTranslations: Int = 0,
        gaps: Int = 0,
        gapAudioSeconds: Double = 0
    ) {
        self.state = state
        self.backlogSeconds = backlogSeconds
        self.reasons = reasons
        self.catchingUp = catchingUp
        self.catchUpAvailable = catchUpAvailable
        self.recommendedMode = recommendedMode
        self.skippedTranslations = skippedTranslations
        self.gaps = gaps
        self.gapAudioSeconds = gapAudioSeconds
    }

    private enum CodingKeys: String, CodingKey {
        case state
        case backlogSeconds = "backlog_s"
        case reasons
        case catchingUp = "catching_up"
        case catchUpAvailable = "catch_up_available"
        case recommendedMode = "recommended_mode"
        case skippedTranslations = "skipped_translations"
        case gaps
        case gapAudioSeconds = "gap_audio_s"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        let raw = try c.decodeIfPresent(String.self, forKey: .state) ?? "ok"
        state = LatencyState(rawValue: raw) ?? .ok
        backlogSeconds = try c.decodeIfPresent(Double.self, forKey: .backlogSeconds) ?? 0
        reasons = try c.decodeIfPresent([LatencyReason].self, forKey: .reasons) ?? []
        catchingUp = try c.decodeIfPresent(Bool.self, forKey: .catchingUp) ?? false
        catchUpAvailable = try c.decodeIfPresent(Bool.self, forKey: .catchUpAvailable) ?? false
        recommendedMode = try c.decodeIfPresent(String.self, forKey: .recommendedMode) ?? "fast"
        skippedTranslations = try c.decodeIfPresent(Int.self, forKey: .skippedTranslations) ?? 0
        gaps = try c.decodeIfPresent(Int.self, forKey: .gaps) ?? 0
        gapAudioSeconds = try c.decodeIfPresent(Double.self, forKey: .gapAudioSeconds) ?? 0
    }

    public var isOK: Bool { state == .ok }

    /// The main cause in words, or nil when healthy.
    /// The lag is an Ask or Notes request the user started; it hands the GPU back every few tokens.
    public var isNotesPause: Bool { reasons.first?.code == "notes_gpu" }

    public var reasonText: String? {
        guard !isOK else { return nil }
        return reasons.first?.message ?? String(format: "Captions are %.0fs behind the speaker", backlogSeconds)
    }

    /// Everything for a tooltip: causes, and what catching up will do.
    public var detailText: String {
        guard !isOK || catchingUp else { return "Captions are keeping up." }
        var parts = reasons.map(\.message)
        if parts.isEmpty { parts.append(String(format: "Captions are %.0fs behind the speaker.", backlogSeconds)) }
        if catchUpAvailable {
            parts.append(
                recommendedMode == "skip"
                    ? "Catch up skips ahead; skipped speech stays in the archive so Review can recover it."
                    : "Catch up skips translating queued lines; they show Japanese only and Review can translate them later."
            )
        }
        return parts.joined(separator: " ")
    }
}

/// What to do when captions fall behind (`catch_up_mode` setting).
public enum CatchUpMode: String, CaseIterable, Identifiable, Sendable {
    case manual
    case auto
    case off

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .manual: "Ask me (Catch up button)"
        case .auto: "Automatically when overloaded"
        case .off: "Never skip anything"
        }
    }
}

/// Result of `POST /api/session/catch-up`.
public struct CatchUpResponse: Codable, Equatable, Sendable {
    public var message: String
    public var mode: String?
    public var translationsSkipped: Int
    public var gaps: Int

    private enum CodingKeys: String, CodingKey {
        case message
        case mode
        case translationsSkipped = "translations_skipped"
        case gaps
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        message = try c.decodeIfPresent(String.self, forKey: .message) ?? ""
        mode = try c.decodeIfPresent(String.self, forKey: .mode)
        translationsSkipped = try c.decodeIfPresent(Int.self, forKey: .translationsSkipped) ?? 0
        gaps = try c.decodeIfPresent(Int.self, forKey: .gaps) ?? 0
    }
}

/// Result of `POST /api/sessions/{id}/recover`.
public struct RecoverResponse: Codable, Equatable, Sendable {
    public var recoveredGaps: Int
    public var retranslated: Int
    public var remaining: Int

    private enum CodingKeys: String, CodingKey {
        case recoveredGaps = "recovered_gaps"
        case retranslated
        case remaining
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        recoveredGaps = try c.decodeIfPresent(Int.self, forKey: .recoveredGaps) ?? 0
        retranslated = try c.decodeIfPresent(Int.self, forKey: .retranslated) ?? 0
        remaining = try c.decodeIfPresent(Int.self, forKey: .remaining) ?? 0
    }

    public init(recoveredGaps: Int, retranslated: Int, remaining: Int) {
        self.recoveredGaps = recoveredGaps
        self.retranslated = retranslated
        self.remaining = remaining
    }

    public static let none = RecoverResponse(recoveredGaps: 0, retranslated: 0, remaining: 0)

    public var summary: String {
        if recoveredGaps + retranslated == 0 {
            return remaining == 0
                ? "Nothing to recover" : "Could not recover \(remaining) skipped \(remaining == 1 ? "item" : "items")"
        }
        return
            "Recovered \(recoveredGaps) skipped \(recoveredGaps == 1 ? "span" : "spans") and translated \(retranslated) \(retranslated == 1 ? "line" : "lines")"
            + (remaining > 0 ? "; \(remaining) could not be recovered" : "")
    }
}
