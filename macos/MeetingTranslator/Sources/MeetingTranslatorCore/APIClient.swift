import Foundation

public protocol HTTPTransport: Sendable {
    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse)
}

extension URLSession: HTTPTransport {
    public func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        let (data, response) = try await self.data(for: request, delegate: nil)
        guard let httpResponse = response as? HTTPURLResponse else {
            throw MeetingTranslatorAPIError.invalidResponse
        }
        return (data, httpResponse)
    }
}

public enum MeetingTranslatorAPIError: Error, Equatable, LocalizedError {
    case invalidBaseURL
    case invalidResponse
    /// Status code, response body, and the URL that answered.
    case httpStatus(Int, String, URL?)

    public var errorDescription: String? {
        switch self {
        case .invalidBaseURL:
            return "Invalid API base URL"
        case .invalidResponse:
            return "Invalid API response"
        case .httpStatus(let code, let body, let url):
            if let message = Self.parseMessage(from: body) {
                return message
            }
            // Never show a raw body: a non-MimiVox server answers with HTML.
            return url.map { "HTTP \(code) from \($0.absoluteString)" } ?? "HTTP \(code)"
        }
    }

    /// The server's own JSON `message` or `detail`, if the body carries one.
    private static func parseMessage(from body: String) -> String? {
        guard let data = body.data(using: .utf8),
            let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
        else {
            return nil
        }
        if let message = object["message"] as? String, !message.isEmpty {
            return message
        }
        if let detail = object["detail"] as? String, !detail.isEmpty {
            return detail
        }
        return nil
    }
}

public struct StartSessionRequest: Codable, Equatable, Sendable {
    public var source: String
    /// "native" when the app captures system audio itself and streams it
    /// to `/api/session/audio`; nil lets the server open an input device.
    public var capture: String?
    /// Per-meeting glossary profile name; "" runs with none, nil defers to settings.
    public var glossaryProfile: String?
    /// Language pair for this session; nil defers to the persisted settings.
    public var sourceLang: String?
    public var targetLang: String?

    public init(
        source: String, capture: String? = nil, glossaryProfile: String? = nil,
        languagePair: LanguagePair? = nil
    ) {
        self.source = source
        self.capture = capture
        self.glossaryProfile = glossaryProfile
        self.sourceLang = languagePair?.source
        self.targetLang = languagePair?.target
    }

    private enum CodingKeys: String, CodingKey {
        case source, capture
        case glossaryProfile = "glossary_profile"
        case sourceLang = "source_lang"
        case targetLang = "target_lang"
    }
}

public struct CommandResponse: Codable, Equatable, Sendable {
    public var message: String?
    public var sessionID: String?
    public var path: String?

    public init(message: String? = nil, sessionID: String? = nil, path: String? = nil) {
        self.message = message
        self.sessionID = sessionID
        self.path = path
    }

    private enum CodingKeys: String, CodingKey {
        case message
        case sessionID = "session_id"
        case path
    }
}

public enum TranslationDirection: String, Codable, Equatable, Sendable {
    case jaToEn = "ja-en"
    case enToJa = "en-ja"
}

public struct TranslateRequest: Codable, Equatable, Sendable {
    public var text: String
    /// Omitted from the JSON when nil so the server applies its default (ja-en).
    public var direction: TranslationDirection?

    public init(text: String, direction: TranslationDirection? = nil) {
        self.text = text
        self.direction = direction
    }
}

public struct TranslateResponse: Codable, Equatable, Sendable {
    public var sourceText: String
    public var translatedText: String
    /// JA→EN of `translatedText`; only present for `en-ja` requests.
    public var backTranslation: String?

    public init(sourceText: String, translatedText: String, backTranslation: String? = nil) {
        self.sourceText = sourceText
        self.translatedText = translatedText
        self.backTranslation = backTranslation
    }

    private enum CodingKeys: String, CodingKey {
        case sourceText = "source_text"
        case translatedText = "translated_text"
        case backTranslation = "back_translation"
    }
}

public struct SettingsResponse: Codable, Equatable, Sendable {
    public var settings: MeetingSettings

    public init(settings: MeetingSettings) {
        self.settings = settings
    }
}

public final class MeetingTranslatorAPIClient: Sendable {
    public let baseURL: URL
    private let transport: any HTTPTransport
    private let bearerToken: String?
    private let decoder: JSONDecoder
    private let encoder: JSONEncoder

    public init(
        baseURL: URL = URL(string: "http://127.0.0.1:8765/")!,
        transport: any HTTPTransport = URLSession.shared,
        bearerToken: String? = nil
    ) {
        self.baseURL = baseURL
        self.transport = transport
        self.bearerToken = bearerToken
        self.decoder = JSONDecoder()
        self.encoder = JSONEncoder()
    }

    public func status() async throws -> MeetingStatusResponse {
        try await send(path: "/api/status?events=0", method: "GET")
    }

    public func eventStream() -> AsyncThrowingStream<MeetingEvent, Error> {
        AsyncThrowingStream { continuation in
            let task = Task {
                do {
                    var request = try makeRequest(path: "/api/events", method: "GET")
                    request.timeoutInterval = 600
                    guard let session = transport as? URLSession else {
                        throw MeetingTranslatorAPIError.invalidResponse
                    }
                    let (bytes, response) = try await session.bytes(for: request)
                    guard let http = response as? HTTPURLResponse else {
                        continuation.finish(throwing: MeetingTranslatorAPIError.invalidResponse)
                        return
                    }
                    guard (200..<300).contains(http.statusCode) else {
                        continuation.finish(
                            throwing: MeetingTranslatorAPIError.httpStatus(
                                http.statusCode, "", request.url))
                        return
                    }
                    for try await line in bytes.lines {
                        try Task.checkCancellation()
                        if line.hasPrefix("data: "),
                            let data = String(line.dropFirst(6)).data(using: .utf8),
                            let event = try? decoder.decode(MeetingEvent.self, from: data)
                        {
                            continuation.yield(event)
                        }
                    }
                    continuation.finish()
                } catch is CancellationError {
                    continuation.finish()
                } catch {
                    continuation.finish(throwing: error)
                }
            }
            continuation.onTermination = { _ in
                task.cancel()
            }
        }
    }

    public func settings() async throws -> MeetingSettings {
        let response: SettingsResponse = try await send(path: "/api/settings", method: "GET")
        return response.settings
    }

    public func devices() async throws -> [AudioDevice] {
        let response: DevicesResponse = try await send(path: "/api/devices", method: "GET")
        return response.inputDevices
    }

    public func updateSettings(_ settings: MeetingSettings) async throws -> MeetingSettings {
        let body = try encoder.encode(settings)
        let response: SettingsResponse = try await send(path: "/api/settings", method: "POST", body: body)
        return response.settings
    }

    public func startSetupDownload() async throws -> SetupState {
        let response: SetupResponse = try await send(path: "/api/setup/download", method: "POST", body: Data("{}".utf8))
        return response.setup
    }

    public func cancelSetupDownload() async throws -> SetupState {
        let response: SetupResponse = try await send(path: "/api/setup/cancel", method: "POST", body: Data("{}".utf8))
        return response.setup
    }

    public func startSession(
        source: String, capture: String? = nil, glossaryProfile: String? = nil,
        languagePair: LanguagePair? = nil
    ) async throws -> CommandResponse {
        let body = try encoder.encode(
            StartSessionRequest(
                source: source, capture: capture, glossaryProfile: glossaryProfile, languagePair: languagePair))
        return try await send(path: "/api/session/start", method: "POST", body: body)
    }

    /// Upload natively captured audio (mono float32 LE @ 16 kHz).
    /// Throws `.httpStatus(409, _)` once the session has ended.
    public func pushSessionAudio(sessionID: String, samples: Data) async throws {
        let encoded = sessionID.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? sessionID
        var request = try makeRequest(path: "/api/session/audio?session_id=\(encoded)", method: "POST")
        request.timeoutInterval = 10
        request.httpBody = samples
        request.setValue("application/octet-stream", forHTTPHeaderField: "content-type")
        let (data, response) = try await transport.data(for: request)
        guard (200..<300).contains(response.statusCode) else {
            throw MeetingTranslatorAPIError.httpStatus(
                response.statusCode, String(decoding: data, as: UTF8.self), request.url)
        }
    }

    /// Text of a short spoken clip (mono float32 LE @ 16 kHz). `hint` is "ja" or "en",
    /// used only when Whisper's own language guess is neither.
    public func dictate(samples: Data, hint: String) async throws -> DictationResponse {
        var request = try makeRequest(path: "/api/dictate?hint=\(hint)", method: "POST")
        request.timeoutInterval = 120  // the first clip may wait for Whisper to load
        request.httpBody = samples
        request.setValue("application/octet-stream", forHTTPHeaderField: "content-type")
        let (data, response) = try await transport.data(for: request)
        guard (200..<300).contains(response.statusCode) else {
            throw MeetingTranslatorAPIError.httpStatus(
                response.statusCode, String(decoding: data, as: UTF8.self), request.url)
        }
        return try decoder.decode(DictationResponse.self, from: data)
    }

    public func translate(text: String, direction: TranslationDirection? = nil) async throws -> TranslateResponse {
        let body = try encoder.encode(TranslateRequest(text: text, direction: direction))
        return try await send(path: "/api/translate", method: "POST", body: body)
    }

    public func pauseSession() async throws -> CommandResponse {
        try await command(path: "/api/session/pause")
    }

    public func resumeSession() async throws -> CommandResponse {
        try await command(path: "/api/session/resume")
    }

    /// Skip ahead when captions are behind. `mode` is "fast" or "skip"; nil lets the server choose.
    public func catchUp(mode: String? = nil) async throws -> CatchUpResponse {
        struct Body: Encodable { var mode: String? }
        let body = try encoder.encode(Body(mode: mode))
        return try await send(path: "/api/session/catch-up", method: "POST", body: body)
    }

    /// Transcribe and translate what catch-ups skipped in an archived session.
    public func recoverSkipped(sessionID: String) async throws -> RecoverResponse {
        let encoded = sessionID.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? sessionID
        return try await send(path: "/api/sessions/\(encoded)/recover", method: "POST", body: Data("{}".utf8))
    }

    public func stopSession() async throws -> CommandResponse {
        try await command(path: "/api/session/stop")
    }

    public func export(format: String, sessionID: String? = nil, pinnedOnly: Bool = false) async throws
        -> CommandResponse
    {
        var path = "/api/session/export?format=\(format)"
        if pinnedOnly {
            path += "&pinned_only=true"
        }
        if let sessionID, !sessionID.isEmpty {
            let encoded = sessionID.addingPercentEncoding(withAllowedCharacters: .urlQueryAllowed) ?? sessionID
            path += "&session_id=\(encoded)"
        }
        return try await command(path: path)
    }

    /// Replaces the pinned line indexes of the live session, or of an archived one.
    public func setPins(_ ids: [Int], sessionID: String? = nil) async throws -> CommandResponse {
        struct Body: Encodable {
            var pinnedIDs: [Int]
            var sessionID: String?
            enum CodingKeys: String, CodingKey {
                case pinnedIDs = "pinned_ids"
                case sessionID = "session_id"
            }
        }
        let body = try encoder.encode(Body(pinnedIDs: ids.sorted(), sessionID: sessionID))
        return try await send(path: "/api/session/pins", method: "POST", body: body)
    }

    public func renameSpeaker(id: String, label: String) async throws -> CommandResponse {
        let body = try encoder.encode(["id": id, "label": label])
        return try await send(path: "/api/session/speakers/rename", method: "POST", body: body)
    }

    public func renameArchivedSpeaker(id: String, label: String, sessionID: String) async throws -> CommandResponse {
        let body = try encoder.encode(["id": id, "label": label])
        let allowedPathCharacters = CharacterSet.urlPathAllowed.subtracting(CharacterSet(charactersIn: "/?#%"))
        let sessionPath = sessionID.addingPercentEncoding(withAllowedCharacters: allowedPathCharacters) ?? sessionID
        return try await send(
            path: "/api/sessions/\(sessionPath)/speakers/rename", method: "POST", body: body)
    }

    public func listSessions() async throws -> [ArchivedSessionSummary] {
        let response: ArchivedSessionsResponse = try await send(path: "/api/sessions", method: "GET")
        return response.sessions
    }

    public func getSession(id: String) async throws -> ArchivedSessionDetail {
        try await send(path: "/api/sessions/\(id)", method: "GET")
    }

    /// Full-text search over archived sessions. Empty filters are omitted.
    public func searchSessions(
        query: String,
        since: String? = nil,
        until: String? = nil,
        speaker: String? = nil,
        tag: String? = nil,
        limit: Int = 20
    ) async throws -> LibrarySearchResponse {
        var components = URLComponents()
        components.queryItems = [
            URLQueryItem(name: "q", value: query),
            URLQueryItem(name: "since", value: since),
            URLQueryItem(name: "until", value: until),
            URLQueryItem(name: "speaker", value: speaker),
            URLQueryItem(name: "tag", value: tag),
            URLQueryItem(name: "limit", value: String(limit)),
        ].filter { $0.value?.isEmpty == false }
        // URLComponents leaves "+" alone, which a server reads as a space; a UTC offset needs it.
        let query = (components.percentEncodedQuery ?? "").replacingOccurrences(of: "+", with: "%2B")
        return try await send(path: "/api/sessions/search?" + query, method: "GET")
    }

    public func updateSessionTags(id: String, add: [String] = [], remove: [String] = []) async throws
        -> SessionTagsResponse
    {
        let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        let body = try encoder.encode(SessionTagsRequest(add: add, remove: remove))
        return try await send(path: "/api/sessions/\(encoded)/tags", method: "POST", body: body)
    }

    /// Renames an archived meeting; an empty title goes back to the generated one.
    public func renameArchivedSession(id: String, title: String) async throws -> SessionTitleResponse {
        let encoded = id.addingPercentEncoding(withAllowedCharacters: .urlPathAllowed) ?? id
        let body = try encoder.encode(["title": title])
        return try await send(path: "/api/sessions/\(encoded)/title", method: "POST", body: body)
    }

    public func deleteArchivedSession(id: String) async throws -> CommandResponse {
        try await send(path: "/api/sessions/\(id)", method: "DELETE")
    }

    /// Generate (or fetch cached) meeting notes. `sessionID` nil means the current session.
    public func meetingNotes(sessionID: String? = nil, regenerate: Bool = false) async throws -> MeetingNotes {
        var body: [String: Any] = ["regenerate": regenerate]
        if let sessionID, !sessionID.isEmpty { body["session_id"] = sessionID }
        let data = try JSONSerialization.data(withJSONObject: body)
        return try await send(path: "/api/session/notes", method: "POST", body: data)
    }

    /// Words from the session (current when `sessionID` is nil) with reading and gloss.
    public func vocabulary(sessionID: String? = nil) async throws -> VocabularyResponse {
        var body: [String: Any] = [:]
        if let sessionID, !sessionID.isEmpty { body["session_id"] = sessionID }
        let data = try JSONSerialization.data(withJSONObject: body)
        return try await send(path: "/api/session/vocabulary", method: "POST", body: data)
    }

    /// Writes the vocabulary to a file (`format` is "csv" or "anki"); the response names it.
    public func exportVocabulary(format: String, sessionID: String? = nil) async throws -> VocabularyExportResponse {
        var items = [URLQueryItem(name: "format", value: format)]
        if let sessionID, !sessionID.isEmpty { items.append(URLQueryItem(name: "session_id", value: sessionID)) }
        var components = URLComponents()
        components.queryItems = items
        let path = "/api/session/vocabulary/export?" + (components.percentEncodedQuery ?? "")
        return try await send(path: path, method: "POST", body: Data("{}".utf8))
    }

    /// Ask a question of the transcript so far; the answer cites line ids.
    public func ask(question: String, sessionID: String? = nil) async throws -> AskAnswer {
        var body: [String: Any] = ["question": question]
        if let sessionID, !sessionID.isEmpty { body["session_id"] = sessionID }
        let data = try JSONSerialization.data(withJSONObject: body)
        return try await send(path: "/api/session/ask", method: "POST", body: data)
    }

    /// Deletes every archived session and everything derived from it.
    public func deleteAllHistory() async throws -> CommandResponse {
        try await send(path: "/api/sessions", method: "DELETE")
    }

    public func modelCommand(_ name: String, body: [String: Bool] = [:]) async throws -> CommandResponse {
        let data = try encoder.encode(body)
        return try await send(path: "/api/models/\(name)", method: "POST", body: data)
    }

    public func makeRequest(path: String, method: String, body: Data? = nil) throws -> URLRequest {
        guard let url = URL(string: path, relativeTo: baseURL)?.absoluteURL,
            url.host == baseURL.host, url.port == baseURL.port,
            url.scheme == baseURL.scheme, url.scheme == "https" || url.scheme == "http",
            bearerToken == nil || url.scheme == "https"
        else {
            throw MeetingTranslatorAPIError.invalidBaseURL
        }
        var request = URLRequest(url: url)
        request.httpMethod = method
        request.setValue("application/json", forHTTPHeaderField: "accept")
        if let bearerToken {
            request.setValue("Bearer \(bearerToken)", forHTTPHeaderField: "Authorization")
        }
        // Live start can wait on device open; status stays short.
        if path.contains("/session/start") || path.contains("/session/stop") {
            request.timeoutInterval = 120
        } else if path.contains("/session/notes") || path.contains("/session/ask") {
            // First call loads the on-device LLM and may wait behind live inference.
            request.timeoutInterval = 300
        } else if path.contains("/api/status") || path.contains("/api/devices") {
            request.timeoutInterval = 15
        } else {
            request.timeoutInterval = 60
        }
        if let body {
            request.httpBody = body
            request.setValue("application/json", forHTTPHeaderField: "content-type")
        }
        return request
    }

    private func command(path: String) async throws -> CommandResponse {
        try await send(path: path, method: "POST", body: Data("{}".utf8))
    }

    private func send<T: Decodable>(path: String, method: String, body: Data? = nil) async throws -> T {
        let request = try makeRequest(path: path, method: method, body: body)
        let (data, response) = try await transport.data(for: request)
        guard (200..<300).contains(response.statusCode) else {
            throw MeetingTranslatorAPIError.httpStatus(
                response.statusCode, String(decoding: data, as: UTF8.self), request.url)
        }
        return try decoder.decode(T.self, from: data)
    }
}
