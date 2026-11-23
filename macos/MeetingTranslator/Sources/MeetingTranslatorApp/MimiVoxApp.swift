import AppKit
import MeetingTranslatorCore
import Sparkle
import SwiftUI

struct MimiVoxApp: App {
    @NSApplicationDelegateAdaptor(AppDelegate.self) private var delegate
    @AppStorage(AppLanguage.defaultsKey) private var appLanguage: AppLanguage = .system

    var body: some Scene {
        Window("MimiVox", id: MainWindow.id) {
            MainWindowRoot(model: delegate.model)
        }
        .defaultSize(width: MainWindow.defaultSize.width, height: MainWindow.defaultSize.height)
        .windowResizability(.contentMinSize)
        .commands {
            AppCommands(model: delegate.model, language: appLanguage)
            InfoCommands(updaterController: delegate.updaterController)
        }

        Window(InfoWindow.about.title, id: InfoWindow.about.id) { AboutView() }
            .windowResizability(.contentSize)
        Window(InfoWindow.help.title, id: InfoWindow.help.id) { HelpView() }
            .defaultSize(width: 620, height: 720)
        Window(InfoWindow.changelog.title, id: InfoWindow.changelog.id) { ChangelogView() }
            .defaultSize(width: 560, height: 640)

        Settings {
            SettingsRoot(model: delegate.model)
                .environment(\.locale, appLanguage.locale)
        }

        MenuBarExtra("MimiVox", systemImage: "captions.bubble") {
            MenuBarContent(model: delegate.model, language: appLanguage)
        }
    }
}

enum AppearanceOverride {
    static let colorScheme: ColorScheme? =
        switch ProcessInfo.processInfo.environment["MIMIVOX_APPEARANCE"] {
        case "dark": .dark
        case "light": .light
        default: nil
        }
}

enum MainWindow {
    static let id = "main"
    static let frameAutosaveName = "MimiVoxMainWindow"
    static let defaultSize = CGSize(width: 1000, height: 680)
    static let minSize = CGSize(width: 720, height: 480)
}

@MainActor
final class AppDelegate: NSObject, NSApplicationDelegate {
    let model = AppModel()
    let updaterController: SPUStandardUpdaterController? =
        SparkleUpdateConfiguration.isAvailable
        ? SPUStandardUpdaterController(startingUpdater: true, updaterDelegate: nil, userDriverDelegate: nil)
        : nil

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        model.start()
    }

    private var isQuitting = false

    func applicationShouldTerminate(_ sender: NSApplication) -> NSApplication.TerminateReply {
        // A second Cmd+Q while the first is still stopping the helper.
        guard !isQuitting else { return .terminateCancel }
        let store = model.store
        let prompt = QuitConfirmation.prompt(
            meetingActive: store.hasActiveSession,
            paused: store.isSessionPaused,
            idleSuppressed: UserDefaults.standard.bool(forKey: QuitConfirmation.idleSuppressedKey)
        )
        var endMeeting = false
        switch prompt {
        case nil:
            break
        case .idle:
            guard QuitAlert.confirmIdleQuit() else { return .terminateCancel }
        case .activeMeeting(let paused):
            switch QuitAlert.askAboutMeeting(paused: paused) {
            case .endAndQuit:
                endMeeting = true
            case .endOnly:
                Task { await store.stopSession() }
                return .terminateCancel
            case .cancel:
                return .terminateCancel
            }
        }
        isQuitting = true
        QuitProgress.terminate(sender, stopping: store, endingMeeting: endMeeting)
        return .terminateLater
    }

    func applicationWillTerminate(_ notification: Notification) {
        model.store.stopHelper()  // already stopped on a normal quit; this catches the rest
    }

    func applicationShouldTerminateAfterLastWindowClosed(_ sender: NSApplication) -> Bool {
        false
    }
}

/// Sparkle requires a feed and its EdDSA key. Unconfigured SwiftPM and local builds stay usable.
private enum SparkleUpdateConfiguration {
    static var isAvailable: Bool {
        guard
            let feedURL = Bundle.main.object(forInfoDictionaryKey: "SUFeedURL") as? String,
            !feedURL.isEmpty,
            let publicKey = Bundle.main.object(forInfoDictionaryKey: "SUPublicEDKey") as? String,
            !publicKey.isEmpty
        else {
            return false
        }
        return true
    }
}

private struct MainWindowRoot: View {
    @ObservedObject var model: AppModel
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        RootView(
            selection: $model.selectedSection, quickTranslate: model.quickTranslate,
            meetingsSearch: model.meetingsSearch
        )
        .environmentObject(model.store)
        .environment(
            \.floatingCaptions,
            FloatingCaptionsControl(isShown: model.showsCaptionPanel, toggle: model.toggleCaptionPanel)
        )
        .environment(\.selectSettingsTab) { model.settingsTab = $0 }
        .frame(minWidth: MainWindow.minSize.width, minHeight: MainWindow.minSize.height)
        .preferredColorScheme(AppearanceOverride.colorScheme)
        .background(WindowFrameAutosave(name: MainWindow.frameAutosaveName))
        .task {
            if model.takeSettingsLaunch() { openSettings() }
        }
    }
}

/// SwiftUI does not name a `Window` scene's frame for autosave, so the NSWindow gets one here.
private struct WindowFrameAutosave: NSViewRepresentable {
    let name: String

    func makeNSView(context: Context) -> NSView {
        let view = NSView()
        DispatchQueue.main.async { [weak view] in
            view?.window?.setFrameAutosaveName(name)
        }
        return view
    }

    func updateNSView(_ nsView: NSView, context: Context) {}
}

private struct AppCommands: Commands {
    @ObservedObject var model: AppModel
    let language: AppLanguage
    @Environment(\.openWindow) private var openWindow

    var body: some Commands {
        CommandGroup(before: .windowArrangement) {
            // A lone Window scene has no File menu, so ⌘W needs its own item.
            Button(L10n.text("Close", language: language)) { NSApp.keyWindow?.performClose(nil) }
                .keyboardShortcut("w", modifiers: .command)
        }
        CommandMenu(L10n.text("Session", language: language)) {
            Button(L10n.text("Start Captions", language: language)) { model.startCaptions() }
                .keyboardShortcut("r", modifiers: .command)
                .disabled(!model.canStartCaptions)
            Button(L10n.text("Stop Captions", language: language)) { model.stopCaptions() }
                .keyboardShortcut(".", modifiers: .command)
                .disabled(!model.canStopCaptions)
            Button(L10n.text(model.captionsPaused ? "Resume Captions" : "Pause Captions", language: language)) {
                model.togglePauseCaptions()
            }
            .keyboardShortcut("p", modifiers: .command)
            .disabled(!model.canPauseCaptions)
            Divider()
            Button(L10n.text("Toggle Floating Captions", language: language)) { model.toggleCaptionPanel() }
                .keyboardShortcut("c", modifiers: [.command, .shift])
        }
        CommandGroup(after: .sidebar) {
            ForEach(AppSection.allCases) { section in
                Button(section.title) {
                    model.selectedSection = section
                    openWindow(id: MainWindow.id)
                }
                .keyboardShortcut(section.shortcut, modifiers: .command)
            }
        }
    }
}

private struct MenuBarContent: View {
    @ObservedObject var model: AppModel
    let language: AppLanguage
    @Environment(\.openWindow) private var openWindow
    @Environment(\.openSettings) private var openSettings

    var body: some View {
        Button(L10n.text("Open MimiVox", language: language)) {
            openWindow(id: MainWindow.id)
            NSApp.activate(ignoringOtherApps: true)
        }
        Divider()
        Button(L10n.text("Start Captions", language: language)) { model.startCaptions() }
            .disabled(!model.canStartCaptions)
        Button(L10n.text("Stop Captions", language: language)) { model.stopCaptions() }
            .disabled(!model.canStopCaptions)
        Button(L10n.text("Toggle Floating Captions", language: language)) { model.toggleCaptionPanel() }
        Divider()
        Button(L10n.text("Settings…", language: language)) {
            openSettings()
            NSApp.activate(ignoringOtherApps: true)
        }
        Divider()
        Button(L10n.text("Quit MimiVox", language: language)) { NSApp.terminate(nil) }
            .keyboardShortcut("q", modifiers: .command)
    }
}
