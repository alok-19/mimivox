import Foundation
import MeetingTranslatorCore
import Observation

enum MeetingDateRange: String, CaseIterable, Identifiable {
    case anyTime = "Any Time"
    case week = "Last 7 Days"
    case month = "Last 30 Days"
    case year = "Last Year"

    var id: String { rawValue }

    /// ISO-8601 timestamp, with offset, of local midnight that many days back, for the `since` filter.
    func since(from now: Date = Date()) -> String? {
        let days: Int
        switch self {
        case .anyTime: return nil
        case .week: days = 7
        case .month: days = 30
        case .year: days = 365
        }
        let calendar = Calendar.current
        let day = calendar.date(byAdding: .day, value: -days, to: now) ?? now
        return Date.ISO8601FormatStyle(timeZoneSeparator: .colon, timeZone: calendar.timeZone).format(
            calendar.startOfDay(for: day))
    }
}

@MainActor
@Observable
final class MeetingsSearch {
    var query: String
    var range: MeetingDateRange = .anyTime
    var speaker = ""
    var tag = ""

    init(query: String = "") {
        self.query = query
    }

    var trimmedQuery: String { query.trimmingCharacters(in: .whitespacesAndNewlines) }

    var hasFilters: Bool { range != .anyTime || !speaker.isEmpty || !tag.isEmpty }

    var isActive: Bool { !trimmedQuery.isEmpty || hasFilters }

    var requestKey: String { "\(trimmedQuery)|\(range.rawValue)|\(speaker)|\(tag)" }

    func clearFilters() {
        range = .anyTime
        speaker = ""
        tag = ""
    }

    func clear() {
        query = ""
        clearFilters()
    }
}

extension LibraryLineHit {
    var transcriptRow: BilingualTranscriptRow {
        let tag = speaker.map { SpeakerTag(id: "speaker-\(Self.stableNumber($0))", label: $0) }
        return BilingualTranscriptRow(
            index: lineID,
            japanese: MeetingEvent(type: "AsrCompletedEvent", index: lineID, start: start, text: ja, speaker: tag),
            english: MeetingEvent(
                type: "TranslationCompletedEvent", index: lineID, start: start, text: en, speaker: tag)
        )
    }

    var highlights: RowHighlights {
        RowHighlights(
            english: enSnippet.isEmpty ? [SnippetSegment(text: en)] : enSnippet,
            japanese: jaSnippet.isEmpty ? [SnippetSegment(text: ja)] : jaSnippet
        )
    }

    private static func stableNumber(_ name: String) -> Int {
        name.unicodeScalars.reduce(7) { ($0 &* 31 &+ Int($1.value)) & 0xFFFF }
    }
}
