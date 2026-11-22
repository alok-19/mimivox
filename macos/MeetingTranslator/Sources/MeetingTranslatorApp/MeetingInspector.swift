import AppKit
import MeetingTranslatorCore
import SwiftUI
import UniformTypeIdentifiers

enum MeetingInspectorTab: String, CaseIterable, Identifiable {
    case notes, ask, words, info

    var id: String { rawValue }

    var title: String {
        switch self {
        case .notes: L10n.text("Notes")
        case .ask: L10n.text("Ask")
        case .words: L10n.text("Words")
        case .info: L10n.text("Info")
        }
    }
}

struct MeetingInspector: View {
    @EnvironmentObject private var store: TranslatorStore
    @Binding var tab: MeetingInspectorTab

    var body: some View {
        let review = store.review
        VStack(spacing: 0) {
            Picker(L10n.text("Inspector"), selection: $tab) {
                ForEach(MeetingInspectorTab.allCases) { Text($0.title).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .padding(12)
            Divider()
            if let detail = review.detail {
                switch tab {
                case .notes: NotesPane(review: review)
                case .ask:
                    AskMeetingPanel(
                        thread: review.ask,
                        sessionID: review.selectedID,
                        showsTitle: false,
                        timecode: { review.timecode(forLine: $0) },
                        onSelect: { review.focus(line: $0) }
                    )
                    .id(review.selectedID)
                case .words: WordsPane(review: review)
                case .info: InfoPane(detail: detail, review: review)
                }
            } else {
                EmptyState(
                    title: review.isLoading ? L10n.text("Opening…") : L10n.text("No Meeting Selected"),
                    systemImage: "clock.arrow.circlepath",
                    showsProgress: review.isLoading
                )
            }
        }
    }
}

// MARK: Notes

private struct NotesPane: View {
    @EnvironmentObject private var store: TranslatorStore
    @Bindable var review: ReviewSession

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 16) {
                if let notes = review.notes {
                    controls(notes)
                    NotesBody(notes: notes, language: review.notesLanguage, review: review)
                } else {
                    generate
                }
                if let error = review.notesError {
                    Text(error)
                        .font(.callout)
                        .foregroundStyle(Palette.error)
                        .fixedSize(horizontal: false, vertical: true)
                        .textSelection(.enabled)
                }
            }
            .padding(16)
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var generate: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.text("A summary, decisions and action items, written on this Mac in English and Japanese."))
                .font(.callout)
                .foregroundStyle(.secondary)
            if store.status?.modelReadiness.notesLlm?.ready == false {
                Text(L10n.text("Notes need the notes model. Download it in Settings, under Models."))
                    .font(.callout)
                    .foregroundStyle(.secondary)
            }
            HStack(spacing: 8) {
                Button(L10n.text("Generate Notes")) {
                    Task { await store.generateNotes(regenerate: false) }
                }
                .buttonStyle(.borderedProminent)
                .disabled(review.isGeneratingNotes || review.rows.isEmpty)
                if review.isGeneratingNotes { ProgressView().controlSize(.small) }
            }
            if review.isGeneratingNotes {
                Text(L10n.text("Summarizing on this Mac. The first run loads the model."))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func controls(_ notes: MeetingNotes) -> some View {
        HStack(spacing: 8) {
            Picker(L10n.text("Language"), selection: $review.notesLanguage) {
                ForEach(NotesLanguage.allCases) { Text($0.rawValue).tag($0) }
            }
            .pickerStyle(.segmented)
            .labelsHidden()
            .fixedSize()
            Spacer(minLength: 8)
            if review.isGeneratingNotes { ProgressView().controlSize(.small) }
            Menu {
                Button(L10n.text("Copy as Markdown")) {
                    let markdown = notes.markdown(language: review.notesLanguage) { review.timecode(forLine: $0) }
                    copyToPasteboard(markdown)
                }
                Divider()
                Button(L10n.text("Markdown")) { export(notes, as: .markdown) }
                Button(L10n.text("PDF")) { export(notes, as: .pdf) }
            } label: {
                Label(L10n.text("Export notes"), systemImage: "square.and.arrow.up")
                    .labelStyle(.iconOnly)
            }
            .buttonStyle(.borderless)
            .help(L10n.text("Export notes"))
            Button {
                Task { await store.generateNotes(regenerate: true) }
            } label: {
                Label(L10n.text("Write Again"), systemImage: "arrow.clockwise")
                    .labelStyle(.iconOnly)
            }
            .buttonStyle(.borderless)
            .disabled(review.isGeneratingNotes)
            .help(L10n.text("Write the notes again"))
        }
    }

    private func export(_ notes: MeetingNotes, as format: NotesExportFormat) {
        let title =
            store.archivedSessions.first { $0.id == review.selectedID }?.headline
            ?? L10n.text("Meeting notes")
        let data: Data
        do {
            switch format {
            case .markdown:
                let body = notes.markdown(language: review.notesLanguage) { review.timecode(forLine: $0) }
                data = Data("# \(title)\n\n\(body)".utf8)
            case .pdf:
                data = try MeetingNotesPDF.render(
                    notes, title: title, language: review.notesLanguage,
                    lineLabel: { review.timecode(forLine: $0) }
                )
            }
        } catch {
            store.post(
                .error(
                    String(format: L10n.text("Could not save notes: %@"), error.localizedDescription),
                    command: "Export notes"
                ))
            return
        }

        let panel = NSSavePanel()
        panel.allowedContentTypes = [format.contentType]
        let safeTitle = title.replacingOccurrences(of: "/", with: "-")
            .replacingOccurrences(of: ":", with: "-")
        panel.nameFieldStringValue = "\(safeTitle).\(format.fileExtension)"
        guard panel.runModal() == .OK, let url = panel.url else { return }
        do {
            try data.write(to: url, options: .atomic)
            store.post(.success(L10n.text("Notes exported"), path: url.path))
        } catch {
            store.post(
                .error(
                    String(format: L10n.text("Could not save notes: %@"), error.localizedDescription),
                    command: "Export notes"
                ))
        }
    }
}

private enum NotesExportFormat: Equatable {
    case markdown
    case pdf

    var fileExtension: String { self == .markdown ? "md" : "pdf" }
    var contentType: UTType { self == .markdown ? UTType(filenameExtension: "md") ?? .plainText : .pdf }
}

private struct NotesBody: View {
    var notes: MeetingNotes
    var language: NotesLanguage
    var review: ReviewSession

    private var isJapanese: Bool { language == .ja }

    var body: some View {
        section(isJapanese ? "要約" : "Summary") {
            Text(notes.summary(in: language))
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        section(isJapanese ? "決定事項" : "Decisions") {
            if notes.decisions.isEmpty { none }
            ForEach(notes.decisions) { decision in
                item(decision.text(in: language), detail: nil, lineIDs: decision.lineIDs)
            }
        }
        section(isJapanese ? "アクションアイテム" : "Action Items") {
            if notes.actionItems.isEmpty { none }
            ForEach(notes.actionItems) { action in
                item(action.text(in: language), detail: detail(of: action), lineIDs: action.lineIDs)
            }
        }
    }

    private var none: some View {
        Text(isJapanese ? "なし" : "None")
            .foregroundStyle(.secondary)
    }

    private func detail(of action: NotesActionItem) -> String? {
        let parts = [action.owner, action.due].compactMap { $0 }
        return parts.isEmpty ? nil : parts.joined(separator: " · ")
    }

    private func item(_ text: String, detail: String?, lineIDs: [Int]) -> some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(text)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            if let detail {
                Text(detail)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            CitationChips(
                lineIDs: lineIDs,
                timecode: { review.timecode(forLine: $0) },
                onSelect: { review.focus(line: $0) }
            )
        }
    }

    private func section<Content: View>(_ title: String, @ViewBuilder content: () -> Content) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(title)
                .font(.subheadline.weight(.semibold))
                .foregroundStyle(.secondary)
            content()
        }
        .frame(maxWidth: .infinity, alignment: .leading)
    }
}

// MARK: Words

private struct WordsPane: View {
    @EnvironmentObject private var store: TranslatorStore
    var review: ReviewSession
    @State private var sortOrder = MeetingWordsSortOrder.frequency

    var body: some View {
        Form {
            Picker(L10n.text("Sort by"), selection: $sortOrder) {
                Text(L10n.text("Frequency")).tag(MeetingWordsSortOrder.frequency)
                Text(L10n.text("Alphabetical")).tag(MeetingWordsSortOrder.alphabetical)
            }
            Section(L10n.text("Glossary")) { glossary }
            Section {
                keyTerms
            } header: {
                HStack {
                    Text(L10n.text("Key Terms"))
                    Spacer()
                    exportMenu
                }
            }
        }
        .formStyle(.grouped)
        .task(id: review.selectedID) {
            if review.vocabulary == nil { await store.loadVocabulary() }
        }
    }

    @ViewBuilder
    private var glossary: some View {
        if review.glossaryHits.isEmpty {
            Text(emptyGlossaryText)
                .foregroundStyle(.secondary)
        }
        ForEach(sortedGlossaryHits) { hit in
            HStack(alignment: .firstTextBaseline, spacing: 8) {
                Button {
                    review.focus(line: hit.firstLineIndex)
                } label: {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(hit.term.source)
                            Text(hit.term.target)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                        }
                        Spacer(minLength: 8)
                        Text("×\(hit.occurrenceCount)")
                            .font(.callout.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
                .help(L10n.text("Jump to the first line that uses this term"))
                Button {
                    store.addGlossaryTerm(source: hit.term.source, target: hit.term.target)
                } label: {
                    Image(systemName: "plus")
                }
                .buttonStyle(.borderless)
                .help(String(format: L10n.text("Add %@ to %@"), hit.term.source, store.glossaryDestination))
                .accessibilityLabel(
                    String(format: L10n.text("Add %@ to %@"), hit.term.source, store.glossaryDestination))
            }
        }
    }

    private var sortedGlossaryHits: [GlossaryHit] {
        switch sortOrder {
        case .frequency:
            review.glossaryHits.sorted {
                $0.occurrenceCount == $1.occurrenceCount
                    ? $0.term.source.localizedStandardCompare($1.term.source) == .orderedAscending
                    : $0.occurrenceCount > $1.occurrenceCount
            }
        case .alphabetical:
            review.glossaryHits.sorted {
                $0.term.source.localizedStandardCompare($1.term.source) == .orderedAscending
            }
        }
    }

    private var emptyGlossaryText: String {
        let profile = review.detail?.settings?.activeGlossaryProfile ?? ""
        return profile.isEmpty
            ? L10n.text("This meeting didn't use a glossary.")
            : String(format: L10n.text("No terms from %@ came up in this meeting."), profile)
    }

    @ViewBuilder
    private var keyTerms: some View {
        if let words = review.vocabulary {
            if words.isEmpty {
                Text(L10n.text("No words found in this meeting."))
                    .foregroundStyle(.secondary)
            }
            ForEach(sortedWords(words)) { word in
                HStack(alignment: .firstTextBaseline, spacing: 8) {
                    Button {
                        if let line = word.lineID { review.focus(line: line) }
                    } label: {
                        VStack(alignment: .leading, spacing: 4) {
                            HStack(alignment: .firstTextBaseline, spacing: 8) {
                                Text(word.word).fontWeight(.medium)
                                Text(word.reading).font(.callout).foregroundStyle(.secondary)
                            }
                            Text(word.gloss)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .lineLimit(2)
                        }
                        .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    Spacer(minLength: 8)
                    if word.count > 1 {
                        Text("×\(word.count)")
                            .font(.callout.monospacedDigit())
                            .foregroundStyle(.secondary)
                    }
                    Button {
                        store.addGlossaryTerm(source: word.word, target: word.gloss)
                    } label: {
                        Image(systemName: "plus")
                    }
                    .buttonStyle(.borderless)
                    .help(String(format: L10n.text("Add %@ to %@"), word.word, store.glossaryDestination))
                    .accessibilityLabel(String(format: L10n.text("Add %@ to %@"), word.word, store.glossaryDestination))
                }
            }
            if let message = review.vocabularyMessage {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
        } else if review.isLoadingVocabulary || review.vocabularyMessage == nil {
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(L10n.text("Finding words…"))
                    .foregroundStyle(.secondary)
            }
        } else {
            if let message = review.vocabularyMessage {
                Text(message)
                    .foregroundStyle(Palette.error)
                    .textSelection(.enabled)
            }
            Button(L10n.text("Try Again")) { Task { await store.loadVocabulary() } }
        }
    }

    private func sortedWords(_ words: [VocabularyWord]) -> [VocabularyWord] {
        switch sortOrder {
        case .frequency:
            words.sorted {
                $0.count == $1.count
                    ? $0.word.localizedStandardCompare($1.word) == .orderedAscending
                    : $0.count > $1.count
            }
        case .alphabetical:
            words.sorted { $0.word.localizedStandardCompare($1.word) == .orderedAscending }
        }
    }

    private var exportMenu: some View {
        Menu {
            Button(L10n.text("Spreadsheet (CSV)")) { Task { await store.exportVocabulary(format: "csv") } }
            Button(L10n.text("Anki text")) { Task { await store.exportVocabulary(format: "anki") } }
        } label: {
            Label(L10n.text("Export Words"), systemImage: "square.and.arrow.up")
                .labelStyle(.iconOnly)
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .disabled(review.vocabulary?.isEmpty ?? true)
        .help(L10n.text("Export the words for flashcards"))
    }
}

private enum MeetingWordsSortOrder: String, Hashable {
    case frequency
    case alphabetical
}

// MARK: Info

private struct InfoPane: View {
    @EnvironmentObject private var store: TranslatorStore
    var detail: ArchivedSessionDetail
    var review: ReviewSession
    @State private var newTag = ""

    var body: some View {
        Form {
            Section {
                LabeledContent(L10n.text("Date")) { Text(dateText).monospacedDigit() }
                LabeledContent(L10n.text("Duration")) {
                    Text(review.duration.map(MeetingFormat.duration) ?? L10n.text("None")).monospacedDigit()
                }
                LabeledContent(L10n.text("Source"), value: MeetingFormat.source(label: detail.label))
                if let file = MeetingFormat.fileName(label: detail.label) {
                    LabeledContent(L10n.text("File"), value: file)
                }
                if let settings = detail.settings {
                    LabeledContent(L10n.text("Languages"), value: settings.languagePair.label)
                    if !settings.activeGlossaryProfile.isEmpty {
                        LabeledContent(L10n.text("Glossary"), value: settings.activeGlossaryProfile)
                    }
                }
                LabeledContent(L10n.text("Lines")) { Text("\(review.lineCount)").monospacedDigit() }
            }
            if !review.speakers.isEmpty {
                Section(L10n.text("Speakers")) {
                    ForEach(review.speakers, id: \.id) { speaker in
                        SpeakerRenameButton(
                            tag: speaker,
                            onRename: { label in
                                guard let sessionID = review.selectedID else { return }
                                Task {
                                    await store.renameArchivedSpeaker(
                                        id: speaker.id, label: label, sessionID: sessionID)
                                }
                            }
                        ) {
                            Label(speaker.label, systemImage: "pencil")
                        }
                    }
                }
            }
            Section(L10n.text("Tags")) {
                ForEach(review.tags, id: \.self) { tag in
                    HStack {
                        Text(tag)
                        Spacer()
                        Button {
                            Task { await store.updateReviewTags(remove: [tag]) }
                        } label: {
                            Label(L10n.text("Remove Tag"), systemImage: "minus.circle")
                                .labelStyle(.iconOnly)
                        }
                        .buttonStyle(.borderless)
                        .help(L10n.text("Remove this tag"))
                    }
                }
                TextField(L10n.text("Add Tag"), text: $newTag, prompt: Text(L10n.text("Add a tag")))
                    .labelsHidden()
                    .textFieldStyle(.roundedBorder)
                    .onSubmit(addTag)
            }
        }
        .formStyle(.grouped)
    }

    private var dateText: String {
        MeetingFormat.date(from: detail.startedAt).map(MeetingFormat.stamp) ?? detail.startedAt
    }

    private func addTag() {
        let tag = newTag.trimmingCharacters(in: .whitespacesAndNewlines)
        newTag = ""
        guard !tag.isEmpty else { return }
        Task { await store.updateReviewTags(add: [tag]) }
    }
}
