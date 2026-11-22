import Foundation

public enum StudyDirection: String, CaseIterable, Identifiable, Sendable {
    case japaneseToEnglish
    case englishToJapanese

    public var id: String { rawValue }
}

public struct StudyCard: Equatable, Identifiable, Sendable {
    public let word: VocabularyWord
    public let direction: StudyDirection

    public var id: String { "\(direction.rawValue):\(word.id)" }
    public var front: String { direction == .japaneseToEnglish ? word.word : word.gloss }
    public var answer: String { direction == .japaneseToEnglish ? word.gloss : word.word }
    public var reading: String? { word.reading.isEmpty ? nil : word.reading }
    public var exampleJapanese: String? { word.exampleJapanese }
    public var exampleEnglish: String? { word.exampleEnglish }

    public init(word: VocabularyWord, direction: StudyDirection) {
        self.word = word
        self.direction = direction
    }
}

public enum StudyCardDeck {
    public static func make(from words: [VocabularyWord], direction: StudyDirection) -> [StudyCard] {
        words
            .sorted {
                $0.count == $1.count ? $0.word < $1.word : $0.count > $1.count
            }
            .map { StudyCard(word: $0, direction: direction) }
    }
}
