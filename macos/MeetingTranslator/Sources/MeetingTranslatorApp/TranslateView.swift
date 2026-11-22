import AVFoundation
import AppKit
import MeetingTranslatorCore
import SwiftUI

struct TranslateView: View {
    @EnvironmentObject private var store: TranslatorStore
    @Bindable var draft: QuickTranslateDraft
    @State private var synthesizer = AVSpeechSynthesizer()
    @State private var mic = MicDictation()

    private static let stackWidth: CGFloat = 640
    private static let inset: CGFloat = 20

    private var isJapaneseToEnglish: Bool { draft.direction == .jaToEn }

    var body: some View {
        GeometryReader { proxy in
            if proxy.size.width >= Self.stackWidth {
                HStack(spacing: 0) {
                    sourcePane
                    Divider()
                    resultPane
                }
            } else {
                VStack(spacing: 0) {
                    sourcePane
                    Divider()
                    resultPane
                }
            }
        }
        .navigationTitle(L10n.text("Translate"))
        .onDisappear { mic.cancel() }
        .toolbar {
            StatusToolbarItem(store: store)
            ToolbarItemGroup(placement: .primaryAction) {
                directionPicker
                swapButton
            }
        }
    }

    private var directionPicker: some View {
        Menu {
            Picker(L10n.text("Direction"), selection: $draft.direction) {
                Text(L10n.text("Japanese to English")).tag(TranslationDirection.jaToEn)
                Text(L10n.text("English to Japanese")).tag(TranslationDirection.enToJa)
            }
            .pickerStyle(.inline)
        } label: {
            Text(L10n.text(isJapaneseToEnglish ? "Japanese to English" : "English to Japanese"))
        }
        .help(L10n.text("Translate Japanese to English or English to Japanese"))
    }

    private var swapButton: some View {
        Button {
            draft.swapDirection()
        } label: {
            Label(L10n.text("Swap Languages"), systemImage: "arrow.left.arrow.right")
        }
        .toolbarIcon()
        .help(L10n.text("Swap the languages, and the texts when there is a translation"))
    }

    // MARK: Source

    private var sourcePane: some View {
        VStack(alignment: .leading, spacing: 0) {
            paneLabel(L10n.text(isJapaneseToEnglish ? "Japanese" : "English"))
            TextEditor(text: $draft.input)
                .font(.system(size: 20))
                .scrollContentBackground(.hidden)
                .overlay(alignment: .topLeading) {
                    if draft.input.isEmpty {
                        Text(L10n.text(isJapaneseToEnglish ? "Type or paste Japanese" : "Type your reply in English"))
                            .font(.system(size: 20))
                            .foregroundStyle(.tertiary)
                            .padding(.leading, 5)
                            .allowsHitTesting(false)
                    }
                }
                .padding(.horizontal, Self.inset - 5)
            sourceBar
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .textBackgroundColor))
    }

    private var sourceBar: some View {
        HStack(spacing: 8) {
            iconButton(
                L10n.text("Paste"), systemImage: "doc.on.clipboard",
                help: L10n.text("Replace the text with what is on the clipboard")
            ) {
                draft.input = NSPasteboard.general.string(forType: .string) ?? ""
            }
            dictationControl
            if mic.phase == .idle, isJapaneseToEnglish, store.latestJapaneseText != nil {
                iconButton(
                    L10n.text("Use Latest Meeting Line"), systemImage: "captions.bubble",
                    help: L10n.text("Fill in the last line captioned on Live")
                ) {
                    useLatestMeetingLine()
                }
            }
            Spacer(minLength: 0)
            if draft.isTranslating {
                ProgressView().controlSize(.small)
            } else if !draft.trimmedInput.isEmpty {
                Button(L10n.text("Translate")) {
                    Task { await draft.translate(using: store) }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.return, modifiers: .command)
                .help(L10n.text("Translate (⌘↩)"))
            }
        }
        .paneBar(inset: Self.inset)
    }

    // MARK: Dictation

    @ViewBuilder
    private var dictationControl: some View {
        switch mic.phase {
        case .idle:
            iconButton(
                L10n.text("Dictate"), systemImage: "mic",
                help: store.isSessionActive
                    ? L10n.text("Dictation is off while Live is captioning")
                    : L10n.text("Speak Japanese or English to fill this in (⇧⌘D)")
            ) {
                Task { await mic.start { Task { await finishDictation() } } }
            }
            .keyboardShortcut("d", modifiers: [.command, .shift])
            .disabled(store.isSessionActive)
            if let error = mic.error {
                Text(error)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .lineLimit(1)
                    .truncationMode(.tail)
                    .help(error)
            }
        case .recording:
            Button {
                Task { await finishDictation() }
            } label: {
                Label(L10n.text("Stop Dictation"), systemImage: "stop.circle.fill")
                    .labelStyle(.iconOnly)
                    .imageScale(.large)
                    .foregroundStyle(Palette.live)
            }
            .buttonStyle(.borderless)
            .keyboardShortcut("d", modifiers: [.command, .shift])
            .help(L10n.text("Stop and transcribe (⇧⌘D)"))
            LevelMeter(level: mic.level)
            Text(L10n.text("Listening…"))
                .font(.callout)
                .foregroundStyle(.secondary)
        case .transcribing:
            ProgressView().controlSize(.small)
            Text(L10n.text("Transcribing…"))
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }

    /// Sends the clip, puts the words in the source pane in the language that was
    /// spoken, and translates them.
    private func finishDictation() async {
        guard let clip = mic.finish() else { return }
        let result = await store.dictate(samples: clip, hint: draft.direction)
        mic.done()
        guard let result else { return }
        guard !result.text.isEmpty else {
            mic.error = L10n.text("Didn't catch any words. Try again a little closer to the mic.")
            return
        }
        let spoken: TranslationDirection = result.language == "en" ? .enToJa : .jaToEn
        draft.direction = spoken
        let separator = spoken == .enToJa ? " " : ""
        draft.input = draft.trimmedInput.isEmpty ? result.text : draft.trimmedInput + separator + result.text
        await draft.translate(using: store)
    }

    // MARK: Result

    private var resultPane: some View {
        VStack(alignment: .leading, spacing: 0) {
            paneLabel(L10n.text(isJapaneseToEnglish ? "English" : "Japanese"))
            ScrollView {
                VStack(alignment: .leading, spacing: 16) {
                    if draft.output.isEmpty {
                        Text(L10n.text("The translation appears here"))
                            .foregroundStyle(.tertiary)
                    } else {
                        Text(draft.output)
                            .textSelection(.enabled)
                    }
                    if !isJapaneseToEnglish, !draft.backTranslation.isEmpty {
                        backTranslation
                    }
                }
                .font(.system(size: 20))
                .frame(maxWidth: .infinity, alignment: .topLeading)
                .padding(.horizontal, Self.inset)
            }
            resultBar
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .windowBackgroundColor))
    }

    private var backTranslation: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(L10n.text("Back-translation"))
                .font(.caption.weight(.semibold))
                .foregroundStyle(.secondary)
            Text(draft.backTranslation)
                .font(.callout)
                .foregroundStyle(.secondary)
                .textSelection(.enabled)
        }
    }

    private var resultBar: some View {
        HStack(spacing: 8) {
            Spacer(minLength: 0)
            if !draft.output.isEmpty {
                if !isJapaneseToEnglish {
                    iconButton(
                        L10n.text("Speak"), systemImage: "speaker.wave.2",
                        help: L10n.text("Read the Japanese aloud")
                    ) {
                        speak(draft.output)
                    }
                }
                iconButton(L10n.text("Copy"), systemImage: "doc.on.doc", help: L10n.text("Copy the translation")) {
                    copyToPasteboard(draft.output)
                }
            }
        }
        .paneBar(inset: Self.inset)
    }

    private func paneLabel(_ language: String) -> some View {
        Text(language)
            .font(.callout.weight(.medium))
            .foregroundStyle(.secondary)
            .padding(.horizontal, Self.inset)
            .padding(.top, 16)
            .padding(.bottom, 8)
    }

    private func iconButton(_ title: String, systemImage: String, help: String, action: @escaping () -> Void)
        -> some View
    {
        Button(action: action) {
            Label(title, systemImage: systemImage)
                .labelStyle(.iconOnly)
                .imageScale(.large)
        }
        .buttonStyle(.borderless)
        .help(help)
    }

    private func useLatestMeetingLine() {
        guard let japanese = store.latestJapaneseText else { return }
        draft.input = japanese
        draft.output = store.latestEnglishText ?? ""
    }

    private func speak(_ text: String) {
        synthesizer.stopSpeaking(at: .immediate)
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice = AVSpeechSynthesisVoice(language: "ja-JP")
        synthesizer.speak(utterance)
    }
}

/// Five bars that rise with the mic level while dictating.
private struct LevelMeter: View {
    var level: Double

    var body: some View {
        HStack(alignment: .center, spacing: 2) {
            ForEach(0..<5, id: \.self) { bar in
                let threshold = Double(bar) / 5
                Capsule()
                    .fill(level > threshold ? Palette.live : Color.secondary.opacity(0.3))
                    .frame(width: 3, height: 6 + CGFloat(bar) * 3)
            }
        }
        .animation(.easeOut(duration: 0.08), value: level)
        .accessibilityHidden(true)
    }
}

extension View {
    fileprivate func paneBar(inset: CGFloat) -> some View {
        frame(height: 32)
            .padding(.horizontal, inset)
            .padding(.vertical, 16)
    }
}
