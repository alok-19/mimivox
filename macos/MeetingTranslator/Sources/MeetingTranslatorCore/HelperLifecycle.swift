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
        }
    }
}

public enum HelperPhase: Equatable, Sendable {
    case idle
    case starting(port: Int)
    case ready(port: Int)
    case failed(HelperFailure)
}

/// Owns the helper server process and its lifecycle phase.
///
/// Launches a helper and checks its authenticated status. Automatic mode retries if a port is busy.
public final class HelperSupervisor: @unchecked Sendable {
    public static let maxPortAttempts = 3

    public let configuration: HelperConfiguration
    /// Called (on an arbitrary thread) after every phase change.
    public var onPhaseChange: (@Sendable (HelperPhase) -> Void)?
    /// Read at every launch and merged over the configured environment, so a
    /// setting changed since the last start reaches the next helper.
    public var launchEnvironment: (@Sendable () -> [String: String])?

    private let lock = NSLock()
    private var _phase: HelperPhase = .idle
    private var process: Process?
    private var connectedClient: MeetingTranslatorAPIClient?
    private var nextPort: Int
    private let probe: (@Sendable (URL) async -> Bool)?
    private let pickPort: @Sendable () -> Int?
    private let parentPID: Int32
    private let pollInterval: Duration

    public init(
        configuration: HelperConfiguration,
        probe: (@Sendable (URL) async -> Bool)? = nil,
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

    public var plannedPort: Int {
        lock.withLock { nextPort }
    }

    public var client: MeetingTranslatorAPIClient? {
        lock.withLock { connectedClient }
    }

    public func start() async -> HelperPhase {
        if case .ready = phase {
            return phase
        }
        stop()
        do {
            try resetLog()
        } catch {
            return setPhase(.failed(.launch(error.localizedDescription)))
        }
        let attempts = configuration.portPolicy == .automatic ? Self.maxPortAttempts : 1
        for attempt in 1...attempts {
            let result = await launch(port: plannedPort)
            if case .ready = result { return result }
            guard case .failed(.portInUse) = result, attempt < attempts else {
                return setPhase(result)
            }
            let fresh = pickPort() ?? 0
            lock.withLock { nextPort = fresh }
        }
        return setPhase(.idle)
    }

    /// Terminates the process this supervisor owns. Idempotent.
    public func stop() {
        let owned: Process? = lock.withLock {
            let owned = process
            process = nil
            connectedClient = nil
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
        let connection: HelperConnection
        do {
            connection = try HelperConnection()
        } catch {
            return .failed(.launch("Could not create the helper TLS identity. \(error.localizedDescription)"))
        }

        let client = connection.client(baseURL: configuration.baseURL(port: port))
        let process = makeProcess(port: port)
        let input = Pipe()
        process.standardInput = input
        let logOffset: UInt64
        do {
            let log = try FileHandle(forWritingTo: configuration.logURL)
            defer { try? log.close() }
            logOffset = try log.seekToEnd()
            process.standardOutput = log
            process.standardError = log
            process.terminationHandler = { [weak self] exited in
                self?.processExited(exited, logOffset: logOffset)
            }
            try process.run()
            try connection.writeCredentials(to: input.fileHandleForWriting)
        } catch {
            if process.isRunning { process.terminate() }
            return .failed(.launch(error.localizedDescription))
        }
        lock.withLock { self.process = process }

        while process.isRunning {
            if Task.isCancelled {
                stop()
                return .idle
            }
            let ready: Bool
            if let probe {
                ready = await probe(client.baseURL)
            } else {
                ready = (try? await client.status()) != nil
            }
            if ready {
                let next = HelperPhase.ready(port: port)
                let ownsProcess = lock.withLock {
                    guard self.process === process, process.isRunning else { return false }
                    connectedClient = client
                    _phase = next
                    return true
                }
                if ownsProcess {
                    onPhaseChange?(next)
                    return next
                }
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
            connectedClient = nil
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

    private func resetLog() throws {
        let fm = FileManager.default
        let url = configuration.logURL
        try fm.createDirectory(at: url.deletingLastPathComponent(), withIntermediateDirectories: true)
        try Data().write(to: url, options: .atomic)
        try fm.setAttributes([.posixPermissions: 0o600], ofItemAtPath: url.path)
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
