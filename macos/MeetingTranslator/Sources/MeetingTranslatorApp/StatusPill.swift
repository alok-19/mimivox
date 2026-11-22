import MeetingTranslatorCore
import SwiftUI

/// The status pill, in the toolbar only while it has something to say. An empty toolbar item still takes up room,
/// and at rest the window subtitle speaks.
struct StatusToolbarItem: ToolbarContent {
    @ObservedObject var store: TranslatorStore

    var body: some ToolbarContent {
        if store.liveStatus.showsToolbarPill {
            ToolbarItem(placement: .primaryAction) {
                StatusPill(status: store.liveStatus, detail: store.liveStatusDetail, startedAt: store.live.startedAt)
            }
        }
    }
}

struct StatusPill: View {
    var status: LiveStatus
    var detail: String
    var startedAt: Date?

    private var localizedStatusTitle: String {
        switch status {
        case .starting: L10n.text("Starting…")
        case .setupNeeded: L10n.text("Setup")
        case .idle: L10n.text("Idle")
        case .live: L10n.text("Live")
        case .paused: L10n.text("Paused")
        case .stopping: L10n.text("Stopping…")
        case .catchingUp: L10n.text("Catching up")
        case .behind(let seconds): String(format: L10n.text("Behind %d s"), seconds)
        case .failed: L10n.text("Failed")
        }
    }

    var body: some View {
        HStack(spacing: 8) {
            if status == .failed {
                Image(systemName: "exclamationmark.triangle.fill")
                    .font(.caption)
                    .foregroundStyle(Palette.error)
            } else {
                Circle()
                    .fill(Palette.dot(for: status))
                    .frame(width: 8, height: 8)
            }
            Text(localizedStatusTitle)
                .font(.callout.weight(.medium))
                .lineLimit(1)
            if status == .live, let startedAt {
                TimelineView(.periodic(from: startedAt, by: 1)) { context in
                    Text(MeetingFormat.timecode(context.date.timeIntervalSince(startedAt)))
                        .font(.callout.monospacedDigit())
                        .foregroundStyle(.secondary)
                }
            }
        }
        .padding(.horizontal, 8)
        .help(detail)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(L10n.text("Status"))
        .accessibilityValue(localizedStatusTitle)
        .accessibilityIdentifier("status-pill")
    }
}
