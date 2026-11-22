import AppKit
import MeetingTranslatorCore
import SwiftUI

/// Stopping the helper can take a few seconds. Quit runs it off the main thread
/// and shows this panel meanwhile, so the app does not look hung.
@MainActor
enum QuitProgress {
    /// Shown only when stopping takes longer than this, so a quick quit does not flash it.
    private static let showDelay: Duration = .milliseconds(250)

    /// `endingMeeting` ends the live meeting first so the server saves its transcript.
    static func terminate(_ app: NSApplication, stopping store: TranslatorStore, endingMeeting: Bool) {
        var panel: NSPanel?
        let reveal = Task {
            try? await Task.sleep(for: showDelay)
            guard !Task.isCancelled else { return }
            panel = makePanel(endingMeeting: endingMeeting)
            panel?.orderFrontRegardless()
        }
        Task {
            if endingMeeting {
                await store.stopSession()
            }
            await store.stopHelperOffMain()
            reveal.cancel()
            panel?.orderOut(nil)
            app.reply(toApplicationShouldTerminate: true)
        }
    }

    private static func makePanel(endingMeeting: Bool) -> NSPanel {
        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 280, height: 84),
            styleMask: [.titled, .fullSizeContentView, .nonactivatingPanel],
            backing: .buffered,
            defer: false
        )
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isMovableByWindowBackground = true
        panel.level = .floating
        for button in [NSWindow.ButtonType.closeButton, .miniaturizeButton, .zoomButton] {
            panel.standardWindowButton(button)?.isHidden = true
        }
        panel.contentView = NSHostingView(rootView: QuittingView(endingMeeting: endingMeeting))
        panel.center()
        return panel
    }
}

private struct QuittingView: View {
    let endingMeeting: Bool

    var body: some View {
        HStack(spacing: 14) {
            ProgressView().controlSize(.regular)
            VStack(alignment: .leading, spacing: 2) {
                Text(L10n.text("Quitting MimiVox…")).font(.headline)
                Text(
                    L10n.text(
                        endingMeeting
                            ? "Saving the meeting and stopping the models."
                            : "Stopping the speech and translation models."
                    )
                )
                .font(.callout)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(.horizontal, 20)
        .frame(width: 280, height: 84, alignment: .leading)
        .accessibilityElement(children: .combine)
    }
}

/// The confirmations Cmd+Q shows, as plain AppKit alerts so they work with no window open.
@MainActor
enum QuitAlert {
    enum MeetingChoice { case endAndQuit, endOnly, cancel }

    static func askAboutMeeting(paused: Bool) -> MeetingChoice {
        let alert = NSAlert()
        alert.messageText = L10n.text(paused ? "A meeting is paused" : "A meeting is in progress")
        alert.informativeText = L10n.text(
            "Ending the meeting saves its transcript. You can end it and quit, or end it and keep MimiVox open."
        )
        alert.addButton(withTitle: L10n.text("End Meeting & Quit"))
        alert.addButton(withTitle: L10n.text("End Meeting"))
        alert.addButton(withTitle: L10n.text("Cancel"))
        NSApp.activate(ignoringOtherApps: true)
        switch alert.runModal() {
        case .alertFirstButtonReturn: return .endAndQuit
        case .alertSecondButtonReturn: return .endOnly
        default: return .cancel
        }
    }

    /// True to quit. Ticking "Don't ask again" skips this prompt from then on.
    static func confirmIdleQuit() -> Bool {
        let alert = NSAlert()
        alert.messageText = L10n.text("Quit MimiVox?")
        alert.informativeText = L10n.text("Captions stop until you open MimiVox again.")
        alert.addButton(withTitle: L10n.text("Quit"))
        alert.addButton(withTitle: L10n.text("Cancel"))
        alert.showsSuppressionButton = true
        alert.suppressionButton?.title = L10n.text("Don't ask again")
        NSApp.activate(ignoringOtherApps: true)
        let quit = alert.runModal() == .alertFirstButtonReturn
        if quit, alert.suppressionButton?.state == .on {
            UserDefaults.standard.set(true, forKey: QuitConfirmation.idleSuppressedKey)
        }
        return quit
    }
}
