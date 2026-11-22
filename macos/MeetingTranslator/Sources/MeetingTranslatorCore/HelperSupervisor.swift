import Foundation

/// Which port the helper listens on.
public enum HelperPortPolicy: Equatable, Sendable {
    /// The app launches its own server on a free loopback port (bundled app).
    case automatic
    /// Launch an owned helper on this port.
    case fixed(Int)
}

public struct HelperConfiguration: Equatable, Sendable {
    public var projectRoot: URL
    public var host: String
    public var portPolicy: HelperPortPolicy
    public var pythonExecutable: URL
    public var pythonArgumentsPrefix: [String]
    public var environment: [String: String]
    /// Helper stdout and stderr are appended here.
    public var logURL: URL

    public init(
        projectRoot: URL,
        host: String = "127.0.0.1",
        portPolicy: HelperPortPolicy = .automatic,
        pythonExecutable: URL = URL(fileURLWithPath: "/usr/bin/env"),
        pythonArgumentsPrefix: [String] = ProjectLocator.defaultPythonArgumentsPrefix(),
        environment: [String: String] = [:],
        logURL: URL? = nil
    ) {
        self.projectRoot = projectRoot
        self.host = host
        self.portPolicy = portPolicy
        self.pythonExecutable = pythonExecutable
        self.pythonArgumentsPrefix = pythonArgumentsPrefix
        self.environment = environment
        // Dev runs: the server's data dir defaults to `data/` under the project root.
        self.logURL = logURL ?? projectRoot.appendingPathComponent("data/logs/helper.log")
    }

    public func baseURL(port: Int) -> URL {
        URL(string: "https://\(host):\(port)/")!
    }

    public func helperArguments(port: Int, parentPID: Int32) -> [String] {
        pythonArgumentsPrefix + [
            "-m",
            "mimivox.server",
            "--host",
            host,
            "--port",
            String(port),
            "--parent-pid",
            String(parentPID),
        ]
    }

    public var helperEnvironment: [String: String] {
        var merged = ProcessInfo.processInfo.environment
        for (key, value) in environment {
            merged[key] = value
        }
        merged["MIMIVOX_PROJECT_ROOT"] = projectRoot.path
        merged["PYTHONUNBUFFERED"] = "1"
        return merged
    }

    /// `MIMIVOX_PORT` pins the port; otherwise `fallback`.
    public static func portPolicy(
        environment: [String: String],
        fallback: HelperPortPolicy
    ) -> HelperPortPolicy {
        environment["MIMIVOX_PORT"].flatMap { Int($0) }.map { .fixed($0) } ?? fallback
    }
}

extension HelperConfiguration {
    /// Relative location of the bundled interpreter inside `MimiVox.app`.
    public static let bundledPythonRelativePath = "Contents/Resources/python/bin/python3"

    /// `~/Library/Application Support/MimiVox`, or `MIMIVOX_DATA_DIR` when set.
    public static func defaultDataRoot(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileManager: FileManager = .default
    ) -> URL {
        if let override = environment["MIMIVOX_DATA_DIR"], !override.isEmpty {
            return URL(fileURLWithPath: override, isDirectory: true)
        }
        let support =
            fileManager.urls(for: .applicationSupportDirectory, in: .userDomainMask).first
            ?? fileManager.homeDirectoryForCurrentUser.appendingPathComponent("Library/Application Support")
        return support.appendingPathComponent("MimiVox", isDirectory: true)
    }

    /// Configuration for a bundle that ships its own Python runtime, or nil
    /// when `bundleURL` has none (dev runs via `swift run` keep the `uv run` path).
    public static func bundled(
        bundleURL: URL = Bundle.main.bundleURL,
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileManager: FileManager = .default
    ) -> HelperConfiguration? {
        let python = bundleURL.appendingPathComponent(bundledPythonRelativePath)
        guard fileManager.isExecutableFile(atPath: python.path) else {
            return nil
        }
        let dataRoot = defaultDataRoot(environment: environment, fileManager: fileManager)
        return HelperConfiguration(
            projectRoot: dataRoot,
            portPolicy: portPolicy(environment: environment, fallback: .automatic),
            pythonExecutable: python,
            pythonArgumentsPrefix: [],
            environment: [
                "MIMIVOX_DATA_DIR": dataRoot.path,
                "PYTHONNOUSERSITE": "1",
                // The build precompiles bytecode before signing; a .pyc written
                // into the bundle at runtime would break its code signature.
                "PYTHONDONTWRITEBYTECODE": "1",
            ],
            logURL: dataRoot.appendingPathComponent("logs/helper.log")
        )
    }

    /// Bundled configuration when the app ships Python, else the dev `uv run` one.
    public static func current(
        environment: [String: String] = ProcessInfo.processInfo.environment
    ) -> HelperConfiguration {
        bundled(environment: environment)
            ?? HelperConfiguration(
                projectRoot: ProjectLocator.defaultProjectRoot(environment: environment),
                portPolicy: portPolicy(environment: environment, fallback: .automatic)
            )
    }
}

public enum ProjectLocator {
    public static func defaultPythonArgumentsPrefix(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileManager: FileManager = .default
    ) -> [String] {
        let uv = defaultUVExecutable(environment: environment, fileManager: fileManager).path
        if uv == "/usr/bin/env" {
            return ["uv", "run", "python"]
        }
        return [uv, "run", "python"]
    }

    public static func defaultUVExecutable(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        fileManager: FileManager = .default
    ) -> URL {
        if let override = environment["UV_EXECUTABLE"], !override.isEmpty {
            return URL(fileURLWithPath: override)
        }
        for path in [
            "/Users/alok/.local/bin/uv",
            "/opt/homebrew/bin/uv",
            "/usr/local/bin/uv",
            "/usr/bin/env",
        ] {
            if path == "/usr/bin/env" || fileManager.isExecutableFile(atPath: path) {
                return URL(fileURLWithPath: path)
            }
        }
        return URL(fileURLWithPath: "/usr/bin/env")
    }

    public static func defaultProjectRoot(
        environment: [String: String] = ProcessInfo.processInfo.environment,
        currentDirectory: URL = URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    ) -> URL {
        if let override = environment["MIMIVOX_PROJECT_ROOT"], !override.isEmpty {
            return URL(fileURLWithPath: override)
        }

        for root in [currentDirectory, Bundle.main.bundleURL, Bundle.main.executableURL].compactMap({ $0 }) {
            if let located = locateProjectRoot(startingAt: root) {
                return located
            }
        }
        return currentDirectory
    }

    private static func locateProjectRoot(startingAt url: URL) -> URL? {
        var candidate = url.standardizedFileURL
        if candidate.pathExtension != "" {
            candidate = candidate.deletingLastPathComponent()
        }
        while true {
            let pyproject = candidate.appendingPathComponent("pyproject.toml").path
            let mimivoxServer = candidate.appendingPathComponent("src/mimivox/server.py").path
            if FileManager.default.fileExists(atPath: pyproject),
                FileManager.default.fileExists(atPath: mimivoxServer)
            {
                return candidate
            }
            let parent = candidate.deletingLastPathComponent()
            if parent.path == candidate.path {
                return nil
            }
            candidate = parent
        }
    }
}
