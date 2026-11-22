import Foundation

extension Array where Element == MeetingEvent {
    public var transcriptDuration: TimeInterval? {
        let captionTypes: Set<String> = [
            "AsrCompletedEvent", "TranslationCompletedEvent", "TranscriptRevisionEvent", "CaptionGapEvent",
        ]
        return filter { captionTypes.contains($0.type) }
            .compactMap { $0.end ?? $0.start }
            .max()
    }
}

public struct GlossaryHit: Equatable, Identifiable, Sendable {
    public var term: GlossaryTerm
    public var lineCount: Int
    public var occurrenceCount: Int
    public var firstLineIndex: Int

    public var id: String { term.id }
}

public enum GlossaryHits {
    public static func find(
        in rows: [BilingualTranscriptRow],
        profile: GlossaryProfile,
        sourceLanguage: String = "ja"
    ) -> [GlossaryHit] {
        profile.entries.compactMap { term in
            guard !term.source.isEmpty, !term.target.isEmpty else { return nil }
            let sourceIsEnglish = sourceLanguage == "en"
            let sourceOptions: String.CompareOptions = sourceIsEnglish ? [.caseInsensitive, .diacriticInsensitive] : []
            let targetOptions: String.CompareOptions = sourceIsEnglish ? [] : [.caseInsensitive, .diacriticInsensitive]
            let matches = rows.compactMap { row -> (index: Int, count: Int)? in
                guard row.gap == nil else { return nil }
                let japanese = row.japanese?.text ?? ""
                let english = row.english?.text ?? ""
                let sourceText = sourceIsEnglish ? english : japanese
                let translatedText = sourceIsEnglish ? japanese : english
                let sourceCount = occurrenceCount(term.source, in: sourceText, options: sourceOptions)
                let fallbackText = term.isASRFix ? japanese : translatedText
                let fallbackOptions: String.CompareOptions = term.isASRFix ? [] : targetOptions
                let count =
                    sourceCount > 0
                    ? sourceCount
                    : occurrenceCount(term.target, in: fallbackText, options: fallbackOptions)
                return count > 0 ? (row.index, count) : nil
            }
            guard let first = matches.first else { return nil }
            return GlossaryHit(
                term: term,
                lineCount: matches.count,
                occurrenceCount: matches.reduce(0) { $0 + $1.count },
                firstLineIndex: first.index
            )
        }
        .sorted { $0.occurrenceCount > $1.occurrenceCount }
    }

    private static func occurrenceCount(
        _ needle: String,
        in text: String,
        options: String.CompareOptions
    ) -> Int {
        guard !needle.isEmpty else { return 0 }
        var count = 0
        var searchStart = text.startIndex
        while searchStart < text.endIndex,
            let match = text.range(of: needle, options: options, range: searchStart..<text.endIndex)
        {
            count += 1
            searchStart = match.upperBound
        }
        return count
    }
}
