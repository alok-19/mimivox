import Darwin
import Foundation

/// Why the helper server could not be started or stopped running.
public enum HelperFailure: Equatable, Sendable {
    /// The interpreter could not be launched at all.
    case launch(String)
    /// The server process exited; `stderrTail` is the end of its log output.
    case exited(code: Int32, stderrTail: String)
    /// The server could not bind its port.
    case portInUse(port: Int)
    /// Something that is not MimiVox answers on the pinned port.
    case foreignServer(port: Int)

    /// One short line for the UI.
    public var summary: String {
        switch self {
        case .launch(let reason):
            return "Could not launch the server: \(reason)"
        case .exited(let code, let tail):
            let lastLine = tail.split(separator: "\n").last.map(String.init) ?? ""
            let detail = lastLine.isEmpty ? "" : ": \(lastLine.prefix(140))"
            return "Server exited with code \(code)\(detail)"
        case .portInUse(let port):
            return "Port \(port) is already in use"
        case .foreignServer(let port):
            return "Port \(port) is used by another program, not MimiVox"
        }
    }
}

public enum HelperPhase: Equatable, Sendable {
    case idle
    case starting(port: Int)
    case ready(port: Int)
    case failed(HelperFailure)
}

/// What answers on a port.
public enum HelperProbeResult: Equatable, Sendable {
    /// `/api/status` decoded as a MimiVox status.
    case mimivox
    /// Something answered HTTP, but not as MimiVox.
    case foreign
    /// Nothing answered.
    case unreachable
}

/// Owns the helper server process and its lifecycle phase.
///
/// `start()` converges to `ready` or `failed`: it reuses a MimiVox server on a
/// pinned port, otherwise launches one and waits for as long as the process
/// lives. Under `.automatic` it retries on a new free port when the bind fails.
public final class HelperSupervisor: @unchecked Sendable {
    public static let maxPortAttempts = 3
    static let maxLogBytes = 5 * 1024 * 1024

    public let configuration: HelperConfiguration
    /// Called (on an arbitrary thread) after every phase change.
    public var onPhaseChange: (@Sendable (HelperPhase) -> Void)?
    /// Read at every launch and merged over the configured environment, so a
    /// setting changed since the last start reaches the next helper.
    public var launchEnvironment: (@Sendable () -> [String: String])?

    private let lock = NSLock()
    private var _phase: HelperPhase = .idle
    private var process: Process?
    private var nextPort: Int
    private let probe: @Sendable (URL) async -> HelperProbeResult
    private let pickPort: @Sendable () -> Int?
    private let parentPID: Int32
    private let pollInterval: Duration

    public init(
        configuration: HelperConfiguration,
        probe: @escaping @Sendable (URL) async -> HelperProbeResult = {
            await HelperSupervisor.probeStatus(baseURL: $0)
        },
        pickPort: @escaping @Sendable () -> Int? = HelperSupervisor.freeLoopbackPort,
        parentPID: Int32 = getpid(),
        pollInterval: Duration = .milliseconds(300)
    ) {
        self.configuration = configuration
        self.probe = probe
        self.pickPort = pickPort
        self.parentPID = parentPID
        self.pollInterval = pollInterval
        switch configuration.portPolicy {
        case .fixed(let port):
            nextPort = port
        case .automatic:
            nextPort = pickPort() ?? 0
        }
    }

    public var phase: HelperPhase {
        lock.withLock { _phase }
    }

    /// The port the next start uses, so API clients can be built before it.
    public var plannedPort: Int {
        lock.withLock { nextPort }
    }

    /// Converges to `.ready` or `.failed`. Restarts a failed or stopped helper;
    /// a ready one is left alone.
    public func start() async -> HelperPhase {
        if case .ready = phase {
            return phase
        }
        stop()
        switch configuration.portPolicy {
        case .fixed(let port):
            switch await probe(configuration.baseURL(port: port)) {
            case .mimivox:
                return setPhase(.ready(port: port))
            case .foreign:
                return setPhase(.failed(.foreignServer(port: port)))
            case .unreachable:
                return setPhase(await launch(port: port))
            }
        case .automatic:
            var result = HelperPhase.idle
            for attempt in 1...Self.maxPortAttempts {
                result = await launch(port: plannedPort)
                guard case .failed(.portInUse) = result, attempt < Self.maxPortAttempts else {
                    break
                }
                let fresh = pickPort() ?? 0
                lock.withLock { nextPort = fresh }
            }
            return setPhase(result)
        }
    }

    /// Terminates the process this supervisor owns. Idempotent.
    public func stop() {
        let owned: Process? = lock.withLock {
            let owned = process
            process = nil
            return owned
        }
        if let owned, owned.isRunning {
            owned.terminate()
            // Give uvicorn a moment to shut down cleanly, then force it.
            let deadline = Date().addingTimeInterval(3)
            while owned.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.05)
            }
            if owned.isRunning {
                kill(owned.processIdentifier, SIGKILL)
            }
        }
        setPhase(.idle)
    }

    public func makeProcess(port: Int) -> Process {
        let process = Process()
        process.executableURL = configuration.pythonExecutable
        process.arguments = configuration.helperArguments(port: port, parentPID: parentPID)
        process.currentDirectoryURL = configuration.projectRoot
        process.environment = configuration.helperEnvironment.merging(launchEnvironment?() ?? [:]) { $1 }
        return process
    }

    /// Launches on `port` and waits while the process lives. Returns `.ready`
    /// or `.failed`; never publishes the final phase itself.
    private func launch(port: Int) async -> HelperPhase {
        guard port > 0 else {
            return .failed(.launch("no free loopback port"))
        }
        setPhase(.starting(port: port))
        let process = makeProcess(port: port)
        let logOffset: UInt64
        do {
            let log = try openLog()
            logOffset = try log.seekToEnd()
            process.standardOutput = log
            process.standardError = log
            process.terminationHandler = { [weak self] exited in
                self?.processExited(exited, logOffset: logOffset)
            }
            try process.run()
            try? log.close()
        } catch {
            return .failed(.launch(error.localizedDescription))
        }
        lock.withLock { self.process = process }

        while process.isRunning {
            if Task.isCancelled {
                stop()
                return .idle
            }
            if await probe(configuration.baseURL(port: port)) == .mimivox {
                return .ready(port: port)
            }
            try? await Task.sleep(for: pollInterval)
        }
        lock.withLock {
            if self.process === process { self.process = nil }
        }
        return exitFailure(of: process, port: port, logOffset: logOffset)
    }

    /// A ready helper that dies is reported as failed.
    private func processExited(_ exited: Process, logOffset: UInt64) {
        let wasOwnedAndReady: Bool = lock.withLock {
            guard process === exited, case .ready = _phase else { return false }
            process = nil
            return true
        }
        if wasOwnedAndReady, case .ready(let port) = phase {
            setPhase(exitFailure(of: exited, port: port, logOffset: logOffset))
        }
    }

    private func exitFailure(of process: Process, port: Int, logOffset: UInt64) -> HelperPhase {
        let tail = Self.tail(of: configuration.logURL, from: logOffset)
        if tail.lowercased().contains("address already in use") {
            return .failed(.portInUse(port: port))
        }
        return .failed(.exited(code: process.terminationStatus, stderrTail: tail))
    }

    @discardableResult
    private func setPhase(_ next: HelperPhase) -> HelperPhase {
        lock.withLock { _phase = next }
        onPhaseChange?(next)
        return next
    }

    /// Opens the log for appending, truncating it first when it grew too large.
    private func openLog() throws -> FileHandle {
        let fm = FileManager.default
        let url = configuration.logURL
        try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        let size = (try? fm.attributesOfItem(atPath: url.path)[.size] as? Int) ?? 0
        if !fm.fileExists(atPath: url.path) || size > Self.maxLogBytes {
            fm.createFile(atPath: url.path, contents: nil)
        }
        return try FileHandle(forWritingTo: url)
    }

    /// Last few non-empty lines written to `url` after `offset`.
    static func tail(of url: URL, from offset: UInt64, lines: Int = 6) -> String {
        guard let handle = try? FileHandle(forReadingFrom: url) else { return "" }
        defer { try? handle.close() }
        let end = (try? handle.seekToEnd()) ?? offset
        let start = max(offset, end > 8192 ? end - 8192 : 0)
        try? handle.seek(toOffset: start)
        let data = (try? handle.readToEnd()) ?? Data()
        return String(decoding: data, as: UTF8.self)
            .split(separator: "\n")
            .map { $0.trimmingCharacters(in: .whitespaces) }
            .filter { !$0.isEmpty }
            .suffix(lines)
            .joined(separator: "\n")
    }

    public static func probeStatus(
        baseURL: URL,
        transport: any HTTPTransport = URLSession.shared
    ) async -> HelperProbeResult {
        do {
            _ = try await MeetingTranslatorAPIClient(baseURL: baseURL, transport: transport).status()
            return .mimivox
        } catch is URLError {
            return .unreachable
        } catch {
            return .foreign
        }
    }

    /// A port the kernel reports free on 127.0.0.1, or nil.
    public static func freeLoopbackPort() -> Int? {
        let fd = socket(AF_INET, SOCK_STREAM, 0)
        guard fd >= 0 else { return nil }
        defer { close(fd) }
        var addr = sockaddr_in()
        addr.sin_len = UInt8(MemoryLayout<sockaddr_in>.size)
        addr.sin_family = sa_family_t(AF_INET)
        addr.sin_port = 0
        addr.sin_addr.s_addr = inet_addr("127.0.0.1")
        var length = socklen_t(MemoryLayout<sockaddr_in>.size)
        let bound = withUnsafeMutablePointer(to: &addr) { pointer in
            pointer.withMemoryRebound(to: sockaddr.self, capacity: 1) { address in
                bind(fd, address, length) == 0 && getsockname(fd, address, &length) == 0
            }
        }
        return bound ? Int(UInt16(bigEndian: addr.sin_port)) : nil
    }
}
