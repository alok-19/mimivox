import AppKit
import MeetingTranslatorCore
import SwiftUI

struct GlossarySettingsTab: View {
    @EnvironmentObject private var store: TranslatorStore
    @State private var newProfileName = ""
    @State private var pasteText = ""
    @State private var confirmsDelete = false

    private var activeIndex: Int? {
        store.settings.glossaryProfiles.firstIndex { $0.name == store.settings.activeGlossaryProfile }
    }

    private var trimmedNewName: String {
        newProfileName.trimmingCharacters(in: .whitespaces)
    }

    var body: some View {
        Form {
            profiles
            if let index = activeIndex {
                terms(in: index)
            }
            file
        }
        .formStyle(.grouped)
        .onAppear {
            DispatchQueue.main.async { NSApp.keyWindow?.makeFirstResponder(nil) }
        }
        .confirmationDialog(L10n.text("Delete this glossary?"), isPresented: $confirmsDelete) {
            Button(L10n.text("Delete Glossary"), role: .destructive, action: deleteActiveProfile)
        } message: {
            Text(L10n.text("Its terms are removed. Saved meetings aren't changed."))
        }
    }

    // MARK: Profiles

    private var profiles: some View {
        Section {
            Picker(L10n.text("Glossary"), selection: $store.settings.activeGlossaryProfile) {
                Text(L10n.text("None")).tag("")
                ForEach(store.settings.glossaryProfiles) { profile in
                    Text(profile.name).tag(profile.name)
                }
            }
            HStack(spacing: 12) {
                TextField(
                    L10n.text("New glossary"), text: $newProfileName, prompt: Text(L10n.text("Name for a new glossary"))
                )
                .labelsHidden()
                .onSubmit(addProfile)
                Button(L10n.text("Add"), action: addProfile)
                    .disabled(trimmedNewName.isEmpty || profileExists(trimmedNewName))
            }
            if let index = activeIndex {
                Button(
                    String(
                        format: L10n.text("Delete “%@”…"), store.settings.glossaryProfiles[index].name),
                    role: .destructive
                ) {
                    confirmsDelete = true
                }
            }
        } header: {
            Text(L10n.text("Glossaries"))
        } footer: {
            SettingsFooter(
                L10n.text(
                    "The chosen glossary applies to the next meeting. You can also pick one from the source menu on Live."
                ))
        }
    }

    // MARK: Terms

    private func terms(in index: Int) -> some View {
        Section {
            ForEach(store.settings.glossaryProfiles[index].entries.indices, id: \.self) { row in
                HStack(spacing: 8) {
                    TextField(
                        L10n.text("Japanese"), text: entryBinding(index, row, \.source),
                        prompt: Text(L10n.text("Japanese"))
                    )
                    .labelsHidden()
                    Image(systemName: "arrow.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.secondary)
                    TextField(
                        L10n.text("Target"), text: entryBinding(index, row, \.target),
                        prompt: Text(L10n.text("English or corrected Japanese"))
                    )
                    .labelsHidden()
                    Text(kindLabel(store.settings.glossaryProfiles[index].entries[row]))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .frame(width: 72, alignment: .leading)
                    Button {
                        store.settings.glossaryProfiles[index].entries.remove(at: row)
                    } label: {
                        Label(L10n.text("Remove Term"), systemImage: "minus.circle")
                            .labelStyle(.iconOnly)
                    }
                    .buttonStyle(.borderless)
                    .help(L10n.text("Remove this term"))
                }
            }
            Button {
                store.settings.glossaryProfiles[index].entries.append(GlossaryTerm())
            } label: {
                Label(L10n.text("Add Term"), systemImage: "plus")
            }
            DisclosureGroup(L10n.text("Paste a List")) {
                VStack(alignment: .leading, spacing: 8) {
                    TextEditor(text: $pasteText)
                        .font(.body.monospaced())
                        .frame(minHeight: 72)
                        .overlay(RoundedRectangle(cornerRadius: 6).strokeBorder(.separator))
                    HStack {
                        Text(L10n.text("One source,target pair per line, split by a comma or a tab."))
                            .font(.caption)
                            .foregroundStyle(.secondary)
                        Spacer()
                        Button(L10n.text("Import")) {
                            store.settings.glossaryProfiles[index].entries += GlossaryTerm.parse(pasted: pasteText)
                            pasteText = ""
                        }
                        .disabled(GlossaryTerm.parse(pasted: pasteText).isEmpty)
                    }
                }
                .padding(.top, 8)
            }
        } header: {
            Text(String(format: L10n.text("Terms in “%@”"), store.settings.glossaryProfiles[index].name))
        } footer: {
            SettingsFooter(
                L10n.text(
                    "A Japanese target fixes a spelling the speech recognizer gets wrong. A Latin target is a term: it is passed to the recognizer and swapped into the text before translation."
                )
            )
        }
    }

    // MARK: File

    private var file: some View {
        Section {
            HStack(spacing: 12) {
                TextField(
                    L10n.text("Glossary file"), text: $store.settings.glossaryPath,
                    prompt: Text(L10n.text("Path to a JSON or CSV file"))
                )
                .labelsHidden()
                Button(L10n.text("Choose…"), action: pickGlossaryFile)
            }
        } header: {
            Text(L10n.text("Glossary File"))
        } footer: {
            if let count = store.status?.modelReadiness.glossary?.termCount {
                SettingsFooter(
                    count == 0
                        ? L10n.text("No terms apply to captions.")
                        : String(
                            format: L10n.text("%lld terms apply to captions, including the ones built into MimiVox."),
                            Int64(count))
                )
            }
        }
    }

    // MARK: Editing

    private func profileExists(_ name: String) -> Bool {
        store.settings.glossaryProfiles.contains { $0.name == name }
    }

    private func addProfile() {
        guard !trimmedNewName.isEmpty, !profileExists(trimmedNewName) else { return }
        store.settings.glossaryProfiles.append(GlossaryProfile(name: trimmedNewName))
        store.settings.activeGlossaryProfile = trimmedNewName
        newProfileName = ""
    }

    private func deleteActiveProfile() {
        guard let index = activeIndex else { return }
        store.settings.glossaryProfiles.remove(at: index)
        store.settings.activeGlossaryProfile = ""
    }

    private func kindLabel(_ entry: GlossaryTerm) -> String {
        entry.target.isEmpty ? "" : L10n.text(entry.isASRFix ? "Spelling fix" : "Term")
    }

    private func entryBinding(
        _ profile: Int, _ row: Int, _ keyPath: WritableKeyPath<GlossaryTerm, String>
    ) -> Binding<String> {
        Binding(
            get: {
                let profiles = store.settings.glossaryProfiles
                guard profiles.indices.contains(profile), profiles[profile].entries.indices.contains(row) else {
                    return ""
                }
                return profiles[profile].entries[row][keyPath: keyPath]
            },
            set: { newValue in
                guard store.settings.glossaryProfiles.indices.contains(profile),
                    store.settings.glossaryProfiles[profile].entries.indices.contains(row)
                else { return }
                store.settings.glossaryProfiles[profile].entries[row][keyPath: keyPath] = newValue
            }
        )
    }

    private func pickGlossaryFile() {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.allowedContentTypes = [.json, .commaSeparatedText, .tabSeparatedText]
        panel.message = L10n.text("Choose glossary JSON or CSV")
        if panel.runModal() == .OK, let url = panel.url {
            store.settings.glossaryPath = url.path
        }
    }
}
