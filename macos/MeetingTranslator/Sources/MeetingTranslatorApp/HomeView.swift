import MeetingTranslatorCore
import SwiftUI

struct HomeView: View {
    @EnvironmentObject private var store: TranslatorStore
    @Environment(\.openSettings) private var openSettings
    @Environment(\.selectSettingsTab) private var selectSettingsTab
    let openSection: (AppSection) -> Void

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 26) {
                readiness
                VStack(spacing: 0) {
                    destination(.live, detail: L10n.text("View live transcription and translation."))
                    Divider()
                    destination(.meetings, detail: L10n.text("Review transcripts, notes, and glossary terms."))
                    Divider()
                    destination(.learn, detail: L10n.text("Study terms from your meetings."))
                    Divider()
                    destination(.translate, detail: L10n.text("Translate text between English and Japanese."))
                }
            }
            .frame(maxWidth: 620, alignment: .leading)
            .padding(28)
            .frame(maxWidth: .infinity)
        }
        .background(Color(nsColor: .textBackgroundColor))
    }

    private var readiness: some View {
        let health = store.systemHealth
        return VStack(alignment: .leading, spacing: 12) {
            Text(L10n.text("Meeting readiness"))
                .font(.title2.weight(.semibold))
                .accessibilityAddTraits(.isHeader)
            GroupBox {
                VStack(alignment: .leading, spacing: 12) {
                    Label {
                        VStack(alignment: .leading, spacing: 4) {
                            Text(health.title).font(.headline)
                            Text(health.detail)
                                .font(.callout)
                                .foregroundStyle(.secondary)
                                .fixedSize(horizontal: false, vertical: true)
                        }
                    } icon: {
                        Image(systemName: health.systemImage)
                            .foregroundStyle(health.color)
                            .accessibilityHidden(true)
                    }
                    if store.showsSetupCard {
                        SetupStateView()
                    } else if !store.helperFailed {
                        Button(L10n.text("Model Settings…")) {
                            selectSettingsTab(.models)
                            openSettings()
                        }
                    }
                }
                .padding(8)
                .frame(maxWidth: .infinity, alignment: .leading)
            }
        }
    }

    private func destination(_ section: AppSection, detail: String) -> some View {
        Button {
            openSection(section)
        } label: {
            HStack(spacing: 14) {
                Image(systemName: section.systemImage)
                    .font(.title3)
                    .foregroundStyle(section == .live ? Color.accentColor : Color.secondary)
                    .frame(width: 24)
                    .accessibilityHidden(true)
                VStack(alignment: .leading, spacing: 3) {
                    Text(section.title).font(.headline)
                    Text(detail).font(.callout).foregroundStyle(.secondary)
                }
                Spacer()
                Image(systemName: "chevron.right")
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(.secondary)
                    .accessibilityHidden(true)
            }
            .padding(.vertical, 10)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .accessibilityLabel(section.title)
        .accessibilityHint(detail)
    }
}

extension SystemHealth {
    fileprivate var systemImage: String {
        switch level {
        case .good: "checkmark.circle.fill"
        case .info: "info.circle"
        case .attention: "exclamationmark.triangle"
        case .problem: "exclamationmark.octagon"
        }
    }
}
