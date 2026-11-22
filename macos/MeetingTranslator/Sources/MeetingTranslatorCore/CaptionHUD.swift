import Foundation

/// Which language(s) the floating caption HUD shows for a line.
public enum CaptionDisplayMode: String, CaseIterable, Identifiable, Sendable {
    case englishOnly
    case bilingual
    case japaneseOnly

    public var id: String { rawValue }

    public var title: String {
        switch self {
        case .englishOnly: "English"
        case .bilingual: "English + Japanese"
        case .japaneseOnly: "Japanese"
        }
    }

    /// What one line shows. In a single-language mode, `revealed` (the viewer
    /// clicked the line) adds the other language under it.
    public func visibility(revealed: Bool) -> CaptionLineVisibility {
        switch self {
        case .englishOnly:
            CaptionLineVisibility(showsEnglish: true, showsJapanese: revealed, canReveal: true)
        case .bilingual:
            CaptionLineVisibility(showsEnglish: true, showsJapanese: true, canReveal: false)
        case .japaneseOnly:
            CaptionLineVisibility(showsEnglish: revealed, showsJapanese: true, canReveal: true)
        }
    }
}

public struct CaptionLineVisibility: Equatable, Sendable {
    public var showsEnglish: Bool
    public var showsJapanese: Bool
    /// True when a click on the line has something to reveal.
    public var canReveal: Bool

    public init(showsEnglish: Bool, showsJapanese: Bool, canReveal: Bool) {
        self.showsEnglish = showsEnglish
        self.showsJapanese = showsJapanese
        self.canReveal = canReveal
    }
}

extension Array where Element == BilingualTranscriptRow {
    /// Markdown for the lines the viewer pinned, oldest first, for copying out
    /// of the HUD. Rows are matched by `index`, the id the server stores in `pinned_ids`.
    public func pinnedMarkdown(pinned: Set<Int>) -> String {
        let rows = filter { pinned.contains($0.index) }
        guard !rows.isEmpty else { return "" }
        var lines = ["## Pinned lines", ""]
        for row in rows {
            let stamp = String(
                format: "%02d:%05.2f", Int(row.start) / 60, row.start.truncatingRemainder(dividingBy: 60))
            let who = row.speaker.map { " \($0.label)" } ?? ""
            lines.append("- [\(stamp)]\(who) \(row.english?.text ?? "—")")
            if let japanese = row.japanese?.text {
                lines.append("  - JA: \(japanese)")
            }
        }
        return lines.joined(separator: "\n") + "\n"
    }
}
