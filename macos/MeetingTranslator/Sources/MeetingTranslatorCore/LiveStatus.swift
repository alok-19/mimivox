import Foundation

public enum LiveStatus: Equatable, Sendable {
    case starting
    case setupNeeded
    case idle
    case live
    case paused
    case stopping
    case catchingUp
    case behind(seconds: Int)
    case failed

    private static let activeStates: Set<String> = [
        "preparing", "loading_models", "listening", "running", "processing", "paused", "stopping",
    ]

    public init(helper: HelperPhase, setup: SetupState?, status: MeetingStatusResponse?, latency: LatencyHealth) {
        if case .failed = helper {
            self = .failed
            return
        }
        if case .starting = helper {
            self = .starting
            return
        }
        if let setup, !setup.isReady {
            if case .loading = setup { self = .starting } else { self = .setupNeeded }
            return
        }
        guard let status else {
            self = .starting
            return
        }
        let hasError = status.error?.isEmpty == false
        if status.health.degraded || ["failed", "degraded"].contains(status.state)
            || (hasError && Self.activeStates.contains(status.state))
        {
            self = .failed
            return
        }
        switch status.state {
        case "preparing", "loading_models":
            self = .starting
        case "stopping":
            self = .stopping
        case "paused":
            self = .paused
        case "listening", "running", "processing":
            if latency.catchingUp {
                self = .catchingUp
            } else if !latency.isOK {
                self = .behind(seconds: Int(latency.backlogSeconds.rounded()))
            } else if status.state == "processing" || status.health.audioInputActive {
                self = .live
            } else {
                self = .starting
            }
        default:
            self = .idle
        }
    }

    public var showsToolbarPill: Bool {
        switch self {
        case .idle, .setupNeeded: false
        case .starting, .live, .paused, .stopping, .catchingUp, .behind, .failed: true
        }
    }

    public var title: String {
        switch self {
        case .starting: "Starting…"
        case .setupNeeded: "Setup"
        case .idle: "Idle"
        case .live: "Live"
        case .paused: "Paused"
        case .stopping: "Stopping…"
        case .catchingUp: "Catching up"
        case .behind(let seconds): "Behind \(seconds) s"
        case .failed: "Failed"
        }
    }
}
