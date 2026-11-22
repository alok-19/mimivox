import MeetingTranslatorCore
import SwiftUI

enum SettingsTab: String, CaseIterable, Identifiable {
    case general
    case audio
    case meetingApps = "meeting-apps"
    case glossary
    case models
    case privacy

    var id: String { rawValue }

    var title: String {
        switch self {
        case .general: L10n.text("General")
        case .audio: L10n.text("Audio")
        case .meetingApps: L10n.text("Meeting Apps")
        case .glossary: L10n.text("Glossary")
        case .models: L10n.text("Models")
        case .privacy: L10n.text("Privacy")
        }
    }

    var systemImage: String {
        switch self {
        case .general: "gearshape"
        case .audio: "waveform"
        case .meetingApps: "person.2.wave.2"
        case .glossary: "book.closed"
        case .models: "shippingbox"
        case .privacy: "lock.shield"
        }
    }

    var height: CGFloat {
        switch self {
        case .general: 408
        case .audio: 160
        case .meetingApps: 560
        case .glossary: 560
        case .models: 680
        case .privacy: 304
        }
    }
}

struct SettingsRoot: View {
    @ObservedObject var model: AppModel

    var body: some View {
        TabView(selection: $model.settingsTab) {
            ForEach(SettingsTab.allCases) { tab in
                SettingsPage(tab: tab)
                    .tabItem { Label(tab.title, systemImage: tab.systemImage) }
                    .tag(tab)
            }
        }
        .environmentObject(model.store)
        .preferredColorScheme(AppearanceOverride.colorScheme)
    }
}

struct SettingsPage: View {
    @EnvironmentObject private var store: TranslatorStore
    var tab: SettingsTab

    static let width: CGFloat = 560

    var body: some View {
        VStack(spacing: 0) {
            if let notice = store.notice, notice.concernsSettings, notice.kind == .error || notice.kind == .warning {
                BannerView(banner: Banner(notice))
                    .padding([.horizontal, .top], 16)
            }
            form
        }
        .frame(width: Self.width, height: tab.height)
    }

    @ViewBuilder
    private var form: some View {
        switch tab {
        case .general: GeneralSettingsTab()
        case .audio: AudioSettingsTab()
        case .meetingApps: MeetingAppsSettingsTab()
        case .glossary: GlossarySettingsTab()
        case .models: ModelsSettingsTab()
        case .privacy: PrivacySettingsTab()
        }
    }
}

/// A grouped Form would otherwise draw it at full strength and right-align the lines of a wrapped footer.
struct SettingsFooter: View {
    private static let groupInset: CGFloat = 10

    private let text: String

    init(_ text: String) {
        self.text = text
    }

    var body: some View {
        Text(text)
            .font(.footnote)
            .foregroundStyle(.secondary)
            .multilineTextAlignment(.leading)
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(.horizontal, Self.groupInset)
    }
}

// MARK: General

private struct GeneralSettingsTab: View {
    @EnvironmentObject private var store: TranslatorStore
    @AppStorage(AppLanguage.defaultsKey) private var appLanguage = AppLanguage.system.rawValue

    var body: some View {
        Form {
            Picker(L10n.text("App language"), selection: $appLanguage) {
                ForEach(AppLanguage.allCases) { language in
                    Text(language.title).tag(language.rawValue)
                }
            }

            Section {
                Picker(L10n.text("Language"), selection: $store.settings.languagePair) {
                    ForEach(LanguagePair.supported) { pair in
                        Text(pair.label).tag(pair)
                    }
                }
                Picker(L10n.text("Speed"), selection: $store.settings.latencyQuality) {
                    Text(L10n.text("Fast")).tag("fast")
                    Text(L10n.text("Balanced")).tag("balanced")
                    Text(L10n.text("Quality")).tag("quality")
                }
                .pickerStyle(.segmented)
                Picker(L10n.text("When behind"), selection: $store.settings.catchUpMode) {
                    ForEach(CatchUpMode.allCases) { mode in
                        Text(L10n.text(mode.title)).tag(mode.rawValue)
                    }
                }
            } header: {
                Text(L10n.text("Captions"))
            } footer: {
                SettingsFooter(
                    store.settings.languagePair.needsTranslationModel
                        ? L10n.text(
                            "This pair needs the optional translation model, under Models. Glossary spelling fixes apply to Japanese only. Terms work for every language."
                        )
                        : L10n.text(
                            "When captions fall behind the speaker, skipped speech is kept so you can recover it from Meetings."
                        )
                )
            }

            Section {
                Toggle(L10n.text("Learning mode"), isOn: $store.settings.learningMode)
            } header: {
                Text(L10n.text("Learning"))
            } footer: {
                SettingsFooter(
                    L10n.text(
                        "Furigana over kanji, a reading and meaning for each word on hover, and a word list in Meetings. Runs on this Mac."
                    )
                )
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: Audio

private struct AudioSettingsTab: View {
    @EnvironmentObject private var store: TranslatorStore

    /// Core Audio process taps (built-in system audio capture) need macOS 14.2.
    private static var nativeSystemAudioAvailable: Bool {
        if #available(macOS 14.2, *) { true } else { false }
    }

    var body: some View {
        Form {
            Section {
                Picker(L10n.text("Microphone"), selection: $store.settings.microphoneDevice) {
                    Text(L10n.text("Default")).tag("")
                    ForEach(store.devices) { device in
                        Text(device.name).tag(device.id)
                    }
                }
                Picker(L10n.text("System audio"), selection: $store.settings.systemAudioDevice) {
                    Text(
                        L10n.text(
                            Self.nativeSystemAudioAvailable ? "Built-in (all Mac audio)" : "Auto (BlackHole…)")
                    )
                    .tag("")
                    ForEach(store.devices) { device in
                        Text(device.name).tag(device.id)
                    }
                }
            } footer: {
                SettingsFooter(
                    Self.nativeSystemAudioAvailable
                        ? L10n.text(
                            "Built-in captures everything your Mac plays. macOS asks for System Audio Recording permission once. Pick a BlackHole or Loopback input to use that instead."
                        )
                        : L10n.text(
                            "Needs BlackHole or Loopback as the system output and input. Built-in capture needs macOS 14.2."
                        )
                )
            }
        }
        .formStyle(.grouped)
    }
}

// MARK: Privacy

private struct PrivacySettingsTab: View {
    @EnvironmentObject private var store: TranslatorStore
    @State private var pendingRetention: String?
    @State private var confirmsDeleteAll = false

    private var retention: Binding<String> {
        Binding(
            get: { store.settings.retentionPolicy },
            set: { new in
                let current = RetentionPolicy(serverValue: store.settings.retentionPolicy)
                if let next = RetentionPolicy(rawValue: new), next.keepsLess(than: current) {
                    pendingRetention = new
                } else {
                    store.settings.retentionPolicy = new
                }
            }
        )
    }

    var body: some View {
        let privacy = store.privacyStatus
        Form {
            Section {
                Label(
                    L10n.text(privacy.indicatorText),
                    systemImage: privacy.offlineLocked ? "lock.shield.fill" : "lock.shield"
                )
                .help(
                    privacy.offlineLocked
                        ? L10n.text("Offline mode is on (MIMIVOX_OFFLINE=1), so model downloads are locked off.")
                        : L10n.text("Model downloads are allowed. Transcripts and audio still stay on this Mac.")
                )
                .accessibilityIdentifier("privacy-indicator")
            } footer: {
                SettingsFooter(L10n.text("Audio and transcripts never leave this Mac."))
            }

            Section {
                Picker(L10n.text("Keep transcripts"), selection: retention) {
                    ForEach(RetentionPolicy.allCases, id: \.rawValue) { policy in
                        Text(L10n.text(policy.label)).tag(policy.rawValue)
                    }
                }
            } footer: {
                SettingsFooter(
                    L10n.text("Expired meetings are deleted when the setting changes, on launch, and every hour."))
            }

            Section {
                LabeledContent(L10n.text("Saved history")) {
                    Button(L10n.text("Delete All…"), role: .destructive) { confirmsDeleteAll = true }
                }
            } footer: {
                SettingsFooter(L10n.text("Removes every saved meeting, export and log."))
            }
        }
        .formStyle(.grouped)
        .confirmationDialog(
            L10n.text("Delete older meetings now?"),
            isPresented: Binding(get: { pendingRetention != nil }, set: { if !$0 { pendingRetention = nil } })
        ) {
            Button(L10n.text("Change and Delete"), role: .destructive) {
                if let pendingRetention { store.settings.retentionPolicy = pendingRetention }
                pendingRetention = nil
            }
        } message: {
            Text(L10n.text("Saved meetings older than the new limit are deleted right away. This can't be undone."))
        }
        .confirmationDialog(L10n.text("Delete all saved meetings, exports and logs?"), isPresented: $confirmsDeleteAll)
        {
            Button(L10n.text("Delete All History"), role: .destructive) {
                Task { await store.deleteAllHistory() }
            }
        } message: {
            Text(L10n.text("This can't be undone."))
        }
    }
}
