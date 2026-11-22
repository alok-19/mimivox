import MeetingTranslatorCore
import Observation
import SwiftUI

struct CitationChips: View {
    var lineIDs: [Int]
    var timecode: (Int) -> String?
    var onSelect: (Int) -> Void

    var body: some View {
        WrappingLayout(lineSpacing: 4) {
            ForEach(lineIDs, id: \.self) { id in
                if let code = timecode(id) {
                    Button(code) { onSelect(id) }
                        .buttonStyle(.bordered)
                        .controlSize(.mini)
                        .font(.caption.monospacedDigit())
                        .padding(.trailing, 4)
                        .help(L10n.text("Jump to this line in the transcript"))
                }
            }
        }
    }
}

/// One conversation with Ask the Meeting: the last question, its answer or error.
@MainActor
@Observable
final class AskThread {
    private(set) var question = ""
    private(set) var answer: AskAnswer?
    private(set) var error: String?
    private(set) var isAsking = false
    private var generation = 0

    /// Starts a question; the returned generation guards against a reset mid-flight.
    func begin(_ question: String) -> Int {
        self.question = question
        error = nil
        isAsking = true
        return generation
    }

    func finish(_ generation: Int, answer: AskAnswer) {
        guard generation == self.generation else { return }
        self.answer = answer
        isAsking = false
    }

    func finish(_ generation: Int, error: String) {
        guard generation == self.generation else { return }
        answer = nil
        self.error = error
        isAsking = false
    }

    func reset() {
        generation += 1
        question = ""
        answer = nil
        error = nil
        isAsking = false
    }
}

struct AskMeetingPanel: View {
    @EnvironmentObject private var store: TranslatorStore
    var thread: AskThread
    /// The session asked about; `nil` is the current or most recent one.
    var sessionID: String?
    var showsTitle = true
    var timecode: (Int) -> String?
    var onSelect: (Int) -> Void
    @State private var question = ""

    var body: some View {
        // Scrolls so a long answer never sets the window's minimum height.
        ScrollView {
            content
                .padding(16)
                .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private var content: some View {
        VStack(alignment: .leading, spacing: 12) {
            if showsTitle {
                Text(L10n.text("Ask the meeting"))
                    .font(.headline)
            }

            TextField(L10n.text("What did Tanaka-san say about the deadline?"), text: $question, axis: .vertical)
                .lineLimit(1...4)
                .textFieldStyle(.roundedBorder)
                .onSubmit(submit)

            HStack(spacing: 8) {
                Button(L10n.text("Ask"), action: submit)
                    .disabled(thread.isAsking || question.trimmingCharacters(in: .whitespaces).isEmpty)
                if thread.isAsking { ProgressView().controlSize(.small) }
            }

            if store.status?.modelReadiness.notesLlm?.ready == false {
                Text(L10n.text("Ask needs the notes model. Download it in Settings, under Models."))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let error = thread.error {
                Text(error)
                    .font(.callout)
                    .foregroundStyle(Palette.error)
                    .fixedSize(horizontal: false, vertical: true)
            }
            if let answer = thread.answer {
                VStack(alignment: .leading, spacing: 8) {
                    Text(answer.answer)
                        .textSelection(.enabled)
                        .fixedSize(horizontal: false, vertical: true)
                    if let phrase = answer.askInJapanese {
                        AskTheRoom(phrase: phrase)
                    } else if answer.lineIDs.isEmpty {
                        Text(L10n.text("No transcript lines cited.")).font(.caption).foregroundStyle(.secondary)
                    } else {
                        CitationChips(lineIDs: answer.lineIDs, timecode: timecode, onSelect: onSelect)
                    }
                }
            }
        }
    }

    private func submit() {
        let q = question
        Task { await store.ask(q, in: thread, sessionID: sessionID) }
    }
}

/// An unanswered question, phrased to put to the room in Japanese.
private struct AskTheRoom: View {
    let phrase: JapanesePhrase

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack {
                Text(L10n.text("Ask the room")).font(.caption.weight(.semibold)).foregroundStyle(.secondary)
                Spacer()
                Button {
                    NSPasteboard.general.clearContents()
                    NSPasteboard.general.setString(phrase.ja, forType: .string)
                } label: {
                    Image(systemName: "doc.on.doc")
                }
                .buttonStyle(.borderless)
                .controlSize(.small)
                .help(L10n.text("Copy the Japanese"))
            }
            Text(phrase.ja)
                .font(.title3)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
            Text(phrase.romaji)
                .font(.callout)
                .italic()
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
                .fixedSize(horizontal: false, vertical: true)
        }
        .padding(10)
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(.quaternary.opacity(0.5), in: RoundedRectangle(cornerRadius: 8))
    }
}
