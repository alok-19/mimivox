import Foundation

/// A call app MimiVox can offer to caption. `bundlePrefixes` match a process's
/// bundle ID exactly or as a dotted prefix, so helper processes
/// ("com.google.Chrome.helper") count as their parent app.
public struct MeetingApp: Equatable, Sendable, Identifiable {
    public let id: String
    public let name: String
    public let bundlePrefixes: [String]
    /// Browsers play every tab's audio through one process, so output alone
    /// cannot tell a call from a video. Require the mic to be open too.
    public let requiresMicInput: Bool

    public static let catalog: [MeetingApp] = [
        MeetingApp(id: "zoom", name: "Zoom", bundlePrefixes: ["us.zoom"], requiresMicInput: false),
        MeetingApp(
            id: "teams", name: "Microsoft Teams", bundlePrefixes: ["com.microsoft.teams2", "com.microsoft.teams"],
            requiresMicInput: false),
        MeetingApp(
            id: "webex", name: "Webex",
            bundlePrefixes: [
                "Cisco-Systems.Spark", "com.cisco.webexmeetingsapp", "com.webex.meetingmanager", "com.cisco.webex",
            ], requiresMicInput: false),
        MeetingApp(id: "slack", name: "Slack", bundlePrefixes: ["com.tinyspeck.slackmacgap"], requiresMicInput: false),
        MeetingApp(
            id: "facetime", name: "FaceTime", bundlePrefixes: ["com.apple.FaceTime", "com.apple.avconferenced"],
            requiresMicInput: false),
        MeetingApp(id: "chrome", name: "Chrome", bundlePrefixes: ["com.google.Chrome"], requiresMicInput: true),
        // Safari's tabs play through the shared WebKit GPU process.
        MeetingApp(
            id: "safari", name: "Safari", bundlePrefixes: ["com.apple.Safari", "com.apple.WebKit.GPU"],
            requiresMicInput: true),
        MeetingApp(id: "arc", name: "Arc", bundlePrefixes: ["company.thebrowser.Browser"], requiresMicInput: true),
        MeetingApp(id: "edge", name: "Edge", bundlePrefixes: ["com.microsoft.edgemac"], requiresMicInput: true),
        MeetingApp(id: "brave", name: "Brave", bundlePrefixes: ["com.brave.Browser"], requiresMicInput: true),
    ]

    public static func app(id: String) -> MeetingApp? {
        catalog.first { $0.id == id }
    }

    public static func app(forBundleID bundleID: String) -> MeetingApp? {
        catalog.first { $0.matches(bundleID: bundleID) }
    }

    public func matches(bundleID: String) -> Bool {
        bundlePrefixes.contains { Self.hasBoundaryPrefix(bundleID, $0) }
    }

    static func hasBoundaryPrefix(_ bundleID: String, _ prefix: String) -> Bool {
        bundleID == prefix || bundleID.hasPrefix(prefix + ".")
    }
}

/// Metadata about one Core Audio process object. Never contains audio.
public struct AudioProcessInfo: Equatable, Sendable {
    public var objectID: UInt32
    public var pid: Int32
    public var bundleID: String
    public var isRunningOutput: Bool
    public var isRunningInput: Bool

    public init(
        objectID: UInt32 = 0, pid: Int32 = 0, bundleID: String, isRunningOutput: Bool, isRunningInput: Bool = false
    ) {
        self.objectID = objectID
        self.pid = pid
        self.bundleID = bundleID
        self.isRunningOutput = isRunningOutput
        self.isRunningInput = isRunningInput
    }
}

/// How a capture key (a catalog app id, or a raw bundle ID the user typed)
/// matches a process.
public enum CaptureKey {
    public static func matches(_ key: String, bundleID: String) -> Bool {
        if let app = MeetingApp.app(id: key) {
            return app.matches(bundleID: bundleID)
        }
        return MeetingApp.hasBoundaryPrefix(bundleID, key)
    }
}

public enum AppCaptureRule: String, Codable, Equatable, Sendable {
    /// Capture only the apps marked include.
    case include
    /// Never capture this app ("Never capture" list), and never prompt for it.
    case exclude
}

/// Which processes a system-audio tap covers.
public enum CaptureScope: Equatable, Sendable {
    case everything(excluding: [String])
    case only([String])
}

/// User preferences for meeting detection and capture scoping. Persisted by the app.
public struct MeetingAutostartPrefs: Codable, Equatable, Sendable {
    /// On by default; macOS is asked for notifications when the first meeting is detected.
    public var detectionEnabled: Bool = true
    /// Catalog app ids that are NOT offered captions (all are offered by default).
    public var mutedPromptApps: Set<String> = []
    public var snoozedUntil: TimeInterval?
    /// Keyed by catalog app id or raw bundle ID. Absent means "default".
    public var rules: [String: AppCaptureRule] = [:]

    public init() {}

    public func excludedKeys() -> [String] {
        rules.filter { $0.value == .exclude }.map(\.key).sorted()
    }

    public func includedKeys() -> [String] {
        rules.filter { $0.value == .include }.map(\.key).sorted()
    }

    public func isExcluded(_ app: MeetingApp) -> Bool {
        rules[app.id] == .exclude
    }

    /// The scope for a session. With `app`, only that app's audio (a prompt's
    /// "Start captions for <App>?"); otherwise the user's include/exclude rules.
    public func scope(forApp app: MeetingApp? = nil) -> CaptureScope {
        if let app {
            return .only(isExcluded(app) ? [] : [app.id])
        }
        let included = includedKeys().filter { rules[$0] != .exclude }
        return included.isEmpty ? .everything(excluding: excludedKeys()) : .only(included)
    }
}

/// Decides when to offer "Start captions for <App>?". Pure: feed it
/// snapshots and a clock, it returns apps to prompt for.
public struct MeetingAutostartDetector: Sendable {
    public static let persistence: TimeInterval = 3
    public static let cooldown: TimeInterval = 10 * 60

    private var audibleSince: [String: TimeInterval] = [:]
    private var prompted: Set<String> = []
    private var cooldownUntil: [String: TimeInterval] = [:]

    public init() {}

    /// - Returns: apps to prompt for on this tick (usually empty).
    public mutating func ingest(
        snapshot: [AudioProcessInfo],
        now: TimeInterval,
        prefs: MeetingAutostartPrefs,
        sessionActive: Bool
    ) -> [MeetingApp] {
        let live = Self.liveApps(in: snapshot)
        // Audio that stopped (or a session that began) restarts the 3 s clock.
        audibleSince = audibleSince.filter { live.contains($0.key) && !sessionActive }
        prompted.formIntersection(live)
        guard !sessionActive else { return [] }

        var offers: [MeetingApp] = []
        for app in MeetingApp.catalog where live.contains(app.id) {
            let start = audibleSince[app.id] ?? now
            audibleSince[app.id] = start
            guard prefs.detectionEnabled,
                !prefs.mutedPromptApps.contains(app.id),
                !prefs.isExcluded(app),
                prefs.snoozedUntil.map({ now >= $0 }) ?? true,
                now - start >= Self.persistence,
                !prompted.contains(app.id),
                now >= (cooldownUntil[app.id] ?? -.infinity)
            else { continue }
            prompted.insert(app.id)
            // An ignored banner gives no callback, so a prompt counts as dismissed.
            cooldownUntil[app.id] = now + Self.cooldown
            offers.append(app)
        }
        return offers
    }

    private static func liveApps(in snapshot: [AudioProcessInfo]) -> Set<String> {
        var live = Set<String>()
        for process in snapshot where process.isRunningOutput {
            guard let app = MeetingApp.app(forBundleID: process.bundleID) else { continue }
            if app.requiresMicInput
                && !snapshot.contains(where: { $0.isRunningInput && app.matches(bundleID: $0.bundleID) })
            {
                continue
            }
            live.insert(app.id)
        }
        return live
    }
}

extension MeetingAutostartPrefs {
    private static let defaultsKey = "MimiVoxMeetingAutostartPrefs"

    public static func load(from defaults: UserDefaults = .standard) -> MeetingAutostartPrefs {
        defaults.data(forKey: defaultsKey)
            .flatMap { try? JSONDecoder().decode(MeetingAutostartPrefs.self, from: $0) }
            ?? MeetingAutostartPrefs()
    }

    public func save(to defaults: UserDefaults = .standard) {
        if let data = try? JSONEncoder().encode(self) {
            defaults.set(data, forKey: Self.defaultsKey)
        }
    }
}
