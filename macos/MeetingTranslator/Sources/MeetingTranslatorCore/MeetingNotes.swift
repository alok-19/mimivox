import Foundation

public enum NotesLanguage: String, CaseIterable, Identifiable, Sendable {
    case en = "EN"
    case ja = "JA"

    public var id: String { rawValue }
}

public struct NotesDecision: Codable, Equatable, Identifiable, Sendable {
    public var text: String
    public var textJa: String
    public var lineIDs: [Int]

    public var id: String { text }

    public init(text: String, textJa: String = "", lineIDs: [Int] = []) {
        self.text = text
        self.textJa = textJa
        self.lineIDs = lineIDs
    }

    private enum CodingKeys: String, CodingKey {
        case text
        case textJa = "text_ja"
        case lineIDs = "line_ids"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        text = try c.decode(String.self, forKey: .text)
        textJa = try c.decodeIfPresent(String.self, forKey: .textJa) ?? ""
        lineIDs = try c.decodeIfPresent([Int].self, forKey: .lineIDs) ?? []
    }

    public func text(in language: NotesLanguage) -> String {
        language == .ja && !textJa.isEmpty ? textJa : text
    }
}

public struct NotesActionItem: Codable, Equatable, Identifiable, Sendable {
    public var owner: String?
    public var text: String
    public var textJa: String
    public var due: String?
    public var lineIDs: [Int]

    public var id: String { text }

    public init(owner: String? = nil, text: String, textJa: String = "", due: String? = nil, lineIDs: [Int] = []) {
        self.owner = owner
        self.text = text
        self.textJa = textJa
        self.due = due
        self.lineIDs = lineIDs
    }

    private enum CodingKeys: String, CodingKey {
        case owner, text, due
        case textJa = "text_ja"
        case lineIDs = "line_ids"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        owner = try c.decodeIfPresent(String.self, forKey: .owner)
        text = try c.decode(String.self, forKey: .text)
        textJa = try c.decodeIfPresent(String.self, forKey: .textJa) ?? ""
        due = try c.decodeIfPresent(String.self, forKey: .due)
        lineIDs = try c.decodeIfPresent([Int].self, forKey: .lineIDs) ?? []
    }

    public func text(in language: NotesLanguage) -> String {
        language == .ja && !textJa.isEmpty ? textJa : text
    }
}

/// Server-side `meta` of a notes / ask response (absent in archived notes).
public struct NotesMeta: Codable, Equatable, Sendable {
    public var cached: Bool?
    public var model: String?
    public var latencyS: Double?

    public init(cached: Bool? = nil, model: String? = nil, latencyS: Double? = nil) {
        self.cached = cached
        self.model = model
        self.latencyS = latencyS
    }

    private enum CodingKeys: String, CodingKey {
        case cached, model
        case latencyS = "latency_s"
    }
}

public struct MeetingNotes: Codable, Equatable, Sendable {
    public var summaryEn: String
    public var summaryJa: String
    public var decisions: [NotesDecision]
    public var actionItems: [NotesActionItem]
    public var meta: NotesMeta?

    public init(
        summaryEn: String = "",
        summaryJa: String = "",
        decisions: [NotesDecision] = [],
        actionItems: [NotesActionItem] = [],
        meta: NotesMeta? = nil
    ) {
        self.summaryEn = summaryEn
        self.summaryJa = summaryJa
        self.decisions = decisions
        self.actionItems = actionItems
        self.meta = meta
    }

    private enum CodingKeys: String, CodingKey {
        case decisions, meta
        case summaryEn = "summary_en"
        case summaryJa = "summary_ja"
        case actionItems = "action_items"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        summaryEn = try c.decodeIfPresent(String.self, forKey: .summaryEn) ?? ""
        summaryJa = try c.decodeIfPresent(String.self, forKey: .summaryJa) ?? ""
        decisions = try c.decodeIfPresent([NotesDecision].self, forKey: .decisions) ?? []
        actionItems = try c.decodeIfPresent([NotesActionItem].self, forKey: .actionItems) ?? []
        meta = try c.decodeIfPresent(NotesMeta.self, forKey: .meta)
    }

    public func summary(in language: NotesLanguage) -> String {
        let text = language == .ja ? summaryJa : summaryEn
        return text.isEmpty ? (language == .ja ? summaryEn : summaryJa) : text
    }

    /// Notes as Markdown for the clipboard. `lineLabel` names a cited line id
    /// (e.g. its timecode); ids it returns nil for are left out.
    public func markdown(language: NotesLanguage, lineLabel: (Int) -> String? = { "#\($0)" }) -> String {
        let ja = language == .ja
        func cite(_ ids: [Int]) -> String {
            let labels = ids.compactMap(lineLabel)
            return labels.isEmpty ? "" : " _(\(labels.joined(separator: ", ")))_"
        }
        var out = ["## \(ja ? "要約" : "Summary")", "", summary(in: language), ""]
        out += ["## \(ja ? "決定事項" : "Decisions")", ""]
        out +=
            decisions.isEmpty
            ? ["_\(ja ? "なし" : "None")._"]
            : decisions.map { "- \($0.text(in: language))\(cite($0.lineIDs))" }
        out += ["", "## \(ja ? "アクションアイテム" : "Action items")", ""]
        out +=
            actionItems.isEmpty
            ? ["_\(ja ? "なし" : "None")._"]
            : actionItems.map { item in
                var meta: [String] = []
                if let owner = item.owner { meta.append("\(ja ? "担当" : "owner"): \(owner)") }
                if let due = item.due { meta.append("\(ja ? "期限" : "due"): \(due)") }
                let suffix = meta.isEmpty ? "" : " (\(meta.joined(separator: ", ")))"
                return "- \(item.text(in: language))\(suffix)\(cite(item.lineIDs))"
            }
        return out.joined(separator: "\n") + "\n"
    }
}

/// A question phrased to ask aloud in Japanese, with its romaji.
public struct JapanesePhrase: Codable, Equatable, Sendable {
    public var ja: String
    public var romaji: String

    public init(ja: String, romaji: String) {
        self.ja = ja
        self.romaji = romaji
    }
}

/// `POST /api/session/ask` reply: the answer plus the transcript lines it cites.
/// During a live meeting, an unanswered question also comes back phrased in Japanese.
public struct AskAnswer: Codable, Equatable, Sendable {
    public var answer: String
    public var lineIDs: [Int]
    public var askInJapanese: JapanesePhrase?
    public var meta: NotesMeta?

    public init(
        answer: String, lineIDs: [Int] = [], askInJapanese: JapanesePhrase? = nil, meta: NotesMeta? = nil
    ) {
        self.answer = answer
        self.lineIDs = lineIDs
        self.askInJapanese = askInJapanese
        self.meta = meta
    }

    private enum CodingKeys: String, CodingKey {
        case answer, meta
        case lineIDs = "line_ids"
        case askInJapanese = "ask_in_japanese"
    }

    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        answer = try c.decode(String.self, forKey: .answer)
        lineIDs = try c.decodeIfPresent([Int].self, forKey: .lineIDs) ?? []
        askInJapanese = try c.decodeIfPresent(JapanesePhrase.self, forKey: .askInJapanese)
        meta = try c.decodeIfPresent(NotesMeta.self, forKey: .meta)
    }
}
