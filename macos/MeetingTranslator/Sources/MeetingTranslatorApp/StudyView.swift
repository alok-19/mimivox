import MeetingTranslatorCore
import SwiftUI

struct StudyView: View {
    @EnvironmentObject private var store: TranslatorStore
    @State private var selectedSessionID: String?
    @State private var direction: StudyDirection = .japaneseToEnglish
    @State private var vocabulary: MeetingVocabulary?
    @State private var cardIndex = 0
    @State private var answerRevealed = false
    @State private var isLoading = false
    @State private var isExporting = false
    @State private var loadMessage: String?

    private struct MeetingVocabulary {
        let sessionID: String
        let words: [VocabularyWord]
    }

    private var cards: [StudyCard] {
        guard let vocabulary, vocabulary.sessionID == selectedSessionID else { return [] }
        return StudyCardDeck.make(from: vocabulary.words, direction: direction)
    }
    private var currentCard: StudyCard? { cards.indices.contains(cardIndex) ? cards[cardIndex] : nil }

    var body: some View {
        Group {
            if !store.archiveLoaded && store.archivedSessions.isEmpty {
                ProgressView(L10n.text("Loading study cards…"))
            } else if store.archivedSessions.isEmpty {
                ContentUnavailableView(
                    L10n.text("No saved meetings yet."),
                    systemImage: "rectangle.on.rectangle.angled",
                    description: Text(L10n.text("Save a meeting to study its terms.")))
            } else {
                studyContent
            }
        }
        .padding(28)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .textBackgroundColor))
        .task {
            if !store.archiveLoaded { await store.refreshArchivedSessions() }
            if selectedSessionID == nil { selectedSessionID = store.archivedSessions.first?.id }
        }
        .task(id: selectedSessionID) { await loadSelectedMeeting() }
        .onChange(of: selectedSessionID) { _, _ in resetCard() }
        .onChange(of: direction) { _, _ in resetCard() }
        .onChange(of: store.archivedSessions.map(\.id)) { _, sessionIDs in
            if let selectedSessionID, !sessionIDs.contains(selectedSessionID) {
                self.selectedSessionID = sessionIDs.first
            } else if selectedSessionID == nil {
                selectedSessionID = sessionIDs.first
            }
        }
    }

    private var studyContent: some View {
        VStack(alignment: .leading, spacing: 20) {
            controls
            if isLoading {
                ProgressView(L10n.text("Loading study cards…"))
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let loadMessage {
                ContentUnavailableView(
                    L10n.text("Could not load study cards"),
                    systemImage: "exclamationmark.triangle",
                    description: Text(loadMessage)
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else if let currentCard {
                ScrollView { review(card: currentCard) }
            } else if selectedSessionID == nil {
                ContentUnavailableView(
                    L10n.text("Choose a meeting"), systemImage: "rectangle.on.rectangle.angled"
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                ContentUnavailableView(
                    L10n.text("This meeting has no study cards."),
                    systemImage: "rectangle.on.rectangle.angled"
                )
                .frame(maxWidth: .infinity, maxHeight: .infinity)
            }
        }
        .frame(maxWidth: 760, maxHeight: .infinity, alignment: .topLeading)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }

    private var controls: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack(spacing: 14) {
                Picker(L10n.text("Choose a meeting"), selection: $selectedSessionID) {
                    Text(L10n.text("Choose a meeting")).tag(String?.none)
                    ForEach(store.archivedSessions) { session in
                        Text(session.headline ?? MeetingFormat.menuTitle(session))
                            .tag(Optional(session.id))
                    }
                }
                .frame(maxWidth: 390)

                Spacer(minLength: 8)

                Menu {
                    Button(L10n.text("Anki text")) {
                        Task { await export(format: "anki-cards") }
                    }
                    Button("CSV") {
                        Task { await export(format: "csv-cards") }
                    }
                } label: {
                    Label(L10n.text("Export study cards"), systemImage: "square.and.arrow.up")
                }
                .disabled(currentCard == nil || isExporting)
            }

            Picker(L10n.text("Direction"), selection: $direction) {
                Text(L10n.text("Japanese → English")).tag(StudyDirection.japaneseToEnglish)
                Text(L10n.text("English → Japanese")).tag(StudyDirection.englishToJapanese)
            }
            .pickerStyle(.segmented)
            .frame(maxWidth: 440)
        }
    }

    private func review(card: StudyCard) -> some View {
        VStack(alignment: .leading, spacing: 18) {
            Text(String(format: L10n.text("Card %lld of %lld"), cardIndex + 1, cards.count))
                .font(.callout.monospacedDigit())
                .foregroundStyle(.secondary)
                .accessibilityAddTraits(.updatesFrequently)

            GroupBox {
                VStack(alignment: .leading, spacing: 18) {
                    Text(card.front)
                        .font(.largeTitle.weight(.medium))
                        .textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading)

                    if answerRevealed {
                        Divider()
                        answer(card)
                    } else {
                        Button(L10n.text("Show answer"), systemImage: "eye") {
                            answerRevealed = true
                        }
                        .keyboardShortcut(.space, modifiers: [])
                    }
                }
                .padding(12)
                .frame(maxWidth: .infinity, minHeight: 230, alignment: .leading)
            }

            HStack {
                Button(L10n.text("Previous"), systemImage: "chevron.left") {
                    moveCard(by: -1)
                }
                .disabled(cardIndex == 0)

                Spacer()

                Button(L10n.text("Next"), systemImage: "chevron.right") {
                    moveCard(by: 1)
                }
                .disabled(cardIndex + 1 >= cards.count)
                .keyboardShortcut(.rightArrow, modifiers: [])
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }

    @ViewBuilder
    private func answer(_ card: StudyCard) -> some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(card.answer)
                .font(.title2.weight(.semibold))
                .textSelection(.enabled)
            if let reading = card.reading {
                Text(reading)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            if let example = card.exampleJapanese, !example.isEmpty {
                Text(example)
                    .font(.body)
                    .textSelection(.enabled)
            }
            if let translation = card.exampleEnglish, !translation.isEmpty {
                Text(translation)
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        }
    }

    private func loadSelectedMeeting() async {
        guard let id = selectedSessionID else {
            vocabulary = nil
            loadMessage = nil
            isLoading = false
            return
        }
        isLoading = true
        loadMessage = nil
        defer {
            if selectedSessionID == id { isLoading = false }
        }
        do {
            let meetingWords = try await store.studyVocabulary(sessionID: id)
            guard !Task.isCancelled, selectedSessionID == id else { return }
            vocabulary = MeetingVocabulary(sessionID: id, words: meetingWords)
        } catch {
            guard !Task.isCancelled, selectedSessionID == id else { return }
            loadMessage = error.localizedDescription
        }
    }

    private func export(format: String) async {
        guard let id = selectedSessionID, currentCard != nil, !isExporting else { return }
        isExporting = true
        defer { isExporting = false }
        do {
            let response = try await store.exportStudyCards(format: format, sessionID: id)
            if let path = response.path {
                store.post(.success(L10n.text("Study cards exported"), path: path))
            } else {
                store.post(.info(response.message))
            }
        } catch {
            store.post(
                .error(
                    String(format: L10n.text("Could not export study cards: %@"), error.localizedDescription)
                ))
        }
    }

    private func moveCard(by offset: Int) {
        cardIndex += offset
        answerRevealed = false
    }

    private func resetCard() {
        cardIndex = 0
        answerRevealed = false
    }
}
