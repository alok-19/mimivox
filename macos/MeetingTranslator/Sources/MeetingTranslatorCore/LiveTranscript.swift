import Foundation

/// The bilingual transcript, kept up to date one event at a time. Rows stay
/// sorted by start time and keep their id when their translation lands or is revised.
public struct LiveTranscript: Sendable {
    public private(set) var rows: [BilingualTranscriptRow] = []

    private var slotByID: [String: Int] = [:]
    private var asrByLine: [Int: MeetingEvent] = [:]
    private var translationsBySource: [Int: [MeetingEvent]] = [:]
    private var translationsByLine: [Int: [MeetingEvent]] = [:]
    private var revisions: [Int: MeetingEvent] = [:]
    private var merged: Set<Int> = []

    public init() {}

    public init(events: [MeetingEvent]) {
        for event in events { apply(event) }
    }

    /// Kinds of event that change rows; `apply` ignores every other kind.
    public static func changesRows(_ type: String) -> Bool {
        switch type {
        case "AsrCompletedEvent", "TranslationCompletedEvent", "TranscriptRevisionEvent", "CaptionGapEvent": true
        default: false
        }
    }

    public mutating func apply(_ event: MeetingEvent) {
        switch event.type {
        case "AsrCompletedEvent":
            guard let line = event.index, event.text?.isEmpty == false else { return }
            asrByLine[line] = event
            refresh(lines: related(to: line))
        case "TranslationCompletedEvent":
            let sources = Self.sources(of: event)
            guard event.text?.isEmpty == false, !sources.isEmpty else { return }
            for source in sources { translationsBySource[source, default: []].append(event) }
            if let line = event.index { translationsByLine[line, default: []].append(event) }
            refresh(lines: Set(sources))
        case "TranscriptRevisionEvent":
            guard let target = event.index else { return }
            revisions[target] = event
            let mergedNow = event.mergedIndexes ?? []
            merged.formUnion(mergedNow)
            refresh(lines: ([target] + mergedNow).reduce(into: Set()) { $0.formUnion(related(to: $1)) })
        case "CaptionGapEvent":
            let row = BilingualTranscriptRow(index: event.index ?? rows.count, japanese: nil, english: nil, gap: event)
            upsert(row, id: row.id)
        default:
            return
        }
    }

    /// Relabel one speaker everywhere (a `SpeakerRenamedEvent`'s effect).
    public mutating func renameSpeaker(_ tag: SpeakerTag) {
        func relabel(_ event: MeetingEvent) -> MeetingEvent {
            guard event.speaker?.id == tag.id else { return event }
            var event = event
            event.speaker = tag
            return event
        }
        asrByLine = asrByLine.mapValues(relabel)
        translationsBySource = translationsBySource.mapValues { $0.map(relabel) }
        translationsByLine = translationsByLine.mapValues { $0.map(relabel) }
        rows = rows.map { row in
            var row = row
            row.japanese = row.japanese.map(relabel)
            row.english = row.english.map(relabel)
            return row
        }
    }

    private static func sources(of translation: MeetingEvent) -> [Int] {
        translation.sourceIndexes ?? translation.index.map { [$0] } ?? []
    }

    private func related(to line: Int) -> Set<Int> {
        var lines: Set<Int> = [line]
        for translation in (translationsBySource[line] ?? []) + (translationsByLine[line] ?? []) {
            lines.formUnion(Self.sources(of: translation))
        }
        return lines
    }

    private mutating func refresh(lines: Set<Int>) {
        for line in lines.sorted() {
            upsert(row(for: line), id: BilingualTranscriptRow.lineID(line))
        }
    }

    private func row(for line: Int) -> BilingualTranscriptRow? {
        if let japanese = asrByLine[line].flatMap(revised) {
            let english = translationsBySource[line]?.lazy.compactMap(revised).first
            return BilingualTranscriptRow(index: line, japanese: japanese, english: english)
        }
        let english = translationsBySource[line]?.lazy.filter { translation in
            let sources = Self.sources(of: translation)
            return sources.first == line && !sources.contains { asrByLine[$0].flatMap(revised) != nil }
        }.compactMap(revised).first
        guard let english else { return nil }
        return BilingualTranscriptRow(index: line, japanese: nil, english: english)
    }

    private func revised(_ event: MeetingEvent) -> MeetingEvent? {
        guard let line = event.index else { return event }
        if merged.contains(line) { return nil }
        guard let revision = revisions[line] else { return event }
        var revised = event
        revised.isRevised = true
        revised.confidence = revision.confidence ?? event.confidence
        revised.end = revision.end ?? event.end
        if event.type == "AsrCompletedEvent" {
            revised.text = revision.japanese ?? event.text
            revised.tokens = revision.tokens
        } else {
            revised.text = revision.text ?? event.text
        }
        return revised
    }

    private mutating func upsert(_ row: BilingualTranscriptRow?, id: String) {
        if let slot = slotByID[id] {
            if let row, row.start == rows[slot].start {
                rows[slot] = row
                return
            }
            rows.remove(at: slot)
            slotByID[id] = nil
            reindex(from: slot)
        }
        guard let row else { return }
        let slot = insertionSlot(for: row)
        rows.insert(row, at: slot)
        reindex(from: slot)
    }

    private func insertionSlot(for row: BilingualTranscriptRow) -> Int {
        func precedes(_ lhs: BilingualTranscriptRow, _ rhs: BilingualTranscriptRow) -> Bool {
            lhs.start == rhs.start ? lhs.index < rhs.index : lhs.start < rhs.start
        }
        if let last = rows.last, !precedes(row, last) { return rows.count }
        var low = 0
        var high = rows.count
        while low < high {
            let mid = (low + high) / 2
            if precedes(rows[mid], row) { low = mid + 1 } else { high = mid }
        }
        return low
    }

    private mutating func reindex(from slot: Int) {
        for position in slot..<rows.count { slotByID[rows[position].id] = position }
    }
}

extension Array where Element == BilingualTranscriptRow {
    public var latestJapanese: MeetingEvent? { last { $0.japanese != nil }?.japanese }
    public var latestEnglish: MeetingEvent? { last { $0.english != nil }?.english }
}
