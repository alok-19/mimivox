import MeetingTranslatorCore
import SwiftUI

struct SpeakerRenameButton<Label: View>: View {
    var tag: SpeakerTag
    var onRename: ((String) -> Void)?
    @ViewBuilder var label: Label

    @State private var isEditing = false
    @State private var draft = ""

    var body: some View {
        if let onRename {
            Button {
                draft = tag.label
                isEditing = true
            } label: {
                label
            }
            .buttonStyle(.plain)
            .help(L10n.text("Rename speaker"))
            .popover(isPresented: $isEditing) {
                HStack {
                    TextField(L10n.text("Speaker name"), text: $draft)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 160)
                        .onSubmit { commit(onRename) }
                    Button(L10n.text("Save")) { commit(onRename) }
                }
                .padding(12)
            }
        } else {
            label
        }
    }

    private func commit(_ onRename: (String) -> Void) {
        isEditing = false
        onRename(draft)
    }
}
