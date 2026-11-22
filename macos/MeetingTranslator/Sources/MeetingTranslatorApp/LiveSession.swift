import Foundation
import MeetingTranslatorCore
import Observation

@MainActor
@Observable
final class LiveSession {
    private(set) var transcript = LiveTranscript()
    private(set) var interimJapanese: String?
    private(set) var audioLevel: Double = 0
    private(set) var latency: LatencyHealth?
    private(set) var staleCaptionDropped = false
    private(set) var translationActive = false
    private(set) var epoch: UInt64 = 0
    private(set) var startedAt: Date?
    private(set) var endedAt: Date?

    /// Loudest level since the session began; read by the silent-capture check, so views need not track it.
    @ObservationIgnored private(set) var peakLevel: Double = 0
    @ObservationIgnored private var acceptsEvents = false
    @ObservationIgnored private var seenEventIDs: Set<String> = []
    @ObservationIgnored private var translationHold: Task<Void, Never>?

    private static let translationMinimumVisible = Duration.milliseconds(800)

    var rows: [BilingualTranscriptRow] { transcript.rows }

    var transcriptDuration: TimeInterval? {
        rows.compactMap(\.end).max()
    }

    @discardableResult
    func ingest(_ event: MeetingEvent, at date: Date = Date()) -> Bool {
        if event.type == "SessionStartedEvent" {
            clear()
            acceptsEvents = true
            startedAt = date
        }
        guard acceptsEvents else { return false }
        switch event.type {
        case "AudioLevelEvent":
            audioLevel = event.level ?? 0
            peakLevel = max(peakLevel, audioLevel)
            return false
        case "LatencyHealthEvent":
            latency = event.health
            return false
        case "SpeakerRenamedEvent":
            guard let tag = event.speaker else { return false }
            transcript.renameSpeaker(tag)
            return true
        default:
            break
        }
        guard seenEventIDs.insert(event.id).inserted else { return false }
        switch event.type {
        case "AsrPartialEvent":
            setInterim(event.text)
        case "TranslationStartedEvent":
            pulseTranslation()
            setInterim(event.text)
        case "AsrCompletedEvent":
            interimJapanese = nil
        case "TranslationCompletedEvent":
            pulseTranslation()
        case "BackpressureStateEvent" where event.reason == "stale_caption_suppressed":
            staleCaptionDropped = true
        case "SessionEndedEvent", "ErrorEvent":
            if event.type == "SessionEndedEvent" {
                latency = nil
                endedAt = date
            }
            clearPipelineFlags()
        default:
            break
        }
        guard LiveTranscript.changesRows(event.type) else { return false }
        transcript.apply(event)
        return true
    }

    func beginNewSession() {
        epoch &+= 1
        clear()
    }

    func clear() {
        acceptsEvents = false
        seenEventIDs = []
        transcript = LiveTranscript()
        interimJapanese = nil
        audioLevel = 0
        peakLevel = 0
        latency = nil
        startedAt = nil
        endedAt = nil
        staleCaptionDropped = false
        clearPipelineFlags()
    }

    func renameSpeaker(_ tag: SpeakerTag) {
        transcript.renameSpeaker(tag)
    }

    func noteTranslationWorker(active: Bool) {
        if active { pulseTranslation() }
    }

    func clearPipelineFlags() {
        translationHold?.cancel()
        translationHold = nil
        translationActive = false
    }

    private func pulseTranslation() {
        translationHold?.cancel()
        translationActive = true
        translationHold = Task { @MainActor [weak self] in
            try? await Task.sleep(for: Self.translationMinimumVisible)
            guard !Task.isCancelled else { return }
            self?.translationActive = false
        }
    }

    private func setInterim(_ text: String?) {
        guard let text, !text.isEmpty else { return }
        interimJapanese = text
    }
}
