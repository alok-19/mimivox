import MeetingTranslatorCore
import SwiftUI

struct ExportMenu: View {
    enum Appearance { case toolbar, text }

    var store: TranslatorStore
    var sessionID: String?
    var isEnabled: Bool
    /// Offered only for a saved meeting, where `sessionID` names it and its pins can be sent.
    var canExportPinned: Bool?
    var appearance: Appearance = .toolbar

    var body: some View {
        Menu {
            ForEach(ExportFormat.common) { option in
                Button(L10n.text(option.title)) {
                    Task { await store.export(format: option.format, sessionID: sessionID) }
                }
            }
            if let canExportPinned {
                Divider()
                Button(L10n.text("Pinned Lines Only")) {
                    Task { await store.export(format: "markdown", sessionID: sessionID, pinnedOnly: true) }
                }
                .disabled(!canExportPinned)
            }
            Menu(L10n.text("More Formats")) {
                ForEach(ExportFormat.advanced) { option in
                    Button(L10n.text(option.title)) {
                        Task { await store.export(format: option.format, sessionID: sessionID) }
                    }
                }
            }
        } label: {
            switch appearance {
            case .toolbar: Label(L10n.text("Share"), systemImage: "square.and.arrow.up")
            case .text: Text(L10n.text("Export…")).foregroundStyle(Color.accentColor)
            }
        }
        .menuIndicator(.hidden)
        .disabled(!isEnabled)
        .help(L10n.text("Export the transcript"))
    }
}

private struct ExportFormat: Identifiable {
    var id: String { format }
    var title: String
    var format: String

    static let common = [
        ExportFormat(title: "Markdown", format: "markdown"),
        ExportFormat(title: "Subtitles (SRT)", format: "srt"),
        ExportFormat(title: "Subtitles (VTT)", format: "vtt"),
        ExportFormat(title: "Plain Text", format: "text"),
    ]
    static let advanced = [
        ExportFormat(title: "Spreadsheet (CSV)", format: "csv"),
        ExportFormat(title: "JSON Lines", format: "jsonl"),
    ]
}
