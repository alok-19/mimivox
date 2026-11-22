import AppKit
import MeetingTranslatorCore
import SwiftUI

struct Banner: Equatable {
    enum Tone: Equatable {
        case info, success, warning, error
    }

    enum Action: Hashable {
        case retry
        case catchUp
        case openLive
        case openPrivacySettings(PrivacyPane)
        case showInFinder(String)
        case showLog(URL)
    }

    var tone: Tone
    var message: String
    var actions: [Action] = []
    var isDismissible = false

    static func resolve(
        status: LiveStatus,
        failure: String?,
        logURL: URL?,
        notice: Notice?,
        latency: LatencyHealth,
        setup: SetupState?,
        catchUpMode: CatchUpMode,
        onLive: Bool,
        showsSetupPrompt: Bool
    ) -> Banner? {
        if status == .failed {
            let log = logURL.map { [Action.showLog($0)] } ?? []
            return Banner(
                tone: .error, message: failure ?? L10n.text("Captioning ran into a problem."),
                actions: [.retry] + log)
        }
        if let notice, notice.kind == .error || notice.kind == .warning {
            return Banner(notice)
        }
        if onLive, let lag = Banner.latency(latency, mode: catchUpMode) {
            return lag
        }
        if let notice { return Banner(notice) }
        if showsSetupPrompt, !onLive, let setup, !setup.isReady {
            return Banner(tone: setup.isFailed ? .warning : .info, message: setup.bannerText, actions: [.openLive])
        }
        return nil
    }

    static func latency(_ health: LatencyHealth, mode: CatchUpMode) -> Banner? {
        guard !health.isOK || health.catchingUp else { return nil }
        let tone: Tone =
            switch (health.state, health.catchingUp || health.isNotesPause) {
            case (.overloaded, _): .error
            case (_, true): .info
            default: .warning
            }
        let message =
            health.catchingUp
            ? L10n.text("Catching up: translating only the newest lines.")
            : (health.reasonText ?? L10n.text("Captions are behind the speaker."))
        let canCatchUp = health.catchUpAvailable && mode != .off
        return Banner(tone: tone, message: message, actions: canCatchUp ? [.catchUp] : [])
    }

    init(tone: Tone, message: String, actions: [Action] = [], isDismissible: Bool = false) {
        self.tone = tone
        self.message = message
        self.actions = actions
        self.isDismissible = isDismissible
    }

    init(_ notice: Notice) {
        var actions: [Action] = []
        if let path = notice.path { actions.append(.showInFinder(path)) }
        switch notice.recovery {
        case .retryServer: actions.append(.retry)
        case .openPrivacySettings(let pane): actions.append(.openPrivacySettings(pane))
        case nil: break
        }
        let tone: Tone =
            switch notice.kind {
            case .info: .info
            case .success: .success
            case .warning: .warning
            case .error: .error
            }
        self.init(tone: tone, message: L10n.text(notice.text), actions: actions, isDismissible: true)
    }
}

extension Banner.Tone {
    fileprivate var symbol: String {
        switch self {
        case .info: "info.circle"
        case .success: "checkmark.circle"
        case .warning: "exclamationmark.triangle"
        case .error: "exclamationmark.octagon"
        }
    }

    fileprivate var tint: Color {
        switch self {
        case .info: Palette.info
        case .success: Color.accentColor
        case .warning: Palette.warning
        case .error: Palette.error
        }
    }
}

extension Banner.Action {
    fileprivate var title: String {
        switch self {
        case .retry: L10n.text("Retry")
        case .catchUp: L10n.text("Catch up")
        case .openLive: L10n.text("Set Up")
        case .openPrivacySettings: L10n.text("Open System Settings")
        case .showInFinder: L10n.text("Show in Finder")
        case .showLog: L10n.text("Show Log")
        }
    }

    fileprivate var identifier: String {
        switch self {
        case .retry: "Retry"
        case .catchUp: "Catch up"
        case .openLive: "Set Up"
        case .openPrivacySettings: "Open System Settings"
        case .showInFinder: "Show in Finder"
        case .showLog: "Show Log"
        }
    }
}

struct BannerView: View {
    @EnvironmentObject private var store: TranslatorStore
    var banner: Banner
    var compact = false
    var openLive: () -> Void = {}

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 12) {
            Image(systemName: banner.tone.symbol)
                .foregroundStyle(banner.tone.tint)
            Text(banner.message)
                .font(compact ? .caption : .callout)
                .lineLimit(compact ? 2 : 3)
                .fixedSize(horizontal: false, vertical: true)
                .textSelection(.enabled)
            Spacer(minLength: 8)
            ForEach(banner.actions, id: \.self) { action in
                Button(action.title) { perform(action) }
                    .buttonStyle(.bordered)
                    .controlSize(.small)
                    .disabled(action == .catchUp && store.isBusy)
                    .accessibilityIdentifier(action == .catchUp ? "catch-up-button" : "banner-\(action.identifier)")
            }
            if banner.isDismissible {
                Button {
                    store.dismissNotice()
                } label: {
                    Label(L10n.text("Dismiss"), systemImage: "xmark")
                        .labelStyle(.iconOnly)
                }
                .buttonStyle(.borderless)
                .help(L10n.text("Dismiss"))
            }
        }
        .padding(.horizontal, compact ? 8 : 16)
        .padding(.vertical, compact ? 4 : 12)
        .frame(maxWidth: compact ? .infinity : 560, alignment: .leading)
        .background {
            if compact {
                banner.tone.tint.opacity(0.18)
            } else {
                // A floating toast: material card, hairline edge, soft offset shadow.
                ZStack {
                    Rectangle().fill(.regularMaterial)
                    banner.tone.tint.opacity(0.06)
                }
            }
        }
        .clipShape(RoundedRectangle(cornerRadius: compact ? 8 : 14, style: .continuous))
        .overlay {
            if !compact {
                RoundedRectangle(cornerRadius: 14, style: .continuous)
                    .strokeBorder(Color.primary.opacity(0.08))
            }
        }
        .shadow(color: .black.opacity(compact ? 0 : 0.14), radius: 12, y: 4)
        .accessibilityElement(children: .contain)
        .accessibilityIdentifier("banner")
    }

    private func perform(_ action: Banner.Action) {
        switch action {
        case .retry: Task { await store.retryHelper() }
        case .catchUp: Task { await store.catchUp() }
        case .openLive: openLive()
        case .openPrivacySettings(let pane): NSWorkspace.shared.open(pane.url)
        case .showInFinder(let path): NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: path)])
        case .showLog(let url): NSWorkspace.shared.activateFileViewerSelecting([url])
        }
    }
}

/// The window's toast: floats at the bottom of the detail area, and the content
/// above makes room for it, so it never covers a control and can be left alone.
struct BannerSlot: View {
    @EnvironmentObject private var store: TranslatorStore
    var onLive: Bool
    var openLive: () -> Void
    var showsSetupPrompt = true

    var body: some View {
        let banner = store.banner(onLive: onLive, showsSetupPrompt: showsSetupPrompt)
        ZStack {
            if let banner {
                BannerView(banner: banner, openLive: openLive)
                    .padding(.horizontal, 16)
                    .padding(.bottom, 16)
                    .transition(.move(edge: .bottom).combined(with: .opacity))
            }
        }
        .frame(maxWidth: .infinity)
        .animation(.spring(duration: 0.35, bounce: 0.15), value: banner)
    }
}

extension TranslatorStore {
    func banner(onLive: Bool, showsSetupPrompt: Bool = true) -> Banner? {
        Banner.resolve(
            status: liveStatus,
            failure: failureDetail,
            logURL: helperLogURL,
            notice: notice,
            latency: latencyHealth,
            setup: setup,
            catchUpMode: CatchUpMode(rawValue: settings.catchUpMode) ?? .manual,
            onLive: onLive,
            showsSetupPrompt: showsSetupPrompt
        )
    }
}

extension SetupState {
    fileprivate var isFailed: Bool {
        if case .failed = self { true } else { false }
    }

    fileprivate var bannerText: String {
        switch self {
        case .needsDownload: L10n.text("MimiVox needs to download its speech models before it can translate.")
        case .downloading(let progress):
            String(
                format: L10n.text("Downloading speech models, %d%% done."),
                Int((progress.fraction * 100).rounded()))
        case .loading: L10n.text("Loading the speech models…")
        case .failed: L10n.text("The speech model download didn't finish. Open Live to try again.")
        case .ready: ""
        }
    }
}
