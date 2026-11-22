import MeetingTranslatorCore
import SwiftUI

enum AppSection: String, CaseIterable, Identifiable {
    case home
    case live
    case meetings
    case learn
    case translate

    var id: String { rawValue }

    var title: String {
        switch self {
        case .home: L10n.text("Home")
        case .live: L10n.text("Live")
        case .meetings: L10n.text("Meetings")
        case .learn: L10n.text("Learn")
        case .translate: L10n.text("Translate")
        }
    }

    var systemImage: String {
        switch self {
        case .home: "house"
        case .live: "waveform"
        case .meetings: "clock.arrow.circlepath"
        case .learn: "rectangle.on.rectangle.angled"
        case .translate: "text.bubble"
        }
    }

    var shortcut: KeyEquivalent {
        switch self {
        case .live: "1"
        case .meetings: "2"
        case .translate: "3"
        case .home: "4"
        case .learn: "5"
        }
    }
}

struct RootView: View {
    @EnvironmentObject private var store: TranslatorStore
    @AppStorage(AppLanguage.defaultsKey) private var appLanguage: AppLanguage = .system
    @Binding var selection: AppSection
    var quickTranslate: QuickTranslateDraft
    var meetingsSearch: MeetingsSearch

    var body: some View {
        NavigationSplitView {
            Sidebar(selection: $selection)
                .navigationSplitViewColumnWidth(min: 180, ideal: 200, max: 260)
        } detail: {
            detail
                .navigationTitle(selection.title)
                .safeAreaInset(edge: .bottom, spacing: 0) {
                    BannerSlot(
                        onLive: selection == .live,
                        openLive: { selection = .home },
                        showsSetupPrompt: selection != .home
                    )
                }
        }
        .environment(\.locale, appLanguage.locale)
    }

    @ViewBuilder
    private var detail: some View {
        switch selection {
        case .home:
            HomeView { selection = $0 }
        case .live:
            LiveView { id in
                store.openMeeting(id: id)
                selection = .meetings
            }
        case .meetings:
            MeetingsView(search: meetingsSearch) { selection = .live }
        case .learn:
            StudyView()
        case .translate:
            TranslateView(draft: quickTranslate)
        }
    }
}

private struct Sidebar: View {
    @Binding var selection: AppSection

    var body: some View {
        List(selection: Binding<AppSection?>(get: { selection }, set: { selection = $0 ?? selection })) {
            ForEach(AppSection.allCases) { section in
                Label(section.title, systemImage: section.systemImage)
                    .tag(section)
            }
        }
        .listStyle(.sidebar)
        .navigationTitle("MimiVox")
        .safeAreaInset(edge: .bottom, spacing: 0) { SidebarFooter() }
    }
}

extension EnvironmentValues {
    /// Picks the Settings tab to show before Settings opens; set by the main window.
    @Entry var selectSettingsTab: (SettingsTab) -> Void = { _ in }
}

/// The sidebar's foot: how MimiVox is doing, and the way into Settings.
private struct SidebarFooter: View {
    @EnvironmentObject private var store: TranslatorStore
    @Environment(\.openSettings) private var openSettings
    @Environment(\.selectSettingsTab) private var selectSettingsTab

    var body: some View {
        let health = store.systemHealth
        HStack(spacing: 8) {
            Button {
                selectSettingsTab(health.settingsTab)
                openSettings()
            } label: {
                HStack(spacing: 8) {
                    Circle().fill(health.color).frame(width: 7, height: 7)
                    Text(health.title)
                        .font(.callout)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                        .truncationMode(.tail)
                }
                .frame(maxWidth: .infinity, alignment: .leading)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .help(health.detail)
            .accessibilityLabel(String(format: L10n.text("Status: %@"), health.title))
            .accessibilityHint(health.detail)

            Button {
                openSettings()
            } label: {
                Label(L10n.text("Settings"), systemImage: "gearshape")
                    .labelStyle(.iconOnly)
                    .font(.body)
            }
            .buttonStyle(.borderless)
            .help(L10n.text("Settings (⌘,)"))
        }
        .padding(.horizontal, 16)
        .padding(.vertical, 12)
    }
}

/// One line on how MimiVox is doing, for the sidebar's foot.
struct SystemHealth: Equatable {
    enum Level { case good, info, attention, problem }

    var level: Level
    var title: String
    var detail: String
    var settingsTab: SettingsTab = .models

    var color: Color {
        switch level {
        case .good: Color(nsColor: .systemGreen)
        case .info: Palette.info
        case .attention: Palette.warning
        case .problem: Palette.error
        }
    }
}

extension TranslatorStore {
    var systemHealth: SystemHealth {
        if helperFailed {
            return SystemHealth(
                level: .problem, title: L10n.text("Helper stopped"),
                detail: L10n.text("The MimiVox helper is not running."), settingsTab: .general
            )
        }
        guard let status else {
            return SystemHealth(
                level: .info, title: L10n.text("Starting…"), detail: L10n.text("Starting the MimiVox helper."),
                settingsTab: .general)
        }
        let models = status.modelReadiness
        switch setup {
        case .needsDownload:
            return SystemHealth(
                level: .attention, title: L10n.text("Setup needed"),
                detail: L10n.text("Download the caption models to start a meeting."))
        case .downloading:
            return SystemHealth(
                level: .info, title: L10n.text("Downloading models…"),
                detail: L10n.text("You can start a meeting when the download finishes."))
        case .loading:
            return SystemHealth(
                level: .info, title: L10n.text("Checking models…"),
                detail: L10n.text("MimiVox is checking the caption models."))
        case .failed:
            return SystemHealth(
                level: .problem, title: L10n.text("Setup failed"),
                detail: L10n.text("The caption models are not ready."))
        case .ready, nil:
            break
        }
        if models.whisper?.ready == false || models.catTranslate?.ready == false {
            return SystemHealth(
                level: .problem, title: L10n.text("Model missing"),
                detail: L10n.text("Open Settings, then Models, to download the missing caption models.")
            )
        }
        if !models.ready {
            return SystemHealth(
                level: .attention, title: L10n.text("Models need attention"),
                detail: L10n.text("Open Settings, then Models, to check the caption models.")
            )
        }
        if models.notesLlm?.ready == false {
            return SystemHealth(
                level: .info, title: L10n.text("Notes model not downloaded"),
                detail: L10n.text("Captions are ready. Download the notes model to use notes, titles, and questions.")
            )
        }
        return SystemHealth(
            level: .good, title: L10n.text("Ready"), detail: L10n.text("The caption models are on this Mac."),
            settingsTab: .models
        )
    }
}
