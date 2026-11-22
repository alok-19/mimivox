import Foundation

/// Where the helper downloads models from. Every download goes through
/// `huggingface_hub`, which reads `HF_ENDPOINT` once when the helper starts,
/// so an internal mirror (Artifactory, Nexus, a Hugging Face proxy) only
/// needs its base URL here.
public struct ModelSource: Equatable, Sendable {
    public enum Origin: Equatable, Sendable {
        /// huggingface.co
        case standard
        /// Baked into Info.plist by `scripts/build_app.sh`.
        case build
        /// Pushed by the organization in a configuration profile.
        case managed
        /// Typed in Settings.
        case user
    }

    public static let standardEndpoint = URL(string: "https://huggingface.co")!
    /// Info.plist key and the managed (MDM) preference key.
    public static let infoPlistKey = "MIMIVOXModelEndpoint"
    public static let defaultsKey = "ModelDownloadEndpoint"

    /// nil means huggingface.co.
    public var endpoint: URL?
    public var origin: Origin

    public init(endpoint: URL? = nil, origin: Origin = .standard) {
        self.endpoint = endpoint
        self.origin = origin
    }

    public var effectiveEndpoint: URL { endpoint ?? Self.standardEndpoint }

    /// An organization's choice; Settings shows it read-only.
    public var isLocked: Bool { origin == .managed }

    /// Precedence: managed, then the user's value, then the build default.
    /// Values that are not http(s) URLs are ignored.
    public static func resolve(managed: String?, user: String?, build: String?) -> ModelSource {
        if let url = managed.flatMap(normalize) { return ModelSource(endpoint: url, origin: .managed) }
        if let url = user.flatMap(normalize) { return ModelSource(endpoint: url, origin: .user) }
        if let url = build.flatMap(normalize) { return ModelSource(endpoint: url, origin: .build) }
        return ModelSource()
    }

    /// Reads managed and user values from `defaults` and the build value from `bundle`.
    public static func current(defaults: UserDefaults = .standard, bundle: Bundle = .main) -> ModelSource {
        let stored = defaults.string(forKey: defaultsKey)
        let forced = defaults.objectIsForced(forKey: defaultsKey)
        return resolve(
            managed: forced ? stored : nil,
            user: forced ? nil : stored,
            build: bundle.object(forInfoDictionaryKey: infoPlistKey) as? String
        )
    }

    /// An http(s) URL with a host and no trailing slash, or nil.
    public static func normalize(_ raw: String) -> URL? {
        var text = raw.trimmingCharacters(in: .whitespacesAndNewlines)
        while text.hasSuffix("/") { text.removeLast() }
        guard let url = URL(string: text),
            let scheme = url.scheme?.lowercased(), scheme == "https" || scheme == "http",
            url.host?.isEmpty == false,
            url.query == nil, url.fragment == nil
        else { return nil }
        return url
    }

    /// Variables for the helper. `HF_TOKEN` is sent only to a custom endpoint,
    /// never to huggingface.co.
    public func helperEnvironment(token: String?) -> [String: String] {
        guard let endpoint else { return [:] }
        var env = ["HF_ENDPOINT": endpoint.absoluteString]
        if let token, !token.isEmpty { env["HF_TOKEN"] = token }
        return env
    }

    /// The translation model, which every install downloads (`CAT_MODEL_ID` in `config.py`).
    public static let probeRepo = "hotchpotch/CAT-Translate-1.4b-mlx-q4"

    /// The file a reachability check fetches: what a real download asks for first.
    public func probeURL(repo: String = probeRepo, file: String = "config.json") -> URL {
        effectiveEndpoint.appendingPathComponent(repo).appendingPathComponent("resolve/main").appendingPathComponent(
            file)
    }
}
