import MeetingTranslatorCore
import SwiftUI

struct LiveView: View {
    @EnvironmentObject private var store: TranslatorStore
    var openMeeting: (String) -> Void
    @AppStorage("live.showsInspector") private var showsInspector = false

    var body: some View {
        Group {
            if store.showsSetupCard {
                SetupCard()
            } else {
                TranscriptColumn(openMeeting: openMeeting)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .background(Color(nsColor: .textBackgroundColor))
        .navigationTitle(L10n.text("Live"))
        .navigationSubtitle(store.liveSubtitle)
        .toolbar { LiveToolbar(store: store, showsInspector: $showsInspector) }
        .inspector(isPresented: $showsInspector) {
            AskMeetingPanel(
                thread: store.liveAsk,
                timecode: { store.timecode(forLine: $0) },
                onSelect: { store.scrollToLine($0) }
            )
            .inspectorColumnWidth(min: 240, ideal: 280, max: 360)
        }
    }
}

private struct TranscriptColumn: View {
    @EnvironmentObject private var store: TranslatorStore
    var openMeeting: (String) -> Void

    private static let columnWidth: CGFloat = 760

    var body: some View {
        let rows = store.bilingualTranscriptRows
        if rows.isEmpty {
            LiveEmptyState()
        } else {
            let lines = TranscriptLine.lines(for: rows)
            let currentID = store.isSessionActive ? rows.last?.id : nil
            let actions = TranscriptRowActions(store: store, scope: .live)
            let pinned = store.pinnedIndexes
            let stale = store.staleCaptionDropped
            let scrollTarget = store.scrollRequest.map {
                ScrollTarget(id: BilingualTranscriptRow.lineID($0.index), nonce: $0.nonce)
            }
            let hasEnded = store.hasEndedSession

            FollowingScrollView(
                rowCount: rows.count + (hasEnded ? 1 : 0),
                tail: rows.last?.english?.text ?? "",
                spacing: 0,
                maxContentWidth: Self.columnWidth,
                topInset: 8,
                bottomInset: 8,
                scrollTarget: scrollTarget
            ) {
                ForEach(lines) { line in
                    TranscriptRow(
                        row: line.row,
                        showsSpeaker: line.showsSpeaker,
                        isCurrent: line.row.id == currentID,
                        isPinned: pinned.contains(line.row.index),
                        pendingText: L10n.text(
                            line.row.hasNoTranslation
                                ? "No translation"
                                : line.row.id == currentID && stale ? "Skipped a stale caption" : "Translating…"),
                        actions: actions
                    )
                    .equatable()
                }
                if hasEnded {
                    SessionEndMarker(
                        summary: store.endedSummary,
                        meetingID: store.status?.sessionID,
                        openMeeting: openMeeting
                    )
                }
            }
            .id(store.transcriptEpoch)
        }
    }
}

private struct SessionEndMarker: View {
    @EnvironmentObject private var store: TranslatorStore
    var summary: String
    var meetingID: String?
    var openMeeting: (String) -> Void

    var body: some View {
        VStack(spacing: 8) {
            HStack(spacing: 12) {
                rule
                Text(summary)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .monospacedDigit()
                    .lineLimit(1)
                    .layoutPriority(1)
                rule
            }
            HStack(spacing: 16) {
                if let meetingID {
                    Button(L10n.text("Open in Meetings")) { openMeeting(meetingID) }
                        .foregroundStyle(Color.accentColor)
                }
                ExportMenu(store: store, sessionID: nil, isEnabled: true, appearance: .text)
                    .fixedSize()
            }
            .buttonStyle(.borderless)
            .menuStyle(.borderlessButton)
        }
        .padding(.vertical, 24)
        .padding(.horizontal, 12)
        .padding(.leading, TranscriptRow.gutterWidth + 12)
        .frame(maxWidth: .infinity)
    }

    private var rule: some View {
        Rectangle()
            .fill(.separator)
            .frame(height: 1)
    }
}

private struct LiveEmptyState: View {
    @EnvironmentObject private var store: TranslatorStore

    var body: some View {
        if store.liveStatus == .failed {
            EmptyState(
                title: L10n.text("Can't caption right now"),
                systemImage: "exclamationmark.triangle",
                message: L10n.text("The message above says what went wrong."),
                tint: Palette.warning
            )
        } else if store.liveStatus == .starting {
            EmptyState(
                title: L10n.text("Starting…"),
                systemImage: "hourglass",
                message: L10n.text("Loading the speech models. Captions appear here as people speak."),
                showsProgress: true
            )
        } else if store.isSessionActive {
            EmptyState(
                title: L10n.text("Listening…"),
                systemImage: "waveform",
                message: L10n.text("Captions appear here as people speak.")
            )
        } else {
            EmptyState(
                title: L10n.text("No captions yet"),
                systemImage: "captions.bubble",
                message: String(
                    format: L10n.text("Start when your meeting begins. Captions come from %@."),
                    L10n.text(store.selectedLiveSource.title))
            ) {
                Button(L10n.text("Start Captions")) {
                    Task { await store.startLiveSession() }
                }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)
                .disabled(!store.canStartSession)
                .help(L10n.text("Start captions (⌘R)"))
            }
        }
    }
}

private struct LiveToolbar: ToolbarContent {
    @ObservedObject var store: TranslatorStore
    @Binding var showsInspector: Bool
    @Environment(\.floatingCaptions) private var floatingCaptions

    private static let sources: [LiveSessionSource] = [.microphone, .systemAudio]

    var body: some ToolbarContent {
        if !store.showsSetupCard {
            if store.isSessionActive {
                if store.showsPauseResume {
                    ToolbarItem { pauseResumeButton }
                }
                if store.showsStop {
                    ToolbarItem { stopButton }
                }
            } else {
                ToolbarItem { startButton }
                ToolbarGap()
                ToolbarItem { sourceMenu }
            }

            StatusToolbarItem(store: store)

            ToolbarItemGroup(placement: .primaryAction) {
                if store.isCaptionStreamActive {
                    Toggle(isOn: Binding(get: { floatingCaptions.isShown }, set: { _ in floatingCaptions.toggle() })) {
                        Label(L10n.text("Floating Captions"), systemImage: "captions.bubble")
                    }
                    .toolbarIcon(isOn: floatingCaptions.isShown)
                    .help(L10n.text("Show or hide the floating captions (⇧⌘C)"))
                }
                if !store.bilingualTranscriptRows.isEmpty {
                    ExportMenu(store: store, sessionID: nil, isEnabled: true)
                }
                Toggle(isOn: $showsInspector) {
                    Label(L10n.text("Ask the Meeting"), systemImage: "sidebar.trailing")
                }
                .toolbarIcon(isOn: showsInspector)
                .keyboardShortcut("k", modifiers: .command)
                .help(L10n.text("Ask the meeting a question (⌘K)"))
            }
        }
    }

    private var pauseResumeButton: some View {
        Button {
            Task { await store.togglePauseResume() }
        } label: {
            Label(L10n.text(store.pauseResumeTitle), systemImage: store.pauseResumeSystemImage)
        }
        .disabled(!store.canPauseResumeSession)
        .help(String(format: L10n.text("%@ captions (⌘P)"), L10n.text(store.pauseResumeTitle)))
    }

    private var stopButton: some View {
        Button {
            Task { await store.stopSession() }
        } label: {
            Label(L10n.text("Stop"), systemImage: "stop.fill")
        }
        .disabled(!store.canEndSession)
        .help(L10n.text("Stop captions (⌘.)"))
    }

    private var startButton: some View {
        Button {
            Task { await store.startLiveSession() }
        } label: {
            Label(L10n.text("Start"), systemImage: "play.fill")
                .labelStyle(.titleAndIcon)
        }
        .buttonStyle(.borderedProminent)
        .disabled(!store.canStartSession)
        .help(L10n.text("Start captions (⌘R)"))
    }

    private var sourceMenu: some View {
        Menu {
            Picker(L10n.text("Source"), selection: $store.selectedLiveSource) {
                ForEach(Self.sources) { source in
                    Label(L10n.text(source.title), systemImage: source.systemImage).tag(source)
                }
            }
            .pickerStyle(.inline)

            Picker(L10n.text("Language"), selection: $store.settings.languagePair) {
                ForEach(LanguagePair.supported) { pair in
                    Text(pair.label).tag(pair)
                }
            }
            .pickerStyle(.inline)

            Picker(L10n.text("Glossary"), selection: $store.settings.activeGlossaryProfile) {
                Text(L10n.text("No glossary")).tag("")
                ForEach(store.settings.glossaryProfiles) { profile in
                    Text(profile.name).tag(profile.name)
                }
            }
            .pickerStyle(.inline)
        } label: {
            Label(L10n.text(store.selectedLiveSource.title), systemImage: store.selectedLiveSource.systemImage)
                .labelStyle(.titleAndIcon)
        }
        .help(L10n.text("Where captions come from, the language pair and the glossary"))
    }
}

struct FloatingCaptionsControl {
    var isShown = false
    var toggle: () -> Void = {}
}

extension EnvironmentValues {
    @Entry var floatingCaptions = FloatingCaptionsControl()
}
