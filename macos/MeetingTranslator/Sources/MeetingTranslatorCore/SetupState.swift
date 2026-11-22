import Foundation

/// Where first-run model setup stands, as the server reports it in
/// `/api/status` and `SetupStateEvent`. Parsed once here so no view handles raw JSON.
public enum SetupState: Equatable, Sendable {
    case needsDownload(totalBytes: Int64, freeDiskBytes: Int64)
    case downloading(Progress)
    case loading
    case ready
    case failed(Failure)

    public struct Progress: Equatable, Sendable {
        public var doneBytes: Int64
        public var totalBytes: Int64
        public var bytesPerSecond: Double
        /// Nil while the server has no stable speed estimate yet.
        public var etaSeconds: Double?

        public init(doneBytes: Int64, totalBytes: Int64, bytesPerSecond: Double, etaSeconds: Double?) {
            self.doneBytes = doneBytes
            self.totalBytes = totalBytes
            self.bytesPerSecond = bytesPerSecond
            self.etaSeconds = etaSeconds
        }

        public var fraction: Double {
            totalBytes > 0 ? min(1, max(0, Double(doneBytes) / Double(totalBytes))) : 0
        }

        public var speedText: String? {
            bytesPerSecond > 0 ? "\(SetupState.byteText(Int64(bytesPerSecond)))/s" : nil
        }

        /// Nil when the server has no estimate, so the view omits the phrase.
        public var etaText: String? {
            guard let seconds = etaSeconds, seconds.isFinite, seconds >= 0 else { return nil }
            if seconds < 45 { return "less than a minute left" }
            let minutes = Int((seconds / 60).rounded(.up))
            if minutes < 90 { return "about \(minutes) min left" }
            return "about \(minutes / 60) hr \(minutes % 60) min left"
        }
    }

    public static func byteText(_ bytes: Int64) -> String {
        ByteCountFormatter.string(fromByteCount: bytes, countStyle: .decimal)
    }

    public struct Failure: Equatable, Sendable {
        public enum Reason: String, Equatable, Sendable {
            case offline
            case diskFull = "disk_full"
            case error
        }

        public var reason: Reason
        public var message: String

        public init(reason: Reason, message: String) {
            self.reason = reason
            self.message = message
        }
    }

    public var isReady: Bool {
        if case .ready = self { return true }
        return false
    }
}

extension SetupState: Codable {
    private enum CodingKeys: String, CodingKey {
        case phase
        case totalBytes = "total_bytes"
        case freeDiskBytes = "free_disk_bytes"
        case doneBytes = "done_bytes"
        case bytesPerSecond = "bytes_per_s"
        case etaSeconds = "eta_s"
        case reason, message
    }

    /// A phase this build does not know reads as `.loading`: a spinner is safe, a crash is not.
    public init(from decoder: Decoder) throws {
        let c = try decoder.container(keyedBy: CodingKeys.self)
        switch try c.decode(String.self, forKey: .phase) {
        case "needs_download":
            self = .needsDownload(
                totalBytes: try c.decode(Int64.self, forKey: .totalBytes),
                freeDiskBytes: try c.decodeIfPresent(Int64.self, forKey: .freeDiskBytes) ?? 0
            )
        case "downloading":
            self = .downloading(
                Progress(
                    doneBytes: try c.decode(Int64.self, forKey: .doneBytes),
                    totalBytes: try c.decode(Int64.self, forKey: .totalBytes),
                    bytesPerSecond: try c.decodeIfPresent(Double.self, forKey: .bytesPerSecond) ?? 0,
                    etaSeconds: try c.decodeIfPresent(Double.self, forKey: .etaSeconds)
                ))
        case "ready":
            self = .ready
        case "failed":
            let raw = try c.decodeIfPresent(String.self, forKey: .reason) ?? ""
            self = .failed(
                Failure(
                    reason: Failure.Reason(rawValue: raw) ?? .error,
                    message: try c.decodeIfPresent(String.self, forKey: .message) ?? ""
                ))
        default:
            self = .loading
        }
    }

    public func encode(to encoder: Encoder) throws {
        var c = encoder.container(keyedBy: CodingKeys.self)
        switch self {
        case .needsDownload(let total, let free):
            try c.encode("needs_download", forKey: .phase)
            try c.encode(total, forKey: .totalBytes)
            try c.encode(free, forKey: .freeDiskBytes)
        case .downloading(let progress):
            try c.encode("downloading", forKey: .phase)
            try c.encode(progress.doneBytes, forKey: .doneBytes)
            try c.encode(progress.totalBytes, forKey: .totalBytes)
            try c.encode(progress.bytesPerSecond, forKey: .bytesPerSecond)
            try c.encode(progress.etaSeconds, forKey: .etaSeconds)
        case .loading:
            try c.encode("loading", forKey: .phase)
        case .ready:
            try c.encode("ready", forKey: .phase)
        case .failed(let failure):
            try c.encode("failed", forKey: .phase)
            try c.encode(failure.reason.rawValue, forKey: .reason)
            try c.encode(failure.message, forKey: .message)
        }
    }
}

public struct SetupResponse: Codable, Equatable, Sendable {
    public var setup: SetupState
}

/// Decides whether a started capture is delivering sound, so a revoked
/// permission shows up as a message instead of a silent "Listening".
public enum CaptureSilence {
    public static let grace: Duration = .seconds(5)
    /// Peak audio level (0...1) below which a capture counts as silent.
    public static let threshold = 0.002

    public static func isSilent(peakLevel: Double) -> Bool {
        peakLevel < threshold
    }
}
