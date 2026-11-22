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
            url.user == nil, url.password == nil,
            url.query == nil, url.fragment == nil
        else { return nil }
        return url
    }

    /// Variables for the helper. A token is sent only to a custom HTTPS endpoint.
    public func helperEnvironment(token: String?) -> [String: String] {
        var env = ["HF_HUB_DISABLE_IMPLICIT_TOKEN": "1", "HF_TOKEN": ""]
        if let endpoint { env["HF_ENDPOINT"] = endpoint.absoluteString }
        if allowsAccessToken, let token = token?.trimmingCharacters(in: .whitespacesAndNewlines), !token.isEmpty {
            env["HF_TOKEN"] = token
            env["HF_HUB_DISABLE_IMPLICIT_TOKEN"] = "0"
        }
        return env
    }

    /// Whether a custom HTTPS endpoint can receive its access token.
    public var allowsAccessToken: Bool {
        guard let endpoint, endpoint.scheme?.lowercased() == "https",
            endpoint.host?.isEmpty == false,
            endpoint.user == nil, endpoint.password == nil,
            endpoint.query == nil, endpoint.fragment == nil,
            let host = normalizedHost(endpoint),
            let standardHost = Self.standardEndpoint.host?.lowercased()
        else { return false }

        return host != standardHost && !host.hasSuffix(".\(standardHost)")
    }

    /// The normalized origin that can receive the access token, if one is configured.
    public var accessTokenOrigin: String? {
        guard allowsAccessToken, let endpoint, let host = normalizedHost(endpoint) else { return nil }
        return "https://\(host):\(effectivePort(endpoint))"
    }

    /// A credentialed probe may follow redirects only within the same HTTPS origin.
    public func permitsCredentialedRedirect(to destination: URL) -> Bool {
        guard let endpoint, allowsAccessToken,
            destination.scheme?.lowercased() == "https",
            destination.user == nil, destination.password == nil,
            let endpointHost = normalizedHost(endpoint),
            let destinationHost = normalizedHost(destination)
        else { return false }

        return endpointHost == destinationHost
            && effectivePort(endpoint) == effectivePort(destination)
    }

    private func effectivePort(_ url: URL) -> Int {
        url.port ?? 443
    }

    private func normalizedHost(_ url: URL) -> String? {
        guard let host = url.host?.lowercased() else { return nil }
        return host.hasSuffix(".") ? String(host.dropLast()) : host
    }

    /// The translation model, which every install downloads (`CAT_MODEL_ID` in `config.py`).
    public static let probeRepo = "hotchpotch/CAT-Translate-1.4b-mlx-q4"

    /// The file a reachability check fetches: what a real download asks for first.
    public func probeURL(repo: String = probeRepo, file: String = "config.json") -> URL {
        effectiveEndpoint.appendingPathComponent(repo).appendingPathComponent("resolve/main").appendingPathComponent(
            file)
    }
}
