import Foundation
import MeetingTranslatorCore
import Observation

@MainActor
@Observable
final class ReviewSession {
    /// The meeting on screen; actions apply to it.
    private(set) var selectedID: String?
    /// The meeting being opened. The screen keeps showing `selectedID` until it
    /// arrives, then swaps in one step, so switching meetings never blanks the view.
    private(set) var openingID: String?
    private var openingFocus: Int?
    private(set) var detail: ArchivedSessionDetail?
    private(set) var rows: [BilingualTranscriptRow] = []
    private(set) var pinned: Set<Int> = []
    private(set) var glossaryHits: [GlossaryHit] = []
    var speakers: [SpeakerTag] {
        var byID: [String: SpeakerTag] = [:]
        for row in rows {
            for speaker in [row.japanese?.speaker, row.english?.speaker, row.gap?.speaker].compactMap({ $0 }) {
                byID[speaker.id] = speaker
            }
        }
        return byID.values.sorted { $0.label.localizedStandardCompare($1.label) == .orderedAscending }
    }
    var tags: [String] = []
    private(set) var lineCount = 0
    private(set) var focusRequest: TranscriptScrollRequest?

    var notes: MeetingNotes?
    var notesLanguage: NotesLanguage = .en
    var isGeneratingNotes = false
    var notesError: String?

    /// Ask the Meeting for the open meeting; cleared when another one opens.
    let ask = AskThread()

    var vocabulary: [VocabularyWord]?
    var isLoadingVocabulary = false
    var vocabularyMessage: String?

    var duration: TimeInterval? { detail?.events.transcriptDuration }

    var hasSkippedCaptions: Bool {
        detail?.events.contains { $0.type == "CaptionGapEvent" || $0.translationSkipped != nil } == true
    }

    var isLoading: Bool { openingID != nil }

    /// The meeting the list highlights: the one opening, else the one shown.
    var highlightedID: String? { openingID ?? selectedID }

    func beginLoading(id: String, focusLine: Int? = nil) {
        openingID = id
        openingFocus = focusLine
    }

    var focusLineID: Int? { focusRequest?.index }

    func focus(line id: Int) {
        focusRequest = TranscriptScrollRequest(index: id, nonce: (focusRequest?.nonce ?? 0) + 1)
    }

    func apply(_ detail: ArchivedSessionDetail) {
        guard detail.id == openingID else { return }
        let focusLine = openingFocus
        reset()
        selectedID = detail.id
        self.detail = detail
        setRows(detail.events.bilingualTranscriptRows)
        pinned = Set(detail.pinnedIDs)
        tags = detail.tags
        notes = detail.notes
        if let focusLine { focus(line: focusLine) }
        glossaryHits =
            detail.settings
            .flatMap { settings in settings.glossaryProfiles.first { $0.name == settings.activeGlossaryProfile } }
            .map { GlossaryHits.find(in: rows, profile: $0, sourceLanguage: detail.settings?.sourceLang ?? "ja") }
            ?? []
    }

    func failLoading() {
        openingID = nil
        openingFocus = nil
    }

    func replaceEvents(_ events: [MeetingEvent]) {
        guard var detail else { return }
        detail.events = events
        self.detail = detail
        setRows(events.bilingualTranscriptRows)
    }

    func clear() {
        reset()
        selectedID = nil
    }

    func setPinned(_ indexes: Set<Int>) {
        pinned = indexes
    }

    func timecode(forLine id: Int) -> String? {
        rows.first { $0.index == id }?.timecode
    }

    private func setRows(_ rows: [BilingualTranscriptRow]) {
        self.rows = rows
        lineCount = rows.count { $0.gap == nil }
    }

    private func reset() {
        detail = nil
        rows = []
        lineCount = 0
        pinned = []
        tags = []
        glossaryHits = []
        focusRequest = nil
        notes = nil
        notesError = nil
        isGeneratingNotes = false
        ask.reset()
        vocabulary = nil
        vocabularyMessage = nil
        isLoadingVocabulary = false
        openingID = nil
        openingFocus = nil
    }
}
