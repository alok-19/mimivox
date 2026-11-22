import AppKit
import MeetingTranslatorCore
import SwiftUI

@MainActor
final class FloatingCaptionPanelController: ObservableObject {
    private var panel: NSPanel?
    @Published private(set) var isVisible = false

    func toggle(store: TranslatorStore) {
        if panel?.isVisible == true {
            close()
        } else {
            show(store: store)
        }
    }

    func show(store: TranslatorStore) {
        if let panel {
            panel.orderFrontRegardless()
            isVisible = true
            return
        }

        let panel = NSPanel(
            contentRect: NSRect(x: 0, y: 0, width: 820, height: 320),
            styleMask: [.nonactivatingPanel, .titled, .fullSizeContentView, .closable, .resizable],
            backing: .buffered,
            defer: false
        )
        panel.title = "Floating Captions"
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.backgroundColor = .clear
        panel.isOpaque = false
        panel.hasShadow = true
        panel.isFloatingPanel = true
        panel.level = .floating
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary, .ignoresCycle]
        panel.isMovableByWindowBackground = true
        panel.hidesOnDeactivate = false
        panel.standardWindowButton(.closeButton)?.isHidden = true
        panel.standardWindowButton(.zoomButton)?.isHidden = true
        panel.standardWindowButton(.miniaturizeButton)?.isHidden = true
        panel.minSize = NSSize(width: 500, height: 164)

        let rootView = FloatingCaptionHUD(onClose: { [weak self] in self?.close() }, window: { [weak panel] in panel })
            .environmentObject(store)
        panel.contentView = NSHostingView(rootView: rootView)
        // Reopen where the user last left it, at the size they chose.
        if !panel.setFrameUsingName(Self.frameName) { panel.center() }
        panel.setFrameAutosaveName(Self.frameName)
        panel.orderFrontRegardless()
        self.panel = panel
        isVisible = true
    }

    private static let frameName = "FloatingCaptions"

    func close() {
        panel?.close()
        panel = nil
        isVisible = false
    }
}

/// The HUD's content without its panel, for the UI snapshot tool.
@MainActor
func makeCaptionHUDSnapshotView(store: TranslatorStore, hovering: Bool = false, highContrast: Bool = false) -> some View
{
    FloatingCaptionHUD(onClose: {}, forcedHover: hovering ? true : nil, forcedHighContrast: highContrast ? true : nil)
        .environment(\.hudUsesMaterial, false)
        .environmentObject(store)
}

/// UserDefaults keys for the per-device HUD preferences. A launch argument such
/// as `-hud.displayMode englishOnly` overrides them for one run.
enum HUDPreferences {
    static let displayModeKey = "hud.displayMode"
    static let sizeKey = "hud.size"
    static let backgroundKey = "hud.background"
    static let voiceKey = "hud.readAloud.voice"
    static let rateKey = "hud.readAloud.rate"
}

private struct FloatingCaptionHUD: View {
    @EnvironmentObject private var store: TranslatorStore
    @State private var isHovering = false
    @AppStorage(HUDPreferences.displayModeKey) private var displayModeRaw = CaptionDisplayMode.bilingual.rawValue
    @AppStorage(HUDPreferences.sizeKey) private var sizeRaw = CaptionSizePreset.compact.rawValue
    @AppStorage(HUDPreferences.backgroundKey) private var backgroundRaw = HUDBackground.standard.rawValue
    @Environment(\.colorSchemeContrast) private var colorSchemeContrast
    /// Where a drag began: the mouse and the window origin, in screen points.
    @State private var dragStart: (mouse: NSPoint, origin: NSPoint)?
    var onClose: () -> Void
    /// The panel to move; nil in the snapshot tool.
    var window: () -> NSWindow? = { nil }
    /// Snapshot-only overrides; nil in the app.
    var forcedHover: Bool?
    var forcedHighContrast: Bool?

    private var hovering: Bool { forcedHover ?? isHovering }
    private var highContrast: Bool {
        forcedHighContrast
            ?? (colorSchemeContrast == .increased || NSWorkspace.shared.accessibilityDisplayShouldIncreaseContrast)
    }

    private var displayMode: CaptionDisplayMode {
        CaptionDisplayMode(rawValue: displayModeRaw) ?? .bilingual
    }

    private var background: HUDBackground {
        HUDBackground(rawValue: backgroundRaw) ?? .standard
    }

    private var sizePreset: CaptionSizePreset {
        get { CaptionSizePreset(rawValue: sizeRaw) ?? .compact }
        nonmutating set { sizeRaw = newValue.rawValue }
    }

    var body: some View {
        VStack(alignment: .leading, spacing: sizePreset.verticalSpacing) {
            // Controls and state share one row above the banner, so hover controls never cover "Catch up".
            HStack(alignment: .center, spacing: 8) {
                HUDStateLine()
                Spacer(minLength: 8)
                controls
            }

            if let lag = Banner.latency(
                store.latencyHealth, mode: CatchUpMode(rawValue: store.settings.catchUpMode) ?? .manual)
            {
                BannerView(banner: lag, compact: true)
            }

            CaptionHistoryView(displayMode: displayMode, sizePreset: sizePreset)
                // Without the blur behind it, a shadow keeps text legible over a busy desktop.
                .shadow(color: .black.opacity(background.usesBlur ? 0 : 0.7), radius: 2, y: 1)
                .contentMargins(.top, 24, for: .scrollContent)
                .mask(alignment: .top) {
                    VStack(spacing: 0) {
                        LinearGradient(colors: [.clear, .black], startPoint: .top, endPoint: .bottom)
                            .frame(height: 24)
                        Color.black
                    }
                }

            HUDFootnote(readAloud: store.readAloud)
        }
        .padding(sizePreset.padding)
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .leading)
        .background {
            ZStack {
                if background.usesBlur { HUDMaterial() }
                Color.black.opacity(HUDStyle.scrim(background, hovering: hovering, highContrast: highContrast))
            }
            .clipShape(RoundedRectangle(cornerRadius: 16, style: .continuous))
        }
        .overlay(alignment: .top) {
            // A grabber, as on a sheet, says this can be moved.
            Capsule()
                .fill(Color.white.opacity(hovering ? 0.45 : 0))
                .frame(width: 36, height: 5)
                .padding(.vertical, 6)
                .padding(.horizontal, 24)
                .contentShape(Rectangle())
                .onHover { inside in inside ? NSCursor.openHand.push() : NSCursor.pop() }
                .padding(.top, 2)
                .help(L10n.text("Drag to move the captions"))
        }
        // Drag anywhere a control isn't to move the captions; buttons keep their clicks.
        .contentShape(Rectangle())
        .gesture(moveWindow)
        .animation(.easeOut(duration: 0.18), value: hovering)
        // Always dark: white text on a black scrim reads over light and dark desktops alike, and the scrim is what carries the contrast.
        .environment(\.colorScheme, .dark)
        .environment(\.hudHighContrast, highContrast)
        .padding(8)
        .onHover { isHovering = $0 }
    }

    private var controls: some View {
        HStack(spacing: 12) {
            Button {
                sizePreset = sizePreset.previous
            } label: {
                Image(systemName: "textformat.size.smaller")
            }
            .help(L10n.text("Smaller captions"))

            Button {
                sizePreset = sizePreset.next
            } label: {
                Image(systemName: "textformat.size.larger")
            }
            .help(L10n.text("Larger captions"))

            Menu {
                Picker(L10n.text("Show"), selection: $displayModeRaw) {
                    ForEach(CaptionDisplayMode.allCases) { mode in
                        Text(L10n.text(mode.title)).tag(mode.rawValue)
                    }
                }
                .pickerStyle(.inline)
            } label: {
                Image(systemName: displayMode.systemImage)
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help(String(format: L10n.text("Show: %@"), L10n.text(displayMode.title)))

            Menu {
                Picker(L10n.text("Transparency"), selection: $backgroundRaw) {
                    ForEach(HUDBackground.allCases) { level in
                        Text(level.localizedTitle).tag(level.rawValue)
                    }
                }
                .pickerStyle(.inline)
            } label: {
                Image(systemName: "circle.lefthalf.filled")
            }
            .menuStyle(.borderlessButton)
            .menuIndicator(.hidden)
            .fixedSize()
            .help(String(format: L10n.text("Transparency: %@"), background.localizedTitle))

            PinnedLinesMenu()
            ReadAloudMenu(readAloud: store.readAloud)

            Button {
                onClose()
            } label: {
                Image(systemName: "xmark")
            }
            .help(L10n.text("Close captions"))
        }
        .buttonStyle(.plain)
        .font(.title3)
        .foregroundStyle(Color.white.opacity(HUDStyle.earlier(highContrast)))
        .padding(.horizontal, 12)
        .padding(.vertical, 8)
        .background(Color.black.opacity(0.55), in: Capsule())
        .opacity(hovering ? 1 : 0)
        .allowsHitTesting(hovering)
    }

    /// Tracks the mouse in screen points, so moving the window under it never feeds back into the drag.
    private var moveWindow: some Gesture {
        DragGesture(minimumDistance: 2)
            .onChanged { _ in
                guard let window = window() else { return }
                let mouse = NSEvent.mouseLocation
                let start = dragStart ?? (mouse, window.frame.origin)
                dragStart = start
                window.setFrameOrigin(
                    NSPoint(x: start.origin.x + mouse.x - start.mouse.x, y: start.origin.y + mouse.y - start.mouse.y))
            }
            .onEnded { _ in dragStart = nil }
    }
}

/// Backing and text levels for the HUD. Over a pure white desktop (blur ignored),
/// the 0.68 scrim gives the current line about 7.9:1 and earlier lines 5.7:1;
/// Increase Contrast raises the scrim to 0.8 and all text to full white.
private enum HUDStyle {
    static let hoverLift = 0.06
    static let highContrastScrim = 0.8

    static func scrim(_ background: HUDBackground, hovering: Bool, highContrast: Bool) -> Double {
        highContrast ? max(highContrastScrim, background.scrim) : background.scrim + (hovering ? hoverLift : 0)
    }

    static func current(_ highContrast: Bool) -> Double { 1.0 }
    static func earlier(_ highContrast: Bool) -> Double { highContrast ? 1.0 : 0.8 }
    static func currentJapanese(_ highContrast: Bool) -> Double { highContrast ? 1.0 : 0.88 }
    static func earlierJapanese(_ highContrast: Bool) -> Double { highContrast ? 1.0 : 0.8 }
}

/// How much of the desktop shows through the captions. Standard is the reviewed
/// default; the lighter levels drop the blur and lean on a text shadow instead.
private enum HUDBackground: String, CaseIterable, Identifiable {
    case solid, standard, translucent, clear

    var id: String { rawValue }

    var localizedTitle: String {
        switch self {
        case .solid: L10n.text("Solid background")
        case .standard: L10n.text("Standard background")
        case .translucent: L10n.text("Translucent background")
        case .clear: L10n.text("Clear background")
        }
    }

    var scrim: Double {
        switch self {
        case .solid: 0.9
        case .standard: 0.68
        case .translucent: 0.4
        case .clear: 0.16
        }
    }

    var usesBlur: Bool { self == .solid || self == .standard }
}

extension EnvironmentValues {
    @Entry fileprivate var hudHighContrast = false

    /// Off in the snapshot tool, where a behind-window blur cannot render and would hide the backdrop.
    @Entry fileprivate var hudUsesMaterial = true
}

private struct HUDMaterial: View {
    @Environment(\.hudUsesMaterial) private var usesMaterial

    var body: some View {
        if usesMaterial { HUDVisualEffect() }
    }
}

private struct HUDVisualEffect: NSViewRepresentable {
    func makeNSView(context: Context) -> NSVisualEffectView {
        let view = NSVisualEffectView()
        view.material = .hudWindow
        view.blendingMode = .behindWindow
        view.state = .active
        // Match the forced dark scheme so the blur never lightens on a light system appearance.
        view.appearance = NSAppearance(named: .vibrantDark)
        return view
    }

    func updateNSView(_ view: NSVisualEffectView, context: Context) {}
}

extension CaptionDisplayMode {
    fileprivate var systemImage: String {
        switch self {
        case .englishOnly: "textformat.abc"
        case .bilingual: "character.bubble"
        case .japaneseOnly: "character"
        }
    }
}

/// A quiet dot and one word, shown only when the session is not simply live.
private struct HUDStateLine: View {
    @EnvironmentObject private var store: TranslatorStore
    @Environment(\.hudHighContrast) private var highContrast

    var body: some View {
        if let state {
            HStack(spacing: 8) {
                Circle().fill(state.color).frame(width: 7, height: 7)
                Text(state.word)
                    .font(.caption.weight(.semibold))
                    .foregroundStyle(Color.white.opacity(HUDStyle.earlier(highContrast)))
            }
            .accessibilityElement(children: .combine)
            .accessibilityLabel(state.word)
        }
    }

    /// Quiet when captions are healthy. The word is HUD-specific; only the color comes from `LiveStatus` via `Palette.dot`.
    private var state: (word: String, color: Color)? {
        let status = store.liveStatus
        let word: String
        switch status {
        case .live, .idle: return nil
        case .failed: word = "Needs attention"
        case .paused: word = "Paused"
        case .starting: word = "Starting"
        case .setupNeeded: word = "Setup needed"
        case .stopping: word = "Ending"
        case .catchingUp: word = "Catching up"
        case .behind: word = "Behind"
        }
        return (word, Palette.dot(for: status))
    }
}

/// Pinned lines: copy or export them, or click one to unpin it.
private struct PinnedLinesMenu: View {
    @EnvironmentObject private var store: TranslatorStore

    var body: some View {
        let pinned = store.bilingualTranscriptRows.filter { store.pinnedIndexes.contains($0.index) }
        Menu {
            Button(L10n.text("Copy pinned lines")) {
                copyToPasteboard(store.pinnedMarkdown)
            }
            Button(L10n.text("Export pinned lines to a file")) {
                Task { await store.exportPinned() }
            }
            Divider()
            ForEach(pinned) { row in
                Button(String(format: L10n.text("Unpin: %@"), row.english?.text ?? row.japanese?.text ?? "")) {
                    store.togglePin(index: row.index)
                }
            }
        } label: {
            Label("\(pinned.count)", systemImage: pinned.isEmpty ? "star" : "star.fill")
                .labelStyle(.titleAndIcon)
                .monospacedDigit()
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .disabled(pinned.isEmpty)
        .help(L10n.text(pinned.isEmpty ? "Pin a line with its star to collect it here" : "Pinned lines"))
    }
}

/// Switch read-aloud on or off and pick its voice and speed.
private struct ReadAloudMenu: View {
    @EnvironmentObject private var store: TranslatorStore
    @ObservedObject var readAloud: ReadAloudCoordinator
    private static let voices = ReadAloudVoices.english()
    private static let speeds: [(String, Double)] = [("Slow", 0.38), ("Normal", 0.5), ("Fast", 0.58), ("Faster", 0.65)]

    var body: some View {
        Menu {
            Toggle(
                L10n.text("Read English captions aloud"),
                isOn: Binding(
                    get: { readAloud.isEnabled },
                    set: { store.setReadAloud(enabled: $0) }
                ))
            Picker(L10n.text("Voice"), selection: $store.readAloudVoiceID) {
                Text(L10n.text("System default")).tag("")
                ForEach(Self.voices) { voice in
                    Text(voice.displayName).tag(voice.id)
                }
            }
            Picker(L10n.text("Speed"), selection: $store.readAloudRate) {
                ForEach(Self.speeds, id: \.1) { name, rate in
                    Text(L10n.text(name)).tag(rate)
                }
            }
            Divider()
            Text(L10n.text("Use headphones: speakers can be picked up by the meeting mic."))
        } label: {
            Image(systemName: readAloud.isEnabled ? "speaker.wave.2.fill" : "speaker.slash")
        }
        .menuStyle(.borderlessButton)
        .menuIndicator(.hidden)
        .fixedSize()
        .help(L10n.text(readAloud.isEnabled ? "Read-aloud is on" : "Read captions aloud"))
    }
}

/// One-line status under the captions: read-aloud route warning and skipped lines.
private struct HUDFootnote: View {
    @EnvironmentObject private var store: TranslatorStore
    @ObservedObject var readAloud: ReadAloudCoordinator
    @State private var deviceName: String?

    var body: some View {
        if let text = footnote {
            Text(text)
                .font(.caption)
                .foregroundStyle(.secondary)
                .fixedSize(horizontal: false, vertical: true)
                .onChange(of: readAloud.isEnabled) { _, enabled in
                    if enabled { deviceName = ReadAloudOutputNote.defaultOutputDeviceName() }
                }
                .onAppear { deviceName = ReadAloudOutputNote.defaultOutputDeviceName() }
        }
    }

    private var footnote: String? {
        var parts: [String] = []
        if let notice = store.notice {
            parts.append(L10n.text(notice.text))
        }
        if readAloud.isEnabled {
            parts.append(ReadAloudOutputNote.text(deviceName: deviceName))
            if store.isSessionPaused {
                parts.append(L10n.text("Read-aloud is paused with the session."))
            }
            if readAloud.droppedCount > 0 {
                let key =
                    readAloud.droppedCount == 1
                    ? "Skipped %d older line to keep up."
                    : "Skipped %d older lines to keep up."
                parts.append(String(format: L10n.text(key), readAloud.droppedCount))
            }
        }
        return parts.isEmpty ? nil : parts.joined(separator: " ")
    }
}

private struct CaptionHistoryView: View {
    @EnvironmentObject private var store: TranslatorStore
    var displayMode: CaptionDisplayMode
    var sizePreset: CaptionSizePreset

    var body: some View {
        let lines = captionLines
        FollowingScrollView(rowCount: lines.count, tail: tail(lines), spacing: sizePreset.rowSpacing) {
            ForEach(lines) { line in
                CaptionLineView(
                    line: line,
                    displayMode: displayMode,
                    startsRevealed: HUDDemo.revealsFirstLine && line.id == lines.first?.id,
                    isPinned: line.index.map(store.pinnedIndexes.contains) ?? false,
                    sizePreset: sizePreset,
                    store: store
                )
                .equatable()
            }
        }
        .id("\(store.transcriptEpoch)-\(displayMode.rawValue)")
        .onChange(of: lines.first?.index) { _, first in
            guard HUDDemo.pinsFirstLine, let first, !store.pinnedIndexes.contains(first) else { return }
            store.togglePin(index: first)
        }
    }

    private func tail(_ lines: [CaptionLine]) -> String {
        let last = lines.last
        return "\(last?.id ?? "")|\(last?.english ?? "")|\(last?.japanese ?? "")"
    }

    private var captionLines: [CaptionLine] {
        let rows = store.bilingualTranscriptRows
        let live = store.isCaptionStreamActive
        var lines: [CaptionLine] = rows.enumerated().map { offset, row in
            CaptionLine(
                // Chip only when the voice changes, so a long turn is not repeated.
                speaker: row.speaker != (offset > 0 ? rows[offset - 1].speaker : nil) ? row.speaker : nil,
                id: row.id,
                index: row.index,
                english: row.english?.text,
                japanese: row.japanese?.text,
                tokens: row.japanese?.tokens,
                placeholder: row.skipNote ?? (live ? "Translating…" : "—"),
                confidence: row.flaggedConfidence
            )
        }

        if let interim = store.interimJapaneseText {
            // Utterance still being transcribed after the last completed row.
            lines.append(
                CaptionLine(
                    id: "live",
                    english: nil,
                    japanese: interim,
                    placeholder: store.isTranslatingLive ? "Translating…" : "Listening…"
                ))
        } else if lines.isEmpty {
            lines.append(
                CaptionLine(
                    id: "placeholder",
                    english: nil,
                    japanese: nil,
                    placeholder: store.latestCaptionText
                ))
        }

        // The newest finished caption is current in every mode; the interim line and placeholders never outrank it.
        let hasText: (CaptionLine) -> Bool = {
            $0.id != "live" && (displayMode == .japaneseOnly ? $0.japanese != nil : $0.english != nil)
        }
        if let current = lines.lastIndex(where: hasText) {
            lines[current].isCurrent = true
        }
        return lines
    }
}

private struct CaptionLine: Identifiable, Equatable {
    var speaker: SpeakerTag?
    var id: String
    /// Server line index; nil for the live placeholder rows, which cannot be pinned.
    var index: Int?
    var english: String?
    var japanese: String?
    /// Furigana breakdown of `japanese` (learning mode); nil shows plain text.
    var tokens: [LearningToken]?
    var placeholder: String
    /// Set only for medium and low confidence.
    var confidence: CaptionConfidence?
    var isCurrent = false
}

private struct CaptionLineView: View, @MainActor Equatable {
    var line: CaptionLine
    var displayMode: CaptionDisplayMode
    var startsRevealed: Bool
    var isPinned: Bool
    var sizePreset: CaptionSizePreset
    var store: TranslatorStore
    @State private var isRevealed: Bool
    @State private var isHovering = false
    @Environment(\.hudHighContrast) private var highContrast

    private var isEmphasized: Bool { line.isCurrent || isPinned }
    private var englishAlpha: Double { isEmphasized ? HUDStyle.current(highContrast) : HUDStyle.earlier(highContrast) }
    private var japaneseAlpha: Double {
        isEmphasized ? HUDStyle.currentJapanese(highContrast) : HUDStyle.earlierJapanese(highContrast)
    }

    init(
        line: CaptionLine, displayMode: CaptionDisplayMode, startsRevealed: Bool,
        isPinned: Bool, sizePreset: CaptionSizePreset, store: TranslatorStore
    ) {
        self.line = line
        self.displayMode = displayMode
        self.startsRevealed = startsRevealed
        self.isPinned = isPinned
        self.sizePreset = sizePreset
        self.store = store
        _isRevealed = State(initialValue: startsRevealed)
    }

    static func == (a: CaptionLineView, b: CaptionLineView) -> Bool {
        a.line == b.line && a.displayMode == b.displayMode && a.startsRevealed == b.startsRevealed
            && a.isPinned == b.isPinned && a.sizePreset == b.sizePreset && a.store === b.store
    }

    var body: some View {
        let visibility = displayMode.visibility(revealed: isRevealed)
        let isPending = line.english == nil
        HStack(alignment: .top, spacing: 8) {
            VStack(alignment: .leading, spacing: sizePreset.textSpacing) {
                if let speaker = line.speaker {
                    HStack(spacing: 8) {
                        Circle().fill(Palette.speaker(speaker)).frame(width: 7, height: 7)
                        Text(speaker.label)
                            .font(.caption.weight(.semibold))
                            .foregroundStyle(Color.white.opacity(englishAlpha))
                            .lineLimit(1)
                    }
                }
                if visibility.showsEnglish {
                    HStack(alignment: .firstTextBaseline, spacing: 8) {
                        Text(line.english ?? line.placeholder)
                            .font(.system(size: sizePreset.englishSize, weight: .semibold))
                            .italic(isPending)
                            .lineSpacing(sizePreset.englishLineSpacing)
                            .fixedSize(horizontal: false, vertical: true)
                            .foregroundStyle(Color.white.opacity(englishAlpha))
                        if let confidence = line.confidence {
                            ConfidenceMarker(confidence: confidence)
                        }
                    }
                }
                if visibility.showsJapanese {
                    // Japanese-only mode falls back to the placeholder while the line is still being transcribed.
                    if let japanese = line.japanese, line.tokens?.matching(japanese) != nil {
                        FuriganaText(
                            text: japanese, tokens: line.tokens, size: sizePreset.japaneseSize,
                            color: Color.white.opacity(japaneseAlpha)
                        )
                    } else {
                        Text(line.japanese ?? (visibility.showsEnglish ? "" : line.placeholder))
                            .font(.system(size: sizePreset.japaneseSize, weight: .regular))
                            .italic(line.japanese == nil)
                            .lineSpacing(sizePreset.japaneseLineSpacing)
                            .fixedSize(horizontal: false, vertical: true)
                            .foregroundStyle(Color.white.opacity(japaneseAlpha))
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            // Clicking a single-language line shows its other language.
            .contentShape(Rectangle())
            .onTapGesture {
                if visibility.canReveal { isRevealed.toggle() }
            }
            .help(
                visibility.canReveal
                    ? (isRevealed ? "Click to hide the other language" : "Click to show the other language") : "")

            if let index = line.index, isPinned || isHovering {
                Button {
                    store.togglePin(index: index)
                } label: {
                    Image(systemName: isPinned ? "star.fill" : "star")
                        .foregroundStyle(isPinned ? Color.yellow : Color.white.opacity(HUDStyle.earlier(highContrast)))
                }
                .buttonStyle(.plain)
                .help(L10n.text(isPinned ? "Unpin Line" : "Pin Line"))
            }
        }
        .onHover { isHovering = $0 }
    }
}

private enum CaptionSizePreset: String, CaseIterable {
    case compact
    case medium
    case large

    var previous: CaptionSizePreset {
        switch self {
        case .compact: .compact
        case .medium: .compact
        case .large: .medium
        }
    }

    var next: CaptionSizePreset {
        switch self {
        case .compact: .medium
        case .medium: .large
        case .large: .large
        }
    }

    var englishSize: CGFloat {
        switch self {
        case .compact: 22
        case .medium: 28
        case .large: 36
        }
    }

    var japaneseSize: CGFloat {
        switch self {
        case .compact: 14.3
        case .medium: 18.2
        case .large: 23.4
        }
    }

    var englishLineSpacing: CGFloat {
        switch self {
        case .compact: 4.4
        case .medium: 5.6
        case .large: 7.2
        }
    }

    var japaneseLineSpacing: CGFloat {
        switch self {
        case .compact: 2.9
        case .medium: 3.6
        case .large: 4.7
        }
    }

    var textSpacing: CGFloat {
        switch self {
        case .compact: 7
        case .medium: 9
        case .large: 10
        }
    }

    var rowSpacing: CGFloat {
        switch self {
        case .compact: 14
        case .medium: 18
        case .large: 22
        }
    }

    var verticalSpacing: CGFloat {
        switch self {
        case .compact: 10
        case .medium: 12
        case .large: 14
        }
    }

    var padding: CGFloat {
        switch self {
        case .compact: 18
        case .medium: 22
        case .large: 26
        }
    }
}
