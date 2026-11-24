import MeetingTranslatorCore
import SwiftUI

struct ModelsSettingsTab: View {
    @EnvironmentObject private var store: TranslatorStore
    @State private var confirmsReset = false

    private var nextSteps: [String] {
        store.status?.modelReadiness.nextSteps ?? []
    }

    var body: some View {
        Form {
            setup
            models
            if !nextSteps.isEmpty { steps }
            maintenance
            ModelSourceSection()
            locations
        }
        .formStyle(.grouped)
        .confirmationDialog(L10n.text("Remove all downloaded models?"), isPresented: $confirmsReset) {
            Button(L10n.text("Remove Models"), role: .destructive) { Task { await store.resetModelCache() } }
        } message: {
            Text(
                L10n.text(
                    "Frees about 3.7 GB. Captions keep working until you quit. Next time MimiVox opens, Setup downloads the models again."
                ))
        }
    }

    // MARK: Setup

    private var setup: some View {
        Section {
            if store.setup?.isReady == true {
                Label {
                    Text(L10n.text("The speech models are installed"))
                } icon: {
                    Image(systemName: "checkmark.circle.fill")
                        .foregroundStyle(Color.accentColor)
                }
            } else {
                SetupStateView()
                    .frame(maxWidth: .infinity)
                    .padding(.vertical, 8)
            }
        } header: {
            Text(L10n.text("Setup"))
        }
    }

    // MARK: Models

    private var models: some View {
        Section(L10n.text("Models")) {
            ForEach(ModelKind.allCases) { kind in
                ModelStatusRow(kind: kind, item: kind.readiness(in: store))
            }
        }
    }

    private var steps: some View {
        Section(L10n.text("Next Steps")) {
            ForEach(nextSteps, id: \.self) { step in
                Label(stepText(step), systemImage: "arrow.right.circle")
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func stepText(_ step: String) -> String {
        if step == "loading_models" {
            return L10n.text("Downloading models…")
        }
        for prefix in [
            "Download whisper model:", "Download Japanese speech model:", "Download CAT model:",
        ] where step.hasPrefix(prefix) {
            return L10n.text(prefix) + step.dropFirst(prefix.count)
        }
        return L10n.text(step)
    }

    // MARK: Maintenance

    private var maintenance: some View {
        Section {
            LabeledContent(L10n.text("Check the files")) {
                Button(L10n.text("Verify")) { Task { await store.modelCommand("verify") } }
            }
            LabeledContent(L10n.text("Get newer versions")) {
                Button(L10n.text("Update")) { Task { await store.modelCommand("update") } }
            }
            if store.status?.modelReadiness.notesLlm?.ready != true {
                LabeledContent(L10n.text("Meeting notes model")) {
                    Button(L10n.text("Download")) { Task { await store.downloadNotesModel() } }
                }
            }
            LabeledContent(L10n.text("Downloaded models")) {
                Button(L10n.text("Remove…"), role: .destructive) { confirmsReset = true }
            }
        } header: {
            Text(L10n.text("Maintenance"))
        } footer: {
            SettingsFooter(
                L10n.text(
                    "Meeting notes need their own model. Removing the models frees disk space, and MimiVox downloads them again when it needs them."
                )
            )
        }
        .disabled(store.isBusy)
    }

    private var locations: some View {
        Section {
            DisclosureGroup(L10n.text("Model Locations")) {
                TextField(
                    L10n.text("Speech model"), text: $store.settings.modelPaths.whisper,
                    prompt: Text(L10n.text("Folder or model ID")))
                TextField(
                    L10n.text("Translation model"), text: $store.settings.modelPaths.catTranslate,
                    prompt: Text(L10n.text("Folder or model ID")))
            }
        }
    }
}

private enum ModelKind: CaseIterable, Identifiable {
    case whisper, asrJa, catTranslate, notesLlm

    var id: Self { self }

    var title: String {
        switch self {
        case .whisper: L10n.text("Speech recognition")
        case .asrJa: L10n.text("Japanese speech recognition")
        case .catTranslate: L10n.text("Translation")
        case .notesLlm: L10n.text("Meeting notes")
        }
    }

    @MainActor
    func readiness(in store: TranslatorStore) -> ModelReadinessItem? {
        let readiness = store.status?.modelReadiness
        return switch self {
        case .whisper: readiness?.whisper
        case .asrJa: readiness?.asrJa
        case .catTranslate: readiness?.catTranslate
        case .notesLlm: readiness?.notesLlm
        }
    }
}

private struct ModelStatusRow: View {
    var kind: ModelKind
    var item: ModelReadinessItem?

    private var detail: String? {
        [item?.displayName, item?.modelID].compactMap { $0 }.first { !$0.isEmpty }
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                Text(kind.title)
                if let detail {
                    Text(detail)
                        .font(.footnote)
                        .foregroundStyle(.secondary)
                }
            }
            Spacer(minLength: 0)
            status
        }
        .textSelection(.enabled)
        .help([item?.message, item?.path].compactMap { $0 }.filter { !$0.isEmpty }.joined(separator: "\n"))
    }

    @ViewBuilder
    private var status: some View {
        switch item?.ready {
        case true:
            Label(L10n.text("Installed"), systemImage: "checkmark.circle")
                .foregroundStyle(.secondary)
        case false:
            Label {
                Text(L10n.text("Not installed"))
            } icon: {
                Image(systemName: "exclamationmark.circle")
                    .foregroundStyle(Palette.warning)
            }
            .foregroundStyle(.secondary)
        default:
            Text(L10n.text("Status unavailable"))
                .foregroundStyle(.secondary)
        }
    }
}
