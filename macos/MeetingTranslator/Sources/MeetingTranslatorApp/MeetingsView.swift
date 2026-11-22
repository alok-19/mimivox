import MeetingTranslatorCore
import SwiftUI

struct MeetingsView: View {
    @EnvironmentObject private var store: TranslatorStore
    @Bindable var search: MeetingsSearch
    var openLive: () -> Void

    @AppStorage("meetings.showsInspector") private var showsInspector = false
    @AppStorage("meetings.inspectorTab") private var inspectorTab = MeetingInspectorTab.notes
    @State private var pendingDeletion: ArchivedSessionSummary?

    var body: some View {
        let openMeeting = store.archivedSessions.first { $0.id == store.review.selectedID }
        let actionable = search.isActive ? nil : openMeeting
        content
            .frame(maxWidth: .infinity, maxHeight: .infinity)
            .background(Color(nsColor: .textBackgroundColor))
            .navigationTitle(L10n.text("Meetings"))
            .navigationSubtitle(subtitle)
            .searchable(
                text: $search.query, placement: .toolbar, prompt: L10n.text("Search Meetings")
            )
            .toolbar {
                MeetingsToolbar(
                    store: store,
                    search: search,
                    showsInspector: $showsInspector,
                    meetingID: actionable?.id,
                    requestDelete: { pendingDeletion = actionable }
                )
            }
            .inspector(isPresented: $showsInspector) {
                MeetingInspector(tab: $inspectorTab)
                    .inspectorColumnWidth(min: 240, ideal: 280, max: 400)
            }
            .confirmationDialog(
                pendingDeletion.map {
                    String(format: L10n.text("Delete %@?"), MeetingFormat.title($0))
                } ?? L10n.text("Delete this meeting?"),
                isPresented: Binding(get: { pendingDeletion != nil }, set: { if !$0 { pendingDeletion = nil } }),
                titleVisibility: .visible,
                presenting: pendingDeletion
            ) { meeting in
                Button(L10n.text("Delete Meeting"), role: .destructive) {
                    Task { await store.deleteMeeting(id: meeting.id) }
                }
            } message: { meeting in
                Text(deletionMessage(for: meeting))
            }
            .task {
                await store.refreshArchivedSessions()
                store.openNewestMeetingIfNone()
            }
            .task(id: search.requestKey) {
                if search.isActive { try? await Task.sleep(for: .milliseconds(250)) }
                guard !Task.isCancelled else { return }
                await store.searchLibrary(
                    query: search.trimmedQuery,
                    since: search.range.since(),
                    until: nil,
                    speaker: search.speaker.isEmpty ? nil : search.speaker,
                    tag: search.tag.isEmpty ? nil : search.tag
                )
            }
    }

    @ViewBuilder
    private var content: some View {
        if search.isActive {
            SearchResults(search: search) { id, line in
                search.clear()
                store.openMeeting(id: id, focusLine: line)
            }
        } else if !store.archiveLoaded {
            if store.helperFailed {
                EmptyState(
                    title: L10n.text("Meetings Aren't Available"),
                    systemImage: "exclamationmark.triangle",
                    message: L10n.text("The message above says what went wrong."),
                    tint: Palette.warning
                )
            } else {
                DelayedProgress()
            }
        } else if store.archivedSessions.isEmpty {
            EmptyMeetings(
                savesTranscripts: store.settings.retentionPolicy != RetentionPolicy.none.rawValue, openLive: openLive)
        } else {
            GeometryReader { proxy in
                let isCompact = proxy.size.width < MeetingsLayout.splitWidth
                HStack(spacing: 0) {
                    if !isCompact {
                        MeetingList(days: MeetingFormat.days(store.archivedSessions)) { pendingDeletion = $0 }
                            .frame(width: MeetingsLayout.listWidth)
                        Divider()
                    }
                    MeetingDetail(isCompact: isCompact)
                }
            }
        }
    }

    private var subtitle: Text {
        if search.isActive {
            guard let response = store.librarySearch else { return Text(L10n.text("Searching…")) }
            let count = response.totalSessions
            return count == 0
                ? Text(L10n.text("No matches"))
                : Text(String(format: L10n.text("%lld matching meetings"), Int64(count)))
        }
        let count = store.archivedSessions.count
        return count == 0
            ? Text("")
            : Text(String(format: L10n.text("%lld meetings"), Int64(count)))
    }

    private func deletionMessage(for meeting: ArchivedSessionSummary) -> String {
        let base = L10n.text(
            "The transcript, notes and pins of this meeting are deleted from this Mac. This can't be undone.")
        guard store.status?.sessionID == meeting.id, !store.isSessionActive else { return base }
        return base + " " + L10n.text("It is also cleared from Live.")
    }
}

private struct EmptyMeetings: View {
    var savesTranscripts: Bool
    var openLive: () -> Void

    var body: some View {
        EmptyState(
            title: L10n.text("No Meetings Yet"),
            systemImage: "clock.arrow.circlepath",
            message: savesTranscripts
                ? L10n.text("Meetings you caption on Live are saved here, with their transcript, notes and key words.")
                : L10n.text("MimiVox is set not to save transcripts. Change this in Settings, under Privacy.")
        ) {
            Button(L10n.text("Open Live"), action: openLive)
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
                .keyboardShortcut(.defaultAction)
        }
    }
}

private struct DelayedProgress: View {
    @State private var isShown = false

    var body: some View {
        ProgressView()
            .controlSize(.large)
            .opacity(isShown ? 1 : 0)
            .task {
                try? await Task.sleep(for: .milliseconds(300))
                isShown = true
            }
    }
}

// MARK: Toolbar

private struct MeetingsToolbar: ToolbarContent {
    @ObservedObject var store: TranslatorStore
    @Bindable var search: MeetingsSearch
    @Binding var showsInspector: Bool
    var meetingID: String?
    var requestDelete: () -> Void

    var body: some ToolbarContent {
        StatusToolbarItem(store: store)

        if !store.archivedSessions.isEmpty {
            ToolbarItemGroup(placement: .primaryAction) {
                filterMenu
                if meetingID != nil {
                    ExportMenu(
                        store: store, sessionID: meetingID, isEnabled: true,
                        canExportPinned: !store.review.pinned.isEmpty)
                    Button(action: requestDelete) {
                        Label(L10n.text("Delete"), systemImage: "trash")
                    }
                    .toolbarIcon()
                    .help(L10n.text("Delete this meeting"))
                }
                Toggle(isOn: $showsInspector) {
                    Label(L10n.text("Inspector"), systemImage: "sidebar.trailing")
                }
                .toolbarIcon(isOn: showsInspector)
                .keyboardShortcut("i", modifiers: [.command, .option])
                .help(L10n.text("Notes, words and details (⌥⌘I)"))
            }

            ToolbarGap()
        }
    }

    private var filterMenu: some View {
        Menu {
            Picker(L10n.text("Date"), selection: $search.range) {
                ForEach(MeetingDateRange.allCases) { Text(L10n.text($0.rawValue)).tag($0) }
            }
            Picker(L10n.text("Speaker"), selection: $search.speaker) {
                Text(L10n.text("Any Speaker")).tag("")
                ForEach(store.librarySearch?.speakers ?? [], id: \.self) { Text($0).tag($0) }
            }
            Picker(L10n.text("Tag"), selection: $search.tag) {
                Text(L10n.text("Any Tag")).tag("")
                ForEach(store.librarySearch?.tags ?? [], id: \.self) { Text($0).tag($0) }
            }
            if search.hasFilters {
                Divider()
                Button(L10n.text("Clear Filters")) { search.clearFilters() }
            }
        } label: {
            Label(
                L10n.text("Filter"),
                systemImage: search.hasFilters
                    ? "line.3.horizontal.decrease.circle.fill" : "line.3.horizontal.decrease.circle")
        }
        .help(L10n.text("Show meetings from a date range, speaker or tag"))
    }
}

// MARK: List

private struct MeetingList: View {
    @EnvironmentObject private var store: TranslatorStore
    var days: [MeetingDay]
    var requestDelete: (ArchivedSessionSummary) -> Void
    @State private var renaming: ArchivedSessionSummary?
    @State private var renameDraft = ""

    var body: some View {
        List(selection: selection) {
            ForEach(days) { day in
                Section(day.title) {
                    ForEach(day.meetings) { meeting in
                        MeetingRow(meeting: meeting)
                            .tag(meeting.id)
                            .contextMenu {
                                Button(L10n.text("Rename…")) {
                                    renameDraft = meeting.headline ?? ""
                                    renaming = meeting
                                }
                                Button(L10n.text("Delete…"), role: .destructive) { requestDelete(meeting) }
                            }
                    }
                }
            }
        }
        .listStyle(.inset)
        .alert(L10n.text("Rename Meeting"), isPresented: isRenaming, presenting: renaming) { meeting in
            TextField(L10n.text("Title"), text: $renameDraft)
            Button(L10n.text("Rename")) { Task { await store.renameMeeting(id: meeting.id, title: renameDraft) } }
            Button(L10n.text("Cancel"), role: .cancel) {}
        } message: { _ in
            Text(L10n.text("Leave the title empty to use the generated one."))
        }
        .onDeleteCommand {
            if let meeting = store.archivedSessions.first(where: { $0.id == store.review.selectedID }) {
                requestDelete(meeting)
            }
        }
    }

    private var isRenaming: Binding<Bool> {
        Binding(get: { renaming != nil }, set: { if !$0 { renaming = nil } })
    }

    private var selection: Binding<String?> {
        Binding(
            get: { store.review.highlightedID },
            set: { id in
                guard let id, id != store.review.highlightedID else { return }
                store.openMeeting(id: id)
            }
        )
    }
}

private struct MeetingRow: View {
    var meeting: ArchivedSessionSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(MeetingFormat.rowTitle(meeting))
                .monospacedDigit()
                .lineLimit(1)
            Text(MeetingFormat.rowDetail(meeting))
                .font(.caption)
                .monospacedDigit()
                .foregroundStyle(.secondary)
                .lineLimit(1)
        }
        .padding(.vertical, 4)
    }
}

// MARK: Open meeting

private enum TranscriptColumn {
    static let width: CGFloat = 760
}

private enum MeetingsLayout {
    static let listWidth: CGFloat = 220
    static let minTranscriptWidth: CGFloat = 440
    static let splitWidth: CGFloat = listWidth + 1 + minTranscriptWidth
}

private struct MeetingDetail: View {
    @EnvironmentObject private var store: TranslatorStore
    var isCompact: Bool
    @State private var isStale = false

    var body: some View {
        let review = store.review
        VStack(spacing: 0) {
            if let detail = review.detail {
                MeetingHeader(detail: detail, review: review, picksMeeting: isCompact)
                Divider()
            }
            ReviewTranscript(review: review)
        }
        .opacity(isStale ? 0.5 : 1)
        .animation(.easeOut(duration: 0.15), value: isStale)
        .task(id: review.openingID) {
            // Dim the meeting still on screen only when the next one is slow to arrive.
            isStale = false
            guard review.openingID != nil, review.detail != nil else { return }
            try? await Task.sleep(for: .milliseconds(300))
            if !Task.isCancelled { isStale = true }
        }
    }
}

private struct MeetingHeader: View {
    @EnvironmentObject private var store: TranslatorStore
    var detail: ArchivedSessionDetail
    var review: ReviewSession
    var picksMeeting: Bool

    private var summary: ArchivedSessionSummary? {
        store.archivedSessions.first { $0.id == detail.id }
    }

    private var startedTitle: String {
        MeetingFormat.title(startedAt: detail.startedAt, fallback: detail.id)
    }

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 16) {
            VStack(alignment: .leading, spacing: 4) {
                title
                meta
                    .font(.callout)
                    .monospacedDigit()
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            if picksMeeting { meetingMenu }
            if review.hasSkippedCaptions {
                Button(L10n.text("Recover Skipped Captions")) {
                    Task { await store.recoverSkippedCaptions(id: detail.id) }
                }
                .disabled(store.isBusy || store.isSessionActive)
                .help(
                    L10n.text(
                        "Transcribe and translate speech a catch-up skipped, using the audio kept with this meeting"))
            }
        }
        .padding(.horizontal, 12)
        .padding(.vertical, 16)
        .frame(maxWidth: TranscriptColumn.width)
        .frame(maxWidth: .infinity)
    }

    private var title: some View {
        MeetingTitleButton(title: summary?.headline ?? startedTitle, current: summary?.headline ?? "") {
            newTitle in
            Task { await store.renameMeeting(id: detail.id, title: newTitle) }
        }
    }

    private var meetingMenu: some View {
        Menu {
            ForEach(store.archivedSessions) { meeting in
                Toggle(isOn: Binding(get: { meeting.id == detail.id }, set: { _ in store.openMeeting(id: meeting.id) }))
                {
                    Text(MeetingFormat.menuTitle(meeting))
                }
            }
        } label: {
            Image(systemName: "list.bullet")
        }
        .menuStyle(.button)
        .buttonStyle(.borderless)
        .menuIndicator(.hidden)
        .fixedSize()
        .help(L10n.text("Choose another meeting"))
    }

    private var meta: Text {
        var parts: [String] = []
        if summary?.headline != nil { parts.append(startedTitle) }
        parts.append(MeetingFormat.source(label: detail.label))
        if let file = MeetingFormat.fileName(label: detail.label) { parts.append(file) }
        if let duration = review.duration { parts.append(MeetingFormat.duration(duration)) }
        let lineCount = review.lineCount
        let lineLabel =
            lineCount == 1
            ? L10n.text("1 line")
            : String(format: L10n.text("%d lines"), lineCount)
        return Text("\(parts.joined(separator: " · ")) · \(lineLabel)")
    }
}

/// The meeting's title; clicking it edits it in place. An empty title restores the generated one.
private struct MeetingTitleButton: View {
    var title: String
    var current: String
    var onRename: (String) -> Void

    @State private var isEditing = false
    @State private var draft = ""

    var body: some View {
        Button {
            draft = current
            isEditing = true
        } label: {
            Text(title)
                .font(.title2.weight(.semibold))
                .lineLimit(2)
                .multilineTextAlignment(.leading)
        }
        .buttonStyle(.plain)
        .help(L10n.text("Rename meeting"))
        .popover(isPresented: $isEditing, arrowEdge: .bottom) {
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    TextField(L10n.text("Meeting title"), text: $draft)
                        .textFieldStyle(.roundedBorder)
                        .frame(width: 320)
                        .onSubmit(commit)
                    Button(L10n.text("Save"), action: commit)
                        .keyboardShortcut(.defaultAction)
                }
                Text(L10n.text("Leave empty to use the generated title."))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            .padding(12)
        }
    }

    private func commit() {
        isEditing = false
        onRename(draft)
    }
}

private struct ReviewTranscript: View {
    @EnvironmentObject private var store: TranslatorStore
    var review: ReviewSession

    var body: some View {
        if review.selectedID == nil {
            if review.isLoading {
                DelayedProgress()
                    .frame(maxWidth: .infinity, maxHeight: .infinity)
            } else {
                EmptyState(title: L10n.text("No Meeting Selected"), systemImage: "clock.arrow.circlepath")
            }
        } else if review.rows.isEmpty {
            EmptyState(
                title: L10n.text("No Captions"),
                systemImage: "captions.bubble",
                message: L10n.text("This meeting ended before anyone spoke.")
            )
        } else {
            transcript
        }
    }

    private var transcript: some View {
        let lines = TranscriptLine.lines(for: review.rows)
        let actions = TranscriptRowActions(store: store, scope: .review)
        return ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    ForEach(lines) { line in
                        TranscriptRow(
                            row: line.row,
                            showsSpeaker: line.showsSpeaker,
                            isCurrent: line.row.gap == nil && line.row.index == review.focusLineID,
                            isPinned: review.pinned.contains(line.row.index),
                            pendingText: L10n.text("No translation"),
                            actions: actions
                        )
                        .equatable()
                    }
                }
                .padding(.vertical, 8)
                .frame(maxWidth: TranscriptColumn.width, alignment: .leading)
                .frame(maxWidth: .infinity)
            }
            .onAppear { scrollToFocus(proxy, animated: false) }
            .onChange(of: review.focusRequest) { scrollToFocus(proxy, animated: true) }
        }
        .id(review.selectedID)  // each meeting opens at its own scroll position
    }

    private func scrollToFocus(_ proxy: ScrollViewProxy, animated: Bool) {
        guard let line = review.focusLineID else { return }
        let target = BilingualTranscriptRow.lineID(line)
        DispatchQueue.main.async {
            if animated {
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(target, anchor: .center) }
            } else {
                proxy.scrollTo(target, anchor: .center)
            }
        }
    }
}

// MARK: Search

private struct SearchResults: View {
    @EnvironmentObject private var store: TranslatorStore
    var search: MeetingsSearch
    var open: (_ meetingID: String, _ line: Int?) -> Void

    var body: some View {
        if let response = store.librarySearch {
            if response.results.isEmpty {
                noResults
            } else {
                results(response)
            }
        } else {
            DelayedProgress()
        }
    }

    private var noResults: some View {
        EmptyState(
            title: search.trimmedQuery.isEmpty
                ? L10n.text("No Matching Meetings")
                : String(format: L10n.text("No Results for “%@”"), search.trimmedQuery),
            systemImage: "magnifyingglass",
            message: search.hasFilters
                ? L10n.text("Try a different search or loosen the filters.")
                : L10n.text("Check the spelling or try another word. Search works in Japanese and English.")
        ) {
            if search.hasFilters {
                Button(L10n.text("Clear Filters")) { search.clearFilters() }
                    .buttonStyle(.borderedProminent)
                    .controlSize(.large)
            }
        }
    }

    private func results(_ response: LibrarySearchResponse) -> some View {
        let actions = TranscriptRowActions(store: store, scope: .search)
        let isStale = store.isSearchingLibrary || response.query != search.trimmedQuery
        return ScrollView {
            LazyVStack(alignment: .leading, spacing: 0) {
                summary(response)
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .padding(.horizontal, 12)
                    .padding(.vertical, 16)
                ForEach(response.results) { hit in
                    SearchGroup(hit: hit, actions: actions, open: open)
                }
            }
            .frame(maxWidth: TranscriptColumn.width, alignment: .leading)
            .frame(maxWidth: .infinity)
        }
        .opacity(isStale ? 0.5 : 1)
    }

    private func summary(_ response: LibrarySearchResponse) -> Text {
        let countText = String(
            format: L10n.text("Showing %1$lld of %2$lld meetings"),
            Int64(response.results.count), Int64(response.totalSessions))
        guard !response.query.isEmpty else { return Text(countText) }
        return Text(
            String(
                format: L10n.text("Showing %1$lld of %2$lld meetings for “%@”"),
                Int64(response.results.count), Int64(response.totalSessions), response.query))
    }
}

private struct SearchGroup: View {
    var hit: LibrarySessionHit
    var actions: TranscriptRowActions
    var open: (_ meetingID: String, _ line: Int?) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 0) {
            Button {
                open(hit.id, hit.lines.first?.lineID)
            } label: {
                HStack(spacing: 8) {
                    VStack(alignment: .leading, spacing: 4) {
                        Text(MeetingFormat.title(startedAt: hit.startedAt, fallback: hit.id))
                            .font(.headline)
                            .monospacedDigit()
                        meta
                            .font(.caption)
                            .monospacedDigit()
                            .foregroundStyle(.secondary)
                    }
                    Spacer(minLength: 8)
                    Image(systemName: "chevron.right")
                        .font(.caption.weight(.semibold))
                        .foregroundStyle(.tertiary)
                }
                .padding(.horizontal, 12)
                .padding(.vertical, 8)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)

            ForEach(hit.lines) { line in
                Button {
                    open(hit.id, line.lineID)
                } label: {
                    TranscriptRow(
                        row: line.transcriptRow,
                        showsSpeaker: true,
                        isCurrent: false,
                        isPinned: false,
                        pendingText: "",
                        actions: actions,
                        highlights: line.highlights
                    )
                    .contentShape(Rectangle())
                }
                .buttonStyle(.plain)
            }

            Divider()
                .padding(.top, 8)
                .padding(.bottom, 8)
        }
    }

    private var meta: Text {
        let lead = ([MeetingFormat.source(label: hit.label)] + hit.tags).joined(separator: " · ")
        guard hit.matchCount > 0 else { return Text(lead) }
        let count = String(format: L10n.text("%lld matching lines"), Int64(hit.matchCount))
        return Text("\(lead) · \(count)")
    }
}
