import AppKit
import MeetingTranslatorCore
import SwiftUI

extension BilingualTranscriptRow {
    @MainActor var timecode: String { MeetingFormat.timecode(start) }
}

@MainActor
struct TranscriptRowActions: @MainActor Equatable {
    enum Scope {
        case live, review, search
    }

    var store: TranslatorStore
    var scope: Scope = .live

    var canRenameSpeakers: Bool { scope == .live }
    var canPin: Bool { scope != .search }

    static func == (lhs: TranscriptRowActions, rhs: TranscriptRowActions) -> Bool {
        lhs.store === rhs.store && lhs.scope == rhs.scope
    }

    func renameSpeaker(id: String, label: String) {
        Task { await store.renameSpeaker(id: id, label: label) }
    }

    func togglePin(index: Int) {
        switch scope {
        case .live: store.togglePin(index: index)
        case .review: store.toggleReviewPin(index: index)
        case .search: break
        }
    }
}

struct TranscriptLine: Identifiable {
    var row: BilingualTranscriptRow
    var showsSpeaker: Bool
    var id: String { row.id }

    static func lines(for rows: [BilingualTranscriptRow]) -> [TranscriptLine] {
        var previous: String?
        return rows.map { row in
            let speaker = row.speaker?.id
            defer { if row.gap == nil { previous = speaker } }
            return TranscriptLine(row: row, showsSpeaker: speaker != nil && speaker != previous)
        }
    }
}

struct RowHighlights: Equatable {
    var english: [SnippetSegment]
    var japanese: [SnippetSegment]
}

extension Array where Element == SnippetSegment {
    fileprivate var attributed: AttributedString {
        var out = AttributedString()
        for segment in self {
            var piece = AttributedString(segment.text)
            if segment.hit {
                piece.inlinePresentationIntent = .stronglyEmphasized
                piece.foregroundColor = .accentColor
            }
            out.append(piece)
        }
        return out
    }
}

struct TranscriptRow: View, @MainActor Equatable {
    static let gutterWidth: CGFloat = 56

    var row: BilingualTranscriptRow
    var showsSpeaker: Bool
    var isCurrent: Bool
    var isPinned: Bool
    var pendingText: String
    var actions: TranscriptRowActions
    var highlights: RowHighlights?

    static func == (lhs: TranscriptRow, rhs: TranscriptRow) -> Bool {
        lhs.row == rhs.row
            && lhs.showsSpeaker == rhs.showsSpeaker
            && lhs.isCurrent == rhs.isCurrent
            && lhs.isPinned == rhs.isPinned
            && lhs.pendingText == rhs.pendingText
            && lhs.actions == rhs.actions
            && lhs.highlights == rhs.highlights
    }

    var body: some View {
        if row.gap != nil {
            SkipDivider(text: row.skipNote.map { L10n.text($0) } ?? L10n.text("Speech skipped while catching up"))
        } else {
            captionRow
        }
    }

    private var captionRow: some View {
        VStack(alignment: .leading, spacing: 4) {
            if showsSpeaker, let speaker = row.speaker {
                SpeakerRenameButton(
                    tag: speaker,
                    onRename: actions.canRenameSpeakers ? { actions.renameSpeaker(id: speaker.id, label: $0) } : nil
                ) {
                    Text(speaker.label)
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(Palette.speaker(speaker))
                        .lineLimit(1)
                }
                .padding(.leading, Self.gutterWidth + 12)
            }
            HStack(alignment: .firstTextBaseline, spacing: 12) {
                gutter
                VStack(alignment: .leading, spacing: 4) {
                    englishText
                        .font(isCurrent ? .title3.weight(.semibold) : .title3)
                        .foregroundStyle(row.english?.text == nil ? Color.secondary : Color.primary)
                        .fixedSize(horizontal: false, vertical: true)
                        .rowTextSelection(actions.scope != .search)
                    japanese
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 12)
        .background(isCurrent ? Palette.currentRow : Color.clear, in: RoundedRectangle(cornerRadius: 8))
        .contextMenu {
            if let english = row.english?.text {
                Button(L10n.text("Copy English")) { copyToPasteboard(english) }
            }
            if let japanese = row.japanese?.text, !japanese.isEmpty {
                Button(L10n.text("Copy Japanese")) { copyToPasteboard(japanese) }
            }
            if actions.canPin {
                Button(L10n.text(isPinned ? "Unpin Line" : "Pin Line")) { actions.togglePin(index: row.index) }
            }
        }
        .accessibilityElement(children: .combine)
    }

    private var gutter: some View {
        HStack(alignment: .firstTextBaseline, spacing: 4) {
            Text(row.timecode)
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .fixedSize()
            if isPinned {
                Image(systemName: "pin.fill")
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .accessibilityLabel(L10n.text("Pinned"))
            }
            if let confidence = row.flaggedConfidence {
                ConfidenceMarker(confidence: confidence)
            }
        }
        .frame(width: Self.gutterWidth, alignment: .leading)
    }

    private var englishText: Text {
        if let matches = highlights?.english, !matches.isEmpty {
            return Text(matches.attributed)
        }
        return Text(row.english?.text ?? row.skipNote ?? pendingText)
    }

    @ViewBuilder
    private var japanese: some View {
        if let matches = highlights?.japanese, !matches.isEmpty {
            Text(matches.attributed)
                .font(.body)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .rowTextSelection(actions.scope != .search)
        } else if let text = row.japanese?.text, !text.isEmpty {
            if row.japanese?.tokens?.matching(text) != nil {
                FuriganaText(text: text, tokens: row.japanese?.tokens, size: 15)
            } else {
                Text(text)
                    .font(.body)
                    .foregroundStyle(.secondary)
                    .fixedSize(horizontal: false, vertical: true)
                    .rowTextSelection(actions.scope != .search)
            }
        }
    }
}

struct SkipDivider: View {
    var text: String

    var body: some View {
        HStack(spacing: 12) {
            rule
            Text(text)
                .font(.caption)
                .foregroundStyle(.secondary)
                .lineLimit(1)
                .layoutPriority(1)
            rule
        }
        .padding(.vertical, 8)
        .padding(.horizontal, 12)
        .padding(.leading, TranscriptRow.gutterWidth + 12)
        .accessibilityElement(children: .combine)
    }

    private var rule: some View {
        Rectangle()
            .fill(.separator)
            .frame(height: 1)
    }
}

func copyToPasteboard(_ text: String) {
    guard !text.isEmpty else {
        return
    }
    NSPasteboard.general.clearContents()
    NSPasteboard.general.setString(text, forType: .string)
}

struct ConfidenceMarker: View {
    var confidence: CaptionConfidence

    var body: some View {
        Image(systemName: confidence.level == .low ? "exclamationmark.circle.fill" : "questionmark.circle")
            .font(.caption)
            .foregroundStyle(confidence.level == .low ? Palette.error : Palette.warning)
            .help(confidence.help)
            .accessibilityLabel(confidence.help)
    }
}

extension View {
    @ViewBuilder
    fileprivate func rowTextSelection(_ isEnabled: Bool) -> some View {
        if isEnabled { textSelection(.enabled) } else { textSelection(.disabled) }
    }
}
