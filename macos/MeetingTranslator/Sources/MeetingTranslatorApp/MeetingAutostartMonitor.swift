import Foundation
import MeetingTranslatorCore
@preconcurrency import UserNotifications

/// Polls Core Audio process metadata every 2 s and offers
/// "Start captions for <App>?" through a notification.
@MainActor
final class MeetingAutostartMonitor: NSObject, UNUserNotificationCenterDelegate {
    nonisolated private static let categoryID = "MIMIVOX_MEETING_AUTOSTART"
    nonisolated private static let startActionID = "MIMIVOX_START_CAPTIONS"

    private weak var store: TranslatorStore?
    private var detector = MeetingAutostartDetector()
    private var timer: Timer?
    private var center: UNUserNotificationCenter?

    init(store: TranslatorStore) {
        self.store = store
    }

    func start() {
        guard #available(macOS 14.2, *), timer == nil else { return }
        // Notifications need an app bundle; `swift run` has none.
        if Bundle.main.bundleIdentifier != nil {
            let center = UNUserNotificationCenter.current()
            center.delegate = self
            center.setNotificationCategories([
                UNNotificationCategory(
                    identifier: Self.categoryID,
                    actions: [UNNotificationAction(identifier: Self.startActionID, title: "Start captions")],
                    intentIdentifiers: []
                )
            ])
            self.center = center
        }
        timer = Timer.scheduledTimer(withTimeInterval: 2, repeats: true) { [weak self] _ in
            MainActor.assumeIsolated { self?.tick() }
        }
    }

    private func tick() {
        guard #available(macOS 14.2, *), let store, store.isSetupReady else { return }
        // A BlackHole/Loopback input cannot be scoped to one app, so do not offer.
        guard store.usesNativeSystemAudio else { return }
        let offers = detector.ingest(
            snapshot: CoreAudioProcesses.snapshot(),
            now: ProcessInfo.processInfo.systemUptime,
            prefs: store.autostartPrefs,
            sessionActive: store.isSessionActive || store.isBusy
        )
        for app in offers {
            post(app)
        }
    }

    /// The first detected meeting is also the first time macOS is asked for
    /// notification permission; the offer is posted only if it is granted.
    private func post(_ app: MeetingApp) {
        guard let center else { return }
        Task {
            let settings = await center.notificationSettings()
            if settings.authorizationStatus == .notDetermined {
                guard (try? await center.requestAuthorization(options: [.alert, .sound])) == true else { return }
            } else if settings.authorizationStatus == .denied {
                return
            }
            let content = UNMutableNotificationContent()
            content.title = "Start captions for \(app.name)?"
            content.body = "MimiVox will caption only \(app.name)'s audio."
            content.categoryIdentifier = Self.categoryID
            content.userInfo = ["appID": app.id]
            try? await center.add(
                UNNotificationRequest(identifier: "meeting-autostart-\(app.id)", content: content, trigger: nil))
        }
    }

    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        willPresent notification: UNNotification
    ) async -> UNNotificationPresentationOptions {
        [.banner, .sound]
    }

    /// Clicking the banner or its action starts captions; dismissing does nothing.
    nonisolated func userNotificationCenter(
        _ center: UNUserNotificationCenter,
        didReceive response: UNNotificationResponse
    ) async {
        guard
            response.actionIdentifier == Self.startActionID
                || response.actionIdentifier == UNNotificationDefaultActionIdentifier,
            let appID = response.notification.request.content.userInfo["appID"] as? String,
            let app = MeetingApp.app(id: appID)
        else { return }
        await MainActor.run {
            guard let store, !store.isSessionActive else { return }
            Task { await store.startLiveSession(scopedTo: app) }
        }
    }
}
