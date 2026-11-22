import MeetingTranslatorCore
import SwiftUI

struct MeetingAppsSettingsTab: View {
    @EnvironmentObject private var store: TranslatorStore
    @State private var customBundleID = ""

    private static let columnGap: CGFloat = 12
    private static let offerWidth: CGFloat = 48
    private static let captureWidth: CGFloat = 136

    private var customKeys: [String] {
        store.autostartPrefs.rules.keys.filter { MeetingApp.app(id: $0) == nil }.sorted()
    }

    private var trimmedBundleID: String {
        customBundleID.trimmingCharacters(in: .whitespaces)
    }

    var body: some View {
        Form {
            Section {
                Toggle(L10n.text("Offer captions when a meeting starts"), isOn: $store.autostartPrefs.detectionEnabled)
            } footer: {
                SettingsFooter(
                    L10n.text(
                        "MimiVox watches which apps are playing audio (names only, never the sound) and asks “Start captions for <App>?” after a few seconds of call audio."
                    )
                )
            }

            Section {
                ForEach(MeetingApp.catalog) { app in
                    row(offer: offerBinding(app)) {
                        Text(app.name)
                    } capture: {
                        capturePicker(for: app.id)
                    }
                }
                ForEach(customKeys, id: \.self) { key in
                    row(offer: nil) {
                        Text(key)
                            .font(.callout.monospaced())
                            .lineLimit(1)
                            .truncationMode(.middle)
                    } capture: {
                        capturePicker(for: key)
                    }
                }
                HStack(spacing: 12) {
                    TextField(
                        L10n.text("Add an app"), text: $customBundleID,
                        prompt: Text(L10n.text("Bundle ID, such as com.spotify.client"))
                    )
                    .labelsHidden()
                    Button(L10n.text("Never Capture")) {
                        guard !trimmedBundleID.isEmpty else { return }
                        store.autostartPrefs.rules[trimmedBundleID] = .exclude
                        customBundleID = ""
                    }
                    .disabled(trimmedBundleID.isEmpty)
                }
            } header: {
                columnHeader
            } footer: {
                SettingsFooter(
                    store.usesNativeSystemAudio
                        ? L10n.text(
                            "Default captures all Mac audio except apps marked Never capture. If any app is set to Only this, just those apps are captured. A prompt always captures only the app it names."
                        )
                        : L10n.text(
                            "Per-app capture needs System Audio set to Built-in. With a BlackHole or Loopback input every app is captured and no prompts are shown."
                        )
                )
            }
        }
        .formStyle(.grouped)
    }

    private var columnHeader: some View {
        HStack(spacing: Self.columnGap) {
            Text(L10n.text("App"))
                .frame(maxWidth: .infinity, alignment: .leading)
            Text(L10n.text("Offer"))
                .frame(width: Self.offerWidth)
            Text(L10n.text("Capture"))
                .frame(width: Self.captureWidth, alignment: .trailing)
        }
        .font(.subheadline)
        .foregroundStyle(.secondary)
    }

    private func row<Name: View, Capture: View>(
        offer: Binding<Bool>?,
        @ViewBuilder name: () -> Name,
        @ViewBuilder capture: () -> Capture
    ) -> some View {
        HStack(spacing: Self.columnGap) {
            name()
                .frame(maxWidth: .infinity, alignment: .leading)
            Group {
                if let offer {
                    Toggle(L10n.text("Offer"), isOn: offer)
                        .labelsHidden()
                        .toggleStyle(.checkbox)
                        .disabled(!store.autostartPrefs.detectionEnabled)
                }
            }
            .frame(width: Self.offerWidth)
            capture()
                .frame(width: Self.captureWidth, alignment: .trailing)
        }
    }

    private func capturePicker(for key: String) -> some View {
        Picker(L10n.text("Capture"), selection: ruleBinding(key)) {
            Text(L10n.text("Default")).tag(AppCaptureRule?.none)
            Text(L10n.text("Only this")).tag(AppCaptureRule?.some(.include))
            Text(L10n.text("Never capture")).tag(AppCaptureRule?.some(.exclude))
        }
        .labelsHidden()
    }

    private func offerBinding(_ app: MeetingApp) -> Binding<Bool> {
        Binding(
            get: { !store.autostartPrefs.mutedPromptApps.contains(app.id) },
            set: { offer in
                if offer {
                    store.autostartPrefs.mutedPromptApps.remove(app.id)
                } else {
                    store.autostartPrefs.mutedPromptApps.insert(app.id)
                }
            }
        )
    }

    private func ruleBinding(_ key: String) -> Binding<AppCaptureRule?> {
        Binding(
            get: { store.autostartPrefs.rules[key] },
            set: { store.autostartPrefs.rules[key] = $0 }
        )
    }
}
