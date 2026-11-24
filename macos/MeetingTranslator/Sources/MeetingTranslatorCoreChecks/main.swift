import CoreAudio
import Foundation
import MeetingTranslatorCore
import PDFKit

struct CheckFailure: Error, CustomStringConvertible {
    var description: String
}

func expect(_ condition: @autoclosure () -> Bool, _ message: String) throws {
    if !condition() {
        throw CheckFailure(description: message)
    }
}

func unwrap<T>(_ value: T?, _ message: String) throws -> T {
    guard let value else {
        throw CheckFailure(description: message)
    }
    return value
}

struct MockTransport: HTTPTransport {
    var handler: @Sendable (URLRequest) throws -> (Data, HTTPURLResponse)

    func data(for request: URLRequest) async throws -> (Data, HTTPURLResponse) {
        try handler(request)
    }
}

func checkModelDecoding() throws {
    let json = """
        {
          "state": "completed",
          "session_id": "session-1",
          "events": [
            {"type": "AsrCompletedEvent", "index": 1, "start": 6.2, "end": 8.9, "text": "では始めさせていただきます"},
            {"type": "TranslationCompletedEvent", "index": 1, "start": 6.2, "end": 8.9, "text": "Now, we will begin.", "source_indexes": [1]},
            {"type": "AsrCompletedEvent", "index": 3, "start": 9.0, "end": 10.0, "text": "次です"},
            {"type": "TranslationCompletedEvent", "index": 99, "start": 9.0, "end": 10.0, "text": "Next.", "source_indexes": [3]},
            {"type": "AudioLevelEvent", "level": 0.2, "timestamp_s": 6.2}
          ],
          "settings": {
            "microphone_device": "1",
            "system_audio_device": "2",
            "glossary_path": "/tmp/glossary.json",
            "privacy_mode": "local-only",
            "latency_quality": "balanced",
            "cloud_provider_enabled": false,
            "cloud_provider_consent": false,
            "replay_audio_path": "fixtures/meeting-recording-converted.wav",
            "replay_duration_seconds": 20,
            "model_paths": {"whisper": "/models/whisper", "cat_translate": "cat"}
          },
          "model_readiness": {"ready": true, "next_steps": []},
          "health": {
            "audio_input_active": true,
            "asr_worker_active": true,
            "translation_worker_active": true,
            "queue_lag_s": 0.0,
            "model_ready": true,
            "degraded": false,
            "session_label": "Completed",
            "audio_label": "Completed",
            "asr_label": "Completed",
            "translation_label": "Completed"
          },
          "performance_summary": {
            "asr_count": 2,
            "translation_count": 2,
            "empty_asr_count": 0,
            "stale_drops": 0,
            "late_translations": 0,
            "display_latency_s": {"p50": 1.2, "p90": 1.8},
            "asr_backend": "mlx"
          },
          "error": null
        }
        """

    let status = try JSONDecoder().decode(MeetingStatusResponse.self, from: Data(json.utf8))
    try expect(status.state == "completed", "status state should decode")
    try expect(status.sessionID == "session-1", "session id should decode")
    try expect(status.modelReadiness.ready, "model readiness should decode")
    try expect(status.health.sessionLabel == "Completed", "health labels should decode")
    try expect(status.performanceSummary?.asrBackend == "mlx", "performance summary should decode")
    struct EventsOnly: Decodable { var events: [MeetingEvent] }
    let paired = try JSONDecoder().decode(EventsOnly.self, from: Data(json.utf8)).events.bilingualTranscriptRows
    try expect(paired.first?.english?.text == "Now, we will begin.", "translation should pair with its ASR row")
    try expect(paired.count == 2, "bilingual rows should pair all ASR rows")
    try expect(paired[1].japanese?.text == "次です", "second ASR row should remain in source order")
    try expect(paired[1].english?.text == "Next.", "translation should pair by source_indexes")
}

func checkDeviceDecoding() throws {
    let json = #"{"input_devices":[{"id":"1","name":"Built-in Microphone"}]}"#
    let response = try JSONDecoder().decode(DevicesResponse.self, from: Data(json.utf8))
    try expect(response.inputDevices.first?.name == "Built-in Microphone", "devices should decode")
}

func checkSettingsEncoding() throws {
    let settings = MeetingSettings(
        microphoneDevice: "mic",
        systemAudioDevice: "system",
        glossaryPath: "/tmp/glossary.json",
        replayAudioPath: "fixtures/sample.wav",
        replayDurationSeconds: 12,
        modelPaths: ModelPaths(whisper: "/models/whisper", catTranslate: "cat-model")
    )

    let data = try JSONEncoder().encode(settings)
    let object = try unwrap(
        JSONSerialization.jsonObject(with: data) as? [String: Any], "settings json should be object")
    try expect(object["microphone_device"] as? String == "mic", "microphone_device key should encode")
    try expect(object["system_audio_device"] as? String == "system", "system_audio_device key should encode")
    try expect(object["replay_duration_seconds"] as? Double == 12, "replay duration should encode")
    let modelPaths = try unwrap(object["model_paths"] as? [String: Any], "model_paths should encode")
    try expect(modelPaths["cat_translate"] as? String == "cat-model", "cat_translate key should encode")
}

func checkGlossaryProfiles() async throws {
    let json = #"""
        {"glossary_profiles":[{"name":"Acme","entries":[
          {"source":"部長","target":"department head"},{"source":"てっこう","target":"結構"}]}],
         "active_glossary_profile":"Acme"}
        """#
    let settings = try JSONDecoder().decode(MeetingSettings.self, from: Data(json.utf8))
    try expect(settings.activeGlossaryProfile == "Acme", "active profile should decode")
    let entries = try unwrap(settings.glossaryProfiles.first?.entries, "profile entries should decode")
    try expect(entries.count == 2, "entries should decode")
    try expect(!entries[0].isASRFix && entries[1].isASRFix, "kind should derive from target")
    let old = try JSONDecoder().decode(MeetingSettings.self, from: Data("{}".utf8))
    try expect(old.glossaryProfiles.isEmpty && old.activeGlossaryProfile.isEmpty, "profiles default empty")

    let data = try JSONEncoder().encode(settings)
    let object = try unwrap(JSONSerialization.jsonObject(with: data) as? [String: Any], "settings json")
    try expect(object["active_glossary_profile"] as? String == "Acme", "active_glossary_profile should encode")
    let profiles = try unwrap(object["glossary_profiles"] as? [[String: Any]], "glossary_profiles should encode")
    try expect(profiles.first?["name"] as? String == "Acme", "profile name should encode")

    let parsed = GlossaryTerm.parse(pasted: "部長,department head\n\nbad\n見積もり\tquote, estimate")
    try expect(parsed.count == 2 && parsed[1].target == "quote, estimate", "paste import should parse CSV/TSV")

    let client = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8765/")!,
        transport: MockTransport { request in
            let body = try unwrap(request.httpBody, "start should have body")
            let object = try unwrap(JSONSerialization.jsonObject(with: body) as? [String: Any], "start body")
            try expect(object["glossary_profile"] as? String == "Acme", "glossary_profile should encode")
            let data = Data(#"{"message":"started","session_id":"s1"}"#.utf8)
            return (data, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
    )
    _ = try await client.startSession(source: "replay-file", glossaryProfile: "Acme")
    let bare = try JSONEncoder().encode(StartSessionRequest(source: "replay-file"))
    let bareObject = try unwrap(JSONSerialization.jsonObject(with: bare) as? [String: Any], "bare body")
    try expect(bareObject["glossary_profile"] == nil, "glossary_profile omitted when nil")
}

func checkSpeakerDecoding() async throws {
    let json = """
        [
          {"type": "AsrCompletedEvent", "index": 1, "start": 1.2, "text": "こんにちは", "speaker": {"id": "speaker-1", "label": "Speaker 1"}},
          {"type": "TranslationCompletedEvent", "index": 1, "start": 1.2, "text": "Hello.", "source_indexes": [1], "speaker": {"id": "speaker-1", "label": "Speaker 1"}},
          {"type": "AsrCompletedEvent", "index": 2, "start": 5.9, "text": "はい", "speaker": {"id": "speaker-2", "label": "Speaker 2"}},
          {"type": "TranslationCompletedEvent", "index": 2, "start": 5.9, "text": "Yes.", "source_indexes": [2], "speaker": {"id": "speaker-2", "label": "Speaker 2"}},
          {"type": "AsrCompletedEvent", "index": 3, "start": 9.0, "text": "次です"},
          {"type": "SpeakerRenamedEvent", "message": "speaker-2=Sato", "speaker": {"id": "speaker-2", "label": "Sato"}}
        ]
        """
    let events = try JSONDecoder().decode([MeetingEvent].self, from: Data(json.utf8))
    try expect(
        events[0].speaker == SpeakerTag(id: "speaker-1", label: "Speaker 1"), "speaker should decode on ASR events")
    try expect(events[4].speaker == nil, "events without a speaker should decode with nil speaker")
    try expect(events[5].speaker?.label == "Sato", "rename event should carry the new tag")

    let rows = events.bilingualTranscriptRows
    try expect(rows.map { $0.speaker?.id } == ["speaker-1", "speaker-2", nil], "rows should expose their speaker")
    try expect(SpeakerTag(id: "speaker-12", label: "x").number == 12, "speaker number should parse from id")

    let revisedJSON = """
        [
          {"type": "AsrCompletedEvent", "index": 1, "start": 1.0, "text": "前半", "speaker": {"id": "speaker-1", "label": "Speaker 1"}},
          {"type": "TranslationCompletedEvent", "index": 1, "start": 1.0, "text": "First half", "source_indexes": [1], "speaker": {"id": "speaker-1", "label": "Speaker 1"}},
          {"type": "AsrCompletedEvent", "index": 2, "start": 3.0, "text": "後半", "speaker": {"id": "speaker-2", "label": "Speaker 2"}},
          {"type": "TranslationCompletedEvent", "index": 2, "start": 3.0, "text": "second half", "source_indexes": [2], "speaker": {"id": "speaker-2", "label": "Speaker 2"}},
          {"type": "TranscriptRevisionEvent", "index": 1, "start": 1.0, "end": 5.0, "text": "One sentence.", "ja": "前半後半", "merged_indexes": [2]}
        ]
        """
    let revisedEvents = try JSONDecoder().decode([MeetingEvent].self, from: Data(revisedJSON.utf8))
    let revisedRows = revisedEvents.bilingualTranscriptRows
    try expect(revisedRows.count == 1, "merged row should disappear")
    try expect(
        revisedRows[0].isRevised && revisedRows[0].speaker?.label == "Speaker 1", "revised row keeps the first speaker")
    try expect(
        revisedRows[0].english?.text == "One sentence." && revisedRows[0].japanese?.text == "前半後半",
        "revised row carries merged text")

    let confidenceJSON = """
        [
          {"type": "AsrCompletedEvent", "index": 1, "start": 1.0, "text": "前半", "confidence": {"level": "low", "reasons": ["Speech recognition was unsure"], "asr": 0.31, "mt": null}},
          {"type": "TranslationCompletedEvent", "index": 1, "start": 1.0, "text": "First half", "source_indexes": [1], "confidence": {"level": "low", "reasons": ["Speech recognition was unsure"], "asr": 0.31, "mt": null}},
          {"type": "AsrCompletedEvent", "index": 2, "start": 3.0, "text": "後半", "confidence": {"level": "high", "reasons": [], "asr": 0.9, "mt": 0.8}},
          {"type": "TranslationCompletedEvent", "index": 2, "start": 3.0, "text": "second half", "source_indexes": [2], "confidence": {"level": "high", "reasons": [], "asr": 0.9, "mt": 0.8}},
          {"type": "AsrCompletedEvent", "index": 3, "start": 6.0, "text": "三", "confidence": {"level": "surprise"}},
          {"type": "TranslationCompletedEvent", "index": 3, "start": 6.0, "text": "three", "source_indexes": [3]},
          {"type": "TranscriptRevisionEvent", "index": 2, "start": 3.0, "end": 5.0, "text": "Second, revised.", "ja": "後半です", "merged_indexes": [], "confidence": {"level": "medium", "reasons": ["Translation was unsure"], "asr": 0.8, "mt": 0.4}}
        ]
        """
    let confidenceEvents = try JSONDecoder().decode([MeetingEvent].self, from: Data(confidenceJSON.utf8))
    try expect(
        confidenceEvents[0].confidence?.level == .low && confidenceEvents[0].confidence?.asr == 0.31,
        "confidence should decode")
    try expect(confidenceEvents[5].confidence == nil, "events without confidence decode as nil")
    try expect(
        confidenceEvents[4].confidence?.level == .high,
        "unknown confidence level should decode as high, not fail the event")
    let confidenceRows = confidenceEvents.bilingualTranscriptRows
    try expect(
        confidenceRows[0].flaggedConfidence?.help == "Low confidence: Speech recognition was unsure",
        "low row exposes help text")
    try expect(confidenceRows[1].flaggedConfidence?.level == .medium, "revision recomputes the row's confidence")
    try expect(confidenceRows[2].flaggedConfidence == nil, "high or missing confidence is not flagged")

    let client = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8765/")!,
        transport: MockTransport { request in
            try expect(request.url?.path == "/api/session/speakers/rename", "rename path should post")
            let body = try unwrap(request.httpBody, "rename request should have body")
            let object = try unwrap(
                JSONSerialization.jsonObject(with: body) as? [String: String], "rename body should be JSON")
            try expect(object == ["id": "speaker-1", "label": "Tanaka"], "rename body should carry id and label")
            let data = Data(#"{"message":"renamed","speaker":{"id":"speaker-1","label":"Tanaka"}}"#.utf8)
            return (data, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
    )
    let response = try await client.renameSpeaker(id: "speaker-1", label: "Tanaka")
    try expect(response.message == "renamed", "rename response should decode")
}

func checkAPIRequests() async throws {
    let client = MeetingTranslatorAPIClient(baseURL: URL(string: "http://127.0.0.1:8765/")!)
    let request = try client.makeRequest(path: "/api/session/start", method: "POST", body: Data("{}".utf8))
    try expect(request.url?.absoluteString == "http://127.0.0.1:8765/api/session/start", "start URL should build")
    try expect(request.httpMethod == "POST", "method should be POST")
    try expect(request.value(forHTTPHeaderField: "content-type") == "application/json", "content type should be JSON")

    let replayClient = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8765/")!,
        transport: MockTransport { request in
            try expect(request.url?.path == "/api/session/start", "start path should post")
            let body = try unwrap(request.httpBody, "request should have body")
            let object = try unwrap(
                JSONSerialization.jsonObject(with: body) as? [String: Any], "body should be JSON object")
            try expect(object["source"] as? String == "replay-file", "source should be replay-file")
            let data = Data(#"{"message":"Realtime WAV replay session started","session_id":"session-1"}"#.utf8)
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (data, response)
        }
    )
    let response = try await replayClient.startSession(source: "replay-file")
    try expect(response.sessionID == "session-1", "session_id should decode")

    let translateClient = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8765/")!,
        transport: MockTransport { request in
            try expect(request.url?.path == "/api/translate", "translate path should post")
            let body = try unwrap(request.httpBody, "translate request should have body")
            let object = try unwrap(
                JSONSerialization.jsonObject(with: body) as? [String: Any], "translate body should be JSON object")
            try expect(object["text"] as? String == "では始めます", "translate text should encode")
            try expect(object["direction"] == nil, "default translate must not send direction")
            let data = Data(#"{"source_text":"では始めます","translated_text":"Let's begin."}"#.utf8)
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (data, response)
        }
    )
    let translation = try await translateClient.translate(text: "では始めます")
    try expect(translation.sourceText == "では始めます", "source_text should decode")
    try expect(translation.translatedText == "Let's begin.", "translated_text should decode")
    try expect(translation.backTranslation == nil, "old response shape should decode with nil back_translation")

    let replyClient = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8765/")!,
        transport: MockTransport { request in
            let body = try unwrap(request.httpBody, "reply request should have body")
            let object = try unwrap(
                JSONSerialization.jsonObject(with: body) as? [String: Any], "reply body should be JSON object")
            try expect(object["direction"] as? String == "en-ja", "direction should encode as en-ja")
            let data = Data(
                #"{"source_text":"Thank you.","translated_text":"ありがとうございます。","back_translation":"Thank you."}"#.utf8)
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (data, response)
        }
    )
    let reply = try await replyClient.translate(text: "Thank you.", direction: .enToJa)
    try expect(reply.translatedText == "ありがとうございます。", "reply translated_text should decode")
    try expect(reply.backTranslation == "Thank you.", "back_translation should decode")
}

func checkNativeSystemAudioRequests() async throws {
    let startClient = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8765/")!,
        transport: MockTransport { request in
            let body = try unwrap(request.httpBody, "start should have body")
            let object = try unwrap(
                JSONSerialization.jsonObject(with: body) as? [String: Any], "start body should be JSON")
            try expect(object["source"] as? String == "system-audio", "source should be system-audio")
            try expect(object["capture"] as? String == "native", "capture should be native")
            let data = Data(#"{"message":"started","session_id":"s1"}"#.utf8)
            return (data, HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!)
        }
    )
    _ = try await startClient.startSession(source: "system-audio", capture: "native")

    let legacyBody = try JSONEncoder().encode(StartSessionRequest(source: "microphone"))
    let legacy = try unwrap(
        JSONSerialization.jsonObject(with: legacyBody) as? [String: Any], "legacy body should be JSON")
    try expect(legacy["capture"] == nil, "capture should be omitted when nil")

    let samples = Data(repeating: 1, count: 6_400)
    let pushClient = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8765/")!,
        transport: MockTransport { request in
            try expect(request.url?.path == "/api/session/audio", "audio path should post")
            try expect(request.url?.query == "session_id=s1", "audio should carry session id")
            try expect(
                request.value(forHTTPHeaderField: "content-type") == "application/octet-stream",
                "audio should be binary")
            try expect(request.httpBody == samples, "audio body should be raw samples")
            let status = request.url?.query == "session_id=s1" ? 204 : 409
            return (
                Data(), HTTPURLResponse(url: request.url!, statusCode: status, httpVersion: nil, headerFields: nil)!
            )
        }
    )
    try await pushClient.pushSessionAudio(sessionID: "s1", samples: samples)

    let goneClient = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8765/")!,
        transport: MockTransport { request in
            (
                Data(#"{"message":"no native system-audio session"}"#.utf8),
                HTTPURLResponse(url: request.url!, statusCode: 409, httpVersion: nil, headerFields: nil)!
            )
        }
    )
    do {
        try await goneClient.pushSessionAudio(sessionID: "old", samples: samples)
        throw CheckFailure(description: "409 should throw")
    } catch MeetingTranslatorAPIError.httpStatus(409, _, _) {
    }
}

final class Recorder: @unchecked Sendable {
    private let lock = NSLock()
    private var items: [Data] = []
    private var stoppedCount = 0

    func record(_ data: Data) {
        lock.lock()
        items.append(data)
        lock.unlock()
    }

    func stopped() {
        lock.lock()
        stoppedCount += 1
        lock.unlock()
    }

    var snapshot: (items: [Data], stopped: Int) {
        lock.lock()
        defer { lock.unlock() }
        return (items, stoppedCount)
    }
}

func checkSystemAudioStreamer() async throws {
    // Batches small capture callbacks into ~minChunkBytes uploads, in order,
    // and flushes the remainder on finish.
    let sent = Recorder()
    let streamer = SystemAudioStreamer(
        minChunkBytes: 8,
        send: { data in
            sent.record(data)
            return true
        })
    for byte in UInt8(0)..<UInt8(10) {
        streamer.append(Data([byte, byte]))
    }
    streamer.finish()
    await streamer.waitUntilFinished()
    let batches = sent.snapshot.items
    try expect(batches.map(\.count) == [8, 8, 4], "streamer should batch to min size and flush remainder")
    try expect(
        batches.reduce(Data(), +) == Data((0..<10).flatMap { [UInt8($0), UInt8($0)] }), "streamer should preserve order"
    )

    // Stops (and reports it) once the session is gone.
    let gone = Recorder()
    let stopping = SystemAudioStreamer(
        minChunkBytes: 4,
        send: { data in
            gone.record(data)
            return false
        }, onStopped: { gone.stopped() })
    stopping.append(Data(count: 4))
    stopping.append(Data(count: 4))
    await stopping.waitUntilFinished()
    try expect(gone.snapshot.items.count == 1, "streamer should stop sending after session ends")
    try expect(gone.snapshot.stopped == 1, "streamer should report stop once")
}

func checkModelSource() throws {
    try expect(
        ModelSource.resolve(managed: nil, user: nil, build: nil) == ModelSource(), "no value means huggingface.co")
    try expect(
        ModelSource().helperEnvironment(token: "secret") == [
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
            "HF_TOKEN": "",
        ], "huggingface.co gets no endpoint or token and ignores inherited Hugging Face credentials")

    let all = ModelSource.resolve(
        managed: "https://org.example/hf/", user: "https://me.example", build: "https://build.example")
    try expect(all.origin == .managed && all.isLocked, "an organization's value wins and locks the field")
    try expect(all.endpoint?.absoluteString == "https://org.example/hf", "a trailing slash is dropped")
    try expect(
        ModelSource.resolve(managed: nil, user: "https://me.example", build: "https://build.example").origin == .user,
        "the user's value beats the build default")
    try expect(
        ModelSource.resolve(managed: nil, user: " ", build: "https://build.example").origin == .build,
        "an empty user value falls back to the build default")
    try expect(
        ModelSource.resolve(managed: "ftp://x", user: "mirror.example", build: nil) == ModelSource(),
        "non-http values are ignored")

    let env = all.helperEnvironment(token: "t0k")
    try expect(
        env == [
            "HF_ENDPOINT": "https://org.example/hf",
            "HF_TOKEN": "t0k",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "0",
        ], "an HTTPS mirror gets its configured token")
    try expect(all.accessTokenOrigin == "https://org.example:443", "the token binds to its HTTPS origin")
    try expect(
        all.helperEnvironment(token: " \n ")["HF_HUB_DISABLE_IMPLICIT_TOKEN"] == "1",
        "a blank token leaves inherited Hugging Face credentials disabled")
    try expect(
        all.permitsCredentialedRedirect(to: URL(string: "https://ORG.example/next")!),
        "a credentialed probe can redirect within its HTTPS origin")
    try expect(
        !all.permitsCredentialedRedirect(to: URL(string: "https://other.example/next")!),
        "a credentialed probe cannot redirect to another host")
    try expect(
        !all.permitsCredentialedRedirect(to: URL(string: "http://org.example/next")!),
        "a credentialed probe cannot downgrade to HTTP")
    try expect(
        !all.permitsCredentialedRedirect(to: URL(string: "https://org.example:444/next")!),
        "a credentialed probe cannot redirect to another port")

    let httpSource = ModelSource.resolve(managed: nil, user: "http://mirror.example/hf", build: nil)
    try expect(
        httpSource.endpoint?.absoluteString == "http://mirror.example/hf",
        "anonymous HTTP mirrors remain available")
    try expect(!httpSource.allowsAccessToken, "HTTP mirrors cannot use access tokens")
    try expect(
        httpSource.helperEnvironment(token: "secret") == [
            "HF_ENDPOINT": "http://mirror.example/hf",
            "HF_TOKEN": "",
            "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
        ], "an HTTP mirror receives no configured or inherited Hugging Face token")
    try expect(
        ModelSource.normalize("https://user:password@mirror.example") == nil,
        "endpoint URLs cannot contain credentials")
    let officialHub = ModelSource.resolve(managed: nil, user: "https://huggingface.co", build: nil)
    try expect(!officialHub.allowsAccessToken, "a custom endpoint cannot send a mirror token to huggingface.co")
    let dottedHub = ModelSource.resolve(managed: nil, user: "https://huggingface.co.", build: nil)
    try expect(!dottedHub.allowsAccessToken, "a trailing DNS dot cannot bypass the huggingface.co token restriction")
    let credentialURL = ModelSource(endpoint: URL(string: "https://user:password@mirror.example")!, origin: .user)
    try expect(!credentialURL.allowsAccessToken, "constructed endpoints with URL credentials cannot receive the token")
    try expect(
        all.probeURL().absoluteString
            == "https://org.example/hf/hotchpotch/CAT-Translate-1.4b-mlx-q4/resolve/main/config.json",
        "the check asks for the file a download fetches first"
    )

    let supervisor = HelperSupervisor(
        configuration: HelperConfiguration(
            projectRoot: URL(fileURLWithPath: "/tmp/mv"), environment: ["HF_ENDPOINT": "https://old.example"]))
    supervisor.launchEnvironment = { ["HF_ENDPOINT": "https://new.example"] }
    try expect(
        supervisor.makeProcess(port: 1).environment?["HF_ENDPOINT"] == "https://new.example",
        "launch environment overrides the configured one")
}

func checkHelperConfiguration() throws {
    let projectRoot = URL(fileURLWithPath: "/tmp/meeting-translator")
    let config = HelperConfiguration(projectRoot: projectRoot, portPolicy: .fixed(8899))
    try expect(
        config.baseURL(port: 8899).absoluteString == "https://127.0.0.1:8899/", "base URL should use the given port")
    try expect(
        config.helperArguments(port: 8899, parentPID: 42).suffix(10) == [
            "run",
            "python",
            "-m",
            "mimivox.server",
            "--host",
            "127.0.0.1",
            "--port",
            "8899",
            "--parent-pid",
            "42",
        ], "helper arguments should launch Python app module with the parent pid")
    try expect(
        config.logURL.path == "/tmp/meeting-translator/data/logs/helper.log",
        "dev log lives under the server's data dir")
    try expect(
        config.helperEnvironment["MIMIVOX_PROJECT_ROOT"] == "/tmp/meeting-translator", "project root env should be set")

    let supervisor = HelperSupervisor(configuration: HelperConfiguration(projectRoot: projectRoot))
    let process = supervisor.makeProcess(port: 8765)
    try expect(process.executableURL?.path == "/usr/bin/env", "helper should use env wrapper")
    try expect(process.arguments?.contains("mimivox.server") == true, "process args should include app module")
    try expect(process.currentDirectoryURL?.path == projectRoot.path, "process cwd should be project root")

    let located = ProjectLocator.defaultProjectRoot(
        environment: [:],
        currentDirectory: URL(fileURLWithPath: FileManager.default.currentDirectoryPath)
    )
    try expect(
        FileManager.default.fileExists(atPath: located.appendingPathComponent("src/mimivox/server.py").path),
        "project locator should walk up to repository root")

    let overridden = ProjectLocator.defaultProjectRoot(
        environment: ["MIMIVOX_PROJECT_ROOT": "/tmp/custom-root"],
        currentDirectory: projectRoot
    )
    try expect(overridden.path == "/tmp/custom-root", "project locator should honor explicit override")
}

/// Hands out `ports` in order, then 0.
final class PortSequence: @unchecked Sendable {
    private let lock = NSLock()
    private var ports: [Int]
    private(set) var handedOut = 0
    init(_ ports: [Int]) { self.ports = ports }
    func next() -> Int? {
        lock.withLock {
            handedOut += 1
            return ports.isEmpty ? nil : ports.removeFirst()
        }
    }
}

/// A supervisor whose "python" is a shell script standing in for the server:
/// it fails to bind on `FAKE_BUSY_PORTS`, exits 3 with FAKE_CRASH, exits 5
/// after a moment with FAKE_DIE_LATER, and otherwise stays up.
func fakeSupervisor(
    dir: URL,
    policy: HelperPortPolicy,
    ports: PortSequence = PortSequence([]),
    env: [String: String] = [:],
    probe: @escaping @Sendable (URL) async -> Bool
) throws -> HelperSupervisor {
    let script = dir.appendingPathComponent("fake-server.sh")
    if !FileManager.default.fileExists(atPath: script.path) {
        try Data(
            """
            while [ $# -gt 0 ]; do [ "$1" = --port ] && PORT=$2; shift; done
            echo "fake server on $PORT"
            case " $FAKE_BUSY_PORTS " in *" $PORT "*)
              echo "ERROR: [Errno 48] error while attempting to bind on address ('127.0.0.1', $PORT): address already in use" >&2
              exit 1;;
            esac
            if [ -n "$FAKE_CRASH" ]; then echo "Traceback: model load boom" >&2; exit 3; fi
            if [ -n "$FAKE_DIE_LATER" ]; then sleep 0.5; echo "segfault later" >&2; exit 5; fi
            exec sleep 30
            """.utf8
        ).write(to: script)
    }
    let config = HelperConfiguration(
        projectRoot: dir,
        portPolicy: policy,
        pythonExecutable: URL(fileURLWithPath: "/bin/sh"),
        pythonArgumentsPrefix: [script.path],
        environment: env,
        logURL: dir.appendingPathComponent("logs/helper.log")
    )
    return HelperSupervisor(
        configuration: config, probe: probe, pickPort: { ports.next() }, pollInterval: .milliseconds(20))
}

func checkHelperLifecycle() async throws {
    let fm = FileManager.default
    let dir = fm.temporaryDirectory.appendingPathComponent("MimiVoxHelper-\(UUID().uuidString)")
    try fm.createDirectory(at: dir, withIntermediateDirectories: true)
    defer { try? fm.removeItem(at: dir) }
    let log = dir.appendingPathComponent("logs/helper.log")

    let free = try unwrap(HelperSupervisor.freeLoopbackPort(), "a free loopback port")
    try expect(free > 1024, "free port is unprivileged, got \(free)")

    // Address in use on the first port: retry on a new one, then ready there.
    let ports = PortSequence([41001, 41002])
    let retrying = try fakeSupervisor(
        dir: dir, policy: .automatic, ports: ports, env: ["FAKE_BUSY_PORTS": "41001"],
        probe: { $0.port == 41002 })
    try expect(retrying.plannedPort == 41001, "automatic policy plans the first free port")
    let ready = await retrying.start()
    try expect(ready == .ready(port: 41002), "errno 48 retries on a new port, got \(ready)")
    try expect(retrying.plannedPort == 41002, "planned port follows the retry")
    let logText = try String(contentsOf: log, encoding: .utf8)
    try expect(
        logText.contains("fake server on 41001") && logText.contains("address already in use"),
        "helper stdout and stderr go to the log")
    let result1 = await retrying.start()
    try expect(result1 == .ready(port: 41002), "start on a ready helper is a no-op")
    retrying.stop()
    try expect(retrying.phase == .idle, "stop returns to idle")

    // Every port busy: give up after the attempt cap.
    let busyPorts = PortSequence([41011, 41012, 41013, 41014])
    let busy = try fakeSupervisor(
        dir: dir, policy: .automatic, ports: busyPorts, env: ["FAKE_BUSY_PORTS": "41011 41012 41013 41014"],
        probe: { _ in false })
    let exhausted = await busy.start()
    try expect(exhausted == .failed(.portInUse(port: 41013)), "three busy ports fail as portInUse, got \(exhausted)")
    try expect(busyPorts.handedOut == HelperSupervisor.maxPortAttempts, "one port per attempt")

    // The process dies during startup: fail at once with its code and stderr.
    let crashing = try fakeSupervisor(
        dir: dir, policy: .automatic, ports: PortSequence([41021]), env: ["FAKE_CRASH": "1"],
        probe: { _ in false })
    let crashed = await crashing.start()
    guard case .failed(.exited(let code, let tail)) = crashed else {
        throw CheckFailure(description: "crash should fail as exited, got \(crashed)")
    }
    try expect(
        code == 3 && tail.hasSuffix("Traceback: model load boom"), "exit code and stderr tail, got \(code) \(tail)")
    try expect(!tail.contains("Errno 48"), "tail covers only this run's output")
    try expect(
        HelperFailure.exited(code: code, stderrTail: tail).summary
            == "Server exited with code 3: Traceback: model load boom", "one-line summary")

    // A ready server that dies later is reported as failed.
    let dying = try fakeSupervisor(
        dir: dir, policy: .automatic, ports: PortSequence([41031]), env: ["FAKE_DIE_LATER": "1"],
        probe: { _ in true })
    let result2 = await dying.start()
    try expect(result2 == .ready(port: 41031), "ready before it dies")
    let deadline = Date().addingTimeInterval(5)
    while dying.phase == .ready(port: 41031) && Date() < deadline {
        try await Task.sleep(for: .milliseconds(50))
    }
    try expect(
        dying.phase == .failed(.exited(code: 5, stderrTail: "fake server on 41031\nsegfault later")),
        "a crash after ready fails the phase, got \(dying.phase)")

    let fixed = try fakeSupervisor(dir: dir, policy: .fixed(41041), probe: { _ in true })
    let fixedPhase = await fixed.start()
    try expect(fixedPhase == .ready(port: 41041), "the app launches its own helper on a fixed port")
    let client = try unwrap(fixed.client, "authenticated client for the owned helper")
    let request = try client.makeRequest(path: "/api/status", method: "GET")
    try expect(request.url?.scheme == "https", "helper requests use TLS")
    try expect(
        request.value(forHTTPHeaderField: "Authorization")?.hasPrefix("Bearer ") == true,
        "helper requests authenticate the client")
    fixed.stop()
    try expect(fixed.client == nil, "stop removes the connected client")

    // A pinned port we cannot bind fails without trying another port.
    let pinnedBusy = try fakeSupervisor(
        dir: dir, policy: .fixed(41051), env: ["FAKE_BUSY_PORTS": "41051"], probe: { _ in false })
    let result5 = await pinnedBusy.start()
    try expect(result5 == .failed(.portInUse(port: 41051)), "pinned bind failure is portInUse")

    try Data("old transcript".utf8).write(to: log)
    let fresh = try fakeSupervisor(dir: dir, policy: .automatic, ports: PortSequence([41061]), probe: { _ in true })
    _ = await fresh.start()
    fresh.stop()
    let newLog = try String(contentsOf: log, encoding: .utf8)
    try expect(!newLog.contains("old transcript"), "the helper starts with a fresh log")

    // A status error carries metadata without rendering the HTML body.
    let status = URL(string: "http://127.0.0.1:8765/api/status?events=0")!
    let html = MockTransport { request in
        (
            Data("<!DOCTYPE HTML><html>404</html>".utf8),
            HTTPURLResponse(url: request.url!, statusCode: 404, httpVersion: nil, headerFields: nil)!
        )
    }
    let base = URL(string: "http://127.0.0.1:8765/")!
    do {
        _ = try await MeetingTranslatorAPIClient(baseURL: base, transport: html).status()
        throw CheckFailure(description: "404 should throw")
    } catch let error as MeetingTranslatorAPIError {
        try expect(
            error.localizedDescription == "HTTP 404 from \(status.absoluteString)",
            "non-JSON error names status and URL, got \(error.localizedDescription)")
    }
    let detail = MeetingTranslatorAPIError.httpStatus(409, #"{"detail": "Session ended"}"#, status)
    try expect(detail.localizedDescription == "Session ended", "server JSON detail is still shown")
}

func checkSetupState() throws {
    func parse(_ json: String) -> SetupState {
        try! JSONDecoder().decode(SetupState.self, from: Data(json.utf8))
    }
    try expect(
        parse(#"{"phase":"needs_download","total_bytes":1714868108,"free_disk_bytes":5000}"#)
            == .needsDownload(totalBytes: 1_714_868_108, freeDiskBytes: 5000), "needs_download parses")
    let withEta = parse(
        #"{"phase":"downloading","done_bytes":100,"total_bytes":400,"bytes_per_s":2000000.5,"eta_s":47}"#)
    guard case .downloading(let progress) = withEta else { throw CheckFailure(description: "downloading parses") }
    try expect(progress.fraction == 0.25 && progress.etaText == "about 1 min left", "47 s rounds up to a minute")
    try expect(progress.speedText == "2 MB/s", "speed text, got \(progress.speedText ?? "nil")")
    let noEta = parse(#"{"phase":"downloading","done_bytes":0,"total_bytes":400,"bytes_per_s":0,"eta_s":null}"#)
    guard case .downloading(let idle) = noEta else { throw CheckFailure(description: "null eta parses") }
    try expect(
        idle.etaSeconds == nil && idle.etaText == nil && idle.speedText == nil, "null eta hides the time-left phrase")
    try expect(
        SetupState.Progress(doneBytes: 0, totalBytes: 1, bytesPerSecond: 1, etaSeconds: 600).etaText
            == "about 10 min left", "minutes round up")
    try expect(parse(#"{"phase":"loading"}"#) == .loading, "loading parses")
    try expect(parse(#"{"phase":"ready"}"#).isReady, "ready parses")
    try expect(
        parse(#"{"phase":"failed","reason":"disk_full","message":"x"}"#)
            == .failed(.init(reason: .diskFull, message: "x")), "disk_full parses")
    try expect(
        parse(#"{"phase":"failed","reason":"offline","message":"y"}"#)
            == .failed(.init(reason: .offline, message: "y")), "offline parses")
    try expect(
        parse(#"{"phase":"failed","reason":"weird","message":"z"}"#)
            == .failed(.init(reason: .error, message: "z")), "unknown failure reason reads as error")
    try expect(parse(#"{"phase":"from_the_future"}"#) == .loading, "unknown phase reads as loading")

    let status = try JSONDecoder().decode(
        MeetingStatusResponse.self,
        from: JSONEncoder().encode(
            MeetingStatusResponse(state: "idle", setup: .loading)))
    try expect(status.setup == .loading, "status carries setup")
    let event = try JSONDecoder().decode(
        MeetingEvent.self, from: Data(#"{"type":"SetupStateEvent","setup":{"phase":"ready"}}"#.utf8))
    try expect(event.setup == .ready, "SetupStateEvent carries setup")
    try expect(MeetingAutostartPrefs().detectionEnabled == true, "meeting offers default on")
    try expect(CaptureSilence.isSilent(peakLevel: 0) && !CaptureSilence.isSilent(peakLevel: 0.05), "silence threshold")
}

func checkRevisionDecoding() throws {
    // Wire shape emitted by api_adapter.py for a VAD-split sentence (rows 2+3 merged into 2).
    let json = """
        [
          {"type": "AsrCompletedEvent", "index": 1, "start": 0.0, "end": 2.0, "text": "先月より二割高いですね。"},
          {"type": "TranslationCompletedEvent", "index": 1, "start": 0.0, "end": 2.0, "text": "Higher.", "source_indexes": [1]},
          {"type": "AsrCompletedEvent", "index": 2, "start": 4.0, "end": 6.0, "text": "部長には報告しましたが、"},
          {"type": "TranslationCompletedEvent", "index": 2, "start": 4.0, "end": 6.0, "text": "I told the, but", "source_indexes": [2]},
          {"type": "TranscriptRevisionEvent", "index": 2, "start": 4.0, "end": 8.8,
           "text": "I told the manager, but there is no reply yet.",
           "ja": "部長には報告しましたが、まだ返事がありません。",
           "merged_indexes": [3], "reason": "merged_fragment", "timestamp_s": 4.0}
        ]
        """
    let events = try JSONDecoder().decode([MeetingEvent].self, from: Data(json.utf8))
    let revision = try unwrap(events.last, "revision event should decode")
    try expect(revision.type == "TranscriptRevisionEvent", "revision type")
    try expect(revision.index == 2 && revision.mergedIndexes == [3], "revision target and merged indexes")
    try expect(revision.japanese == "部長には報告しましたが、まだ返事がありません。", "revision JA")
    try expect(revision.reason == "merged_fragment", "revision reason")

    let rows = events.bilingualTranscriptRows
    try expect(rows.count == 2, "merged row should not appear, got \(rows.count)")
    let revised = rows[1]
    try expect(revised.isRevised && !rows[0].isRevised, "only the target row is flagged revised")
    try expect(revised.english?.text == "I told the manager, but there is no reply yet.", "EN revised in place")
    try expect(revised.japanese?.text == "部長には報告しましたが、まだ返事がありません。", "JA revised in place")
    try expect(rows.latestEnglish?.text == revised.english?.text, "the latest caption uses revised text")

    // Without a revision event nothing changes (older helper).
    let plain = Array(events.prefix(4))
    try expect(
        plain.bilingualTranscriptRows.count == 2 && !plain.bilingualTranscriptRows[1].isRevised,
        "no revision, no change")
}

func checkBundledConfiguration() throws {
    let fm = FileManager.default
    let bundle = fm.temporaryDirectory.appendingPathComponent("MimiVoxCheck-\(UUID().uuidString).app")
    defer { try? fm.removeItem(at: bundle) }
    try expect(
        HelperConfiguration.bundled(bundleURL: bundle, environment: [:]) == nil,
        "bundle without python falls back to dev mode")

    let bin = bundle.appendingPathComponent("Contents/Resources/python/bin")
    try fm.createDirectory(at: bin, withIntermediateDirectories: true)
    let python = bin.appendingPathComponent("python3")
    try Data("#!/bin/sh\n".utf8).write(to: python)
    try fm.setAttributes([.posixPermissions: 0o755], ofItemAtPath: python.path)

    let config = try unwrap(
        HelperConfiguration.bundled(
            bundleURL: bundle,
            environment: ["MIMIVOX_DATA_DIR": "/tmp/mimivox-data", "MIMIVOX_PORT": "8773"]
        ), "bundle with python yields a bundled configuration")
    try expect(config.pythonExecutable.path == python.path, "bundled python is used")
    try expect(config.portPolicy == .fixed(8773), "MIMIVOX_PORT pins the bundled port")
    try expect(
        config.helperArguments(port: 8773, parentPID: 7) == [
            "-m", "mimivox.server", "--host", "127.0.0.1", "--port", "8773", "--parent-pid", "7",
        ], "bundled args skip uv")
    try expect(config.logURL.path == "/tmp/mimivox-data/logs/helper.log", "helper log lives in the data root")
    try expect(config.projectRoot.path == "/tmp/mimivox-data", "cwd is the data root")
    try expect(
        config.helperEnvironment["MIMIVOX_DATA_DIR"] == "/tmp/mimivox-data", "data dir env is passed to the helper")
    try expect(
        config.helperEnvironment["PYTHONDONTWRITEBYTECODE"] == "1",
        "bundled helper never writes bytecode into the signed bundle")

    let defaults = HelperConfiguration.bundled(bundleURL: bundle, environment: [:])
    try expect(defaults?.portPolicy == .automatic, "bundled app picks its own port by default")
    try expect(
        defaults?.projectRoot.path.hasSuffix("Library/Application Support/MimiVox") == true,
        "default data root is Application Support")
}

func checkMeetingNotes() async throws {
    let notesJSON = """
        {"summary_en": "Release moves to the 24th.", "summary_ja": "リリースは24日に延期。",
         "decisions": [{"text": "Postpone the release", "text_ja": "リリースを延期", "line_ids": [7, 8]}],
         "action_items": [
           {"owner": "Suzuki", "text": "Send the schedule", "text_ja": "スケジュールを送る", "due": "Wednesday", "line_ids": [10]},
           {"owner": null, "text": "Check licence plans", "due": null, "line_ids": [16]}],
         "meta": {"cached": false, "model": "m", "latency_s": 4.2, "llm_calls": 1, "retries": 0, "lines": 36}}
        """
    let notes = try JSONDecoder().decode(MeetingNotes.self, from: Data(notesJSON.utf8))
    try expect(notes.summaryJa == "リリースは24日に延期。", "ja summary decodes")
    try expect(notes.decisions.first?.lineIDs == [7, 8], "decision line ids decode")
    try expect(
        notes.actionItems[0].owner == "Suzuki" && notes.actionItems[0].due == "Wednesday", "owner and due decode")
    try expect(notes.actionItems[1].owner == nil && notes.actionItems[1].due == nil, "null owner and due decode as nil")
    try expect(notes.actionItems[1].textJa == "", "missing text_ja defaults to empty")
    try expect(notes.actionItems[1].text(in: .ja) == "Check licence plans", "JA falls back to EN text")
    try expect(notes.meta?.cached == false && notes.meta?.latencyS == 4.2, "meta decodes, extra keys ignored")

    let md = notes.markdown(language: .en) { id in id == 16 ? nil : "00:\(id)" }
    try expect(md.contains("- Postpone the release _(00:7, 00:8)_"), "markdown cites decision lines")
    try expect(
        md.contains("- Send the schedule (owner: Suzuki, due: Wednesday) _(00:10)_"), "markdown has owner/due/cite")
    try expect(md.contains("- Check licence plans\n"), "uncitable ids are left out")
    try expect(notes.markdown(language: .ja).hasPrefix("## 要約\n\nリリースは24日に延期。"), "JA markdown")
    try expect(MeetingNotes().markdown(language: .en).contains("_None._"), "empty notes render None")

    let longNotes = MeetingNotes(
        summaryEn: "Release planning and business terms.",
        summaryJa: "リリース計画と業務用語。",
        decisions: (1...80).map {
            NotesDecision(text: "Decision \($0): confirm the schedule", textJa: "決定事項 \($0)：日程を確認", lineIDs: [$0])
        }
    )
    let pdfData = try MeetingNotesPDF.render(longNotes, title: "会議メモ", language: .ja) { "00:\($0)" }
    guard let pdf = PDFDocument(data: pdfData) else {
        throw CheckFailure(description: "meeting notes should render as a readable PDF")
    }
    let pdfText = (0..<pdf.pageCount).compactMap { pdf.page(at: $0)?.string }.joined()
    try expect(pdf.pageCount > 1, "meeting notes PDF paginates long notes")
    try expect(pdfText.contains("会議メモ") && pdfText.contains("リリース計画"), "meeting notes PDF text remains selectable")

    let archived = """
        {"id": "s1", "events": [], "notes": \(notesJSON)}
        """
    let detail = try JSONDecoder().decode(ArchivedSessionDetail.self, from: Data(archived.utf8))
    try expect(detail.notes?.decisions.count == 1, "archived session carries cached notes")
    let plain = try JSONDecoder().decode(ArchivedSessionDetail.self, from: Data("{\"id\": \"s2\"}".utf8))
    try expect(plain.notes == nil, "archived session without notes decodes")

    let readiness = try JSONDecoder().decode(
        ModelReadiness.self,
        from: Data(
            "{\"ready\": true, \"next_steps\": [], \"notes_llm\": {\"ready\": false, \"model_id\": \"mlx-community/x\"}}"
                .utf8)
    )
    try expect(
        readiness.ready && readiness.notesLlm?.ready == false, "notes_llm readiness decodes and never gates ready")

    // Requests: paths, bodies, long timeouts.
    final class Seen: @unchecked Sendable { var requests: [URLRequest] = [] }
    let seen = Seen()
    let transport = MockTransport { request in
        seen.requests.append(request)
        let body =
            request.url?.path.hasSuffix("/ask") == true
            ? "{\"answer\": \"Friday.\", \"line_ids\": [5]}" : notesJSON
        return (
            Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        )
    }
    let client = MeetingTranslatorAPIClient(baseURL: URL(string: "http://127.0.0.1:9/")!, transport: transport)
    let generated = try await client.meetingNotes(sessionID: "s1", regenerate: true)
    try expect(generated.decisions.count == 1, "meetingNotes decodes response")
    let answer = try await client.ask(question: "When?", sessionID: nil)
    try expect(answer.answer == "Friday." && answer.lineIDs == [5], "ask decodes answer with cited line ids")
    let notesReq = seen.requests[0]
    let askReq = seen.requests[1]
    try expect(notesReq.url?.path == "/api/session/notes" && notesReq.httpMethod == "POST", "notes endpoint")
    let notesBody = try JSONSerialization.jsonObject(with: notesReq.httpBody ?? Data()) as? [String: Any]
    try expect(notesBody?["session_id"] as? String == "s1" && notesBody?["regenerate"] as? Bool == true, "notes body")
    let askBody = try JSONSerialization.jsonObject(with: askReq.httpBody ?? Data()) as? [String: Any]
    try expect(
        askBody?["question"] as? String == "When?" && askBody?["session_id"] == nil,
        "ask body omits session_id for the current session")
    try expect(notesReq.timeoutInterval >= 300 && askReq.timeoutInterval >= 300, "notes/ask allow for model load")
    try expect(answer.askInJapanese == nil, "an answered question has no Japanese phrasing")
    let unanswered = try JSONDecoder().decode(
        AskAnswer.self,
        from: Data(
            """
            {"answer": "Not discussed.", "line_ids": [],
             "ask_in_japanese": {"ja": "予算はいくらですか？", "romaji": "yosan wa ikura desu ka?"}}
            """.utf8))
    try expect(
        unanswered.askInJapanese == JapanesePhrase(ja: "予算はいくらですか？", romaji: "yosan wa ikura desu ka?"),
        "an unanswered live question decodes its Japanese phrasing")
}

func checkLearningMode() async throws {
    let ja = "届きました。"
    let json = """
        {"type": "AsrCompletedEvent", "index": 1, "text": "\(ja)", "tokens": [
          {"surface": "届き", "reading": "とどき", "base": "届く", "gloss": "to reach"},
          {"surface": "まし"}, {"surface": "た"}, {"surface": "。"}]}
        """
    let event = try JSONDecoder().decode(MeetingEvent.self, from: Data(json.utf8))
    let tokens = try unwrap(event.tokens, "tokens decode")
    try expect(tokens[0].rubyText == "とどき" && tokens[1].rubyText == nil, "ruby only where a reading exists")
    try expect(tokens[0].gloss == "to reach" && tokens[1].base == nil, "gloss and base are optional")
    try expect(tokens.matching(ja) != nil, "tokens that spell the caption match")
    try expect(tokens.matching("届きません。") == nil, "stale tokens never match different text")
    let plain = try JSONDecoder().decode(
        MeetingEvent.self, from: Data("{\"type\": \"AsrCompletedEvent\", \"text\": \"x\"}".utf8))
    try expect(plain.tokens == nil, "captions without tokens decode as before")
    let junk = try JSONDecoder().decode(
        MeetingEvent.self, from: Data("{\"type\": \"AsrCompletedEvent\", \"text\": \"x\", \"tokens\": 5}".utf8))
    try expect(junk.tokens == nil, "malformed tokens never fail the event")

    // A revision rewrites the JA, so the breakdown must follow it.
    let newTokens = [LearningToken(surface: "後半", reading: "こうはん", base: "後半", gloss: "second half")]
    let events = [
        MeetingEvent(
            type: "AsrCompletedEvent", index: 1, text: "前半", tokens: [LearningToken(surface: "前半", reading: "ぜんはん")]),
        MeetingEvent(
            type: "TranscriptRevisionEvent", index: 1, text: "Second half.", japanese: "後半", mergedIndexes: [],
            tokens: newTokens),
    ]
    let revised = events.bilingualTranscriptRows.first?.japanese
    try expect(revised?.text == "後半" && revised?.tokens == newTokens, "revision carries its tokens")

    // Setting round trip, and servers that predate it.
    var settings = MeetingSettings()
    try expect(settings.learningMode == false, "learning mode defaults off")
    settings.learningMode = true
    let object = try JSONSerialization.jsonObject(with: JSONEncoder().encode(settings)) as? [String: Any]
    try expect(object?["learning_mode"] as? Bool == true, "learning_mode encodes")
    let legacy = try JSONDecoder().decode(MeetingSettings.self, from: Data("{}".utf8))
    try expect(legacy.learningMode == false, "missing learning_mode decodes as off")

    // Vocabulary API.
    final class Seen: @unchecked Sendable { var requests: [URLRequest] = [] }
    let seen = Seen()
    let transport = MockTransport { request in
        seen.requests.append(request)
        let body =
            request.url?.path.hasSuffix("/export") == true
            ? "{\"message\": \"exported 1 words as csv\", \"path\": \"/tmp/s1-vocabulary.csv\"}"
            : "{\"session_id\": \"s1\", \"count\": 1, \"words\": [{\"word\": \"届く\", \"reading\": \"とどく\", \"gloss\": \"to reach\", \"count\": 2, \"line_id\": 3, \"example_ja\": \"届きました。\", \"example_en\": \"It arrived.\"}]}"
        return (
            Data(body.utf8), HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
        )
    }
    let client = MeetingTranslatorAPIClient(baseURL: URL(string: "http://127.0.0.1:9/")!, transport: transport)
    let vocab = try await client.vocabulary(sessionID: "s1")
    try expect(
        vocab.words.first?.word == "届く" && vocab.words.first?.lineID == 3 && vocab.words.first?.count == 2
            && vocab.words.first?.exampleJapanese == "届きました。"
            && vocab.words.first?.exampleEnglish == "It arrived.",
        "vocabulary decodes")
    let japaneseCards = StudyCardDeck.make(from: vocab.words, direction: .japaneseToEnglish)
    let englishCards = StudyCardDeck.make(from: vocab.words, direction: .englishToJapanese)
    try expect(
        japaneseCards.first?.front == "届く" && japaneseCards.first?.answer == "to reach"
            && englishCards.first?.front == "to reach" && englishCards.first?.answer == "届く"
            && englishCards.first?.reading == "とどく"
            && japaneseCards.first?.exampleEnglish == "It arrived.",
        "study cards provide both directions and meeting context")
    let export = try await client.exportVocabulary(format: "anki", sessionID: "s1")
    try expect(export.path == "/tmp/s1-vocabulary.csv", "export names the file")
    try expect(
        seen.requests[0].url?.path == "/api/session/vocabulary" && seen.requests[0].httpMethod == "POST",
        "vocabulary endpoint")
    let query = seen.requests[1].url?.query ?? ""
    try expect(query.contains("format=anki") && query.contains("session_id=s1"), "export query")
}

// MARK: - HUD controls and read-aloud

func checkCaptionDisplayModes() throws {
    let en = CaptionDisplayMode.englishOnly.visibility(revealed: false)
    try expect(
        en == CaptionLineVisibility(showsEnglish: true, showsJapanese: false, canReveal: true),
        "English-only hides JA until clicked")
    try expect(
        CaptionDisplayMode.englishOnly.visibility(revealed: true).showsJapanese,
        "clicking an English-only line reveals its JA source")
    let ja = CaptionDisplayMode.japaneseOnly.visibility(revealed: false)
    try expect(ja.showsJapanese && !ja.showsEnglish && ja.canReveal, "Japanese-only hides EN until clicked")
    try expect(
        CaptionDisplayMode.japaneseOnly.visibility(revealed: true).showsEnglish,
        "clicking a Japanese-only line reveals its EN")
    let both = CaptionDisplayMode.bilingual.visibility(revealed: false)
    try expect(
        both.showsEnglish && both.showsJapanese && !both.canReveal, "bilingual shows both and has nothing to reveal")
    try expect(
        CaptionDisplayMode(rawValue: "bilingual") == .bilingual && CaptionDisplayMode(rawValue: "nope") == nil,
        "mode round-trips through its stored raw value")
}

func checkMeetingInsights() throws {
    let events = [
        MeetingEvent(type: "SessionStartedEvent", message: "Session started"),
        MeetingEvent(type: "AsrCompletedEvent", index: 0, start: 4, end: 7, text: "定例ミーティングを始めます。"),
        MeetingEvent(
            type: "TranslationCompletedEvent", index: 0, start: 4, end: 7, text: "Let's start the Weekly Sync.",
            sourceIndexes: [0]),
        MeetingEvent(type: "AsrCompletedEvent", index: 1, start: 9, end: 12.5, text: "見積もりを送ります。"),
        MeetingEvent(
            type: "TranslationCompletedEvent", index: 1, start: 9, end: 12.5, text: "I'll send the quote.",
            sourceIndexes: [1]),
        MeetingEvent(type: "AudioLevelEvent", level: 0.4, timestampSeconds: 99),
    ]
    try expect(events.transcriptDuration == 12.5, "duration is the latest caption end, ignoring meters")
    try expect([MeetingEvent]().transcriptDuration == nil, "no captions means no duration")
    try expect(
        [MeetingEvent(type: "AsrPartialEvent", index: 2, start: 30, text: "…")].transcriptDuration == nil,
        "partials do not count")

    let rows = events.bilingualTranscriptRows
    let profile = GlossaryProfile(
        name: "Sales",
        entries: [
            GlossaryTerm(source: "定例", target: "weekly sync"),
            GlossaryTerm(source: "見積り", target: "quote"),
            GlossaryTerm(source: "予算", target: "budget"),
            GlossaryTerm(source: "ていれい", target: "定例"),
            GlossaryTerm(source: "", target: "ignored"),
        ])
    let hits = GlossaryHits.find(in: rows, profile: profile)
    try expect(
        Set(hits.map(\.term.target)) == ["weekly sync", "quote", "定例"],
        "hits cover the terms present in the transcript, got \(hits.map(\.term.target))")
    try expect(
        hits.first { $0.term.target == "weekly sync" }?.firstLineIndex == 0,
        "a term matches its Japanese source or its English text, case-insensitively")
    try expect(
        hits.first { $0.term.target == "quote" }?.lineCount == 1,
        "a term matches its English text when the Japanese source is spelled differently")
    let repeatedRow = [
        BilingualTranscriptRow(
            index: 9,
            japanese: MeetingEvent(type: "AsrCompletedEvent", index: 9, text: "打ち合わせ"),
            english: MeetingEvent(type: "TranslationCompletedEvent", index: 9, text: "weekly sync then weekly sync")
        )
    ]
    let repeatedHit = GlossaryHits.find(
        in: repeatedRow,
        profile: GlossaryProfile(name: "Sales", entries: [GlossaryTerm(source: "meeting", target: "weekly sync")])
    ).first
    try expect(
        repeatedHit?.lineCount == 1 && repeatedHit?.occurrenceCount == 2,
        "two term mentions in one line count as two occurrences")
    let englishSourceHit = GlossaryHits.find(
        in: rows,
        profile: GlossaryProfile(name: "Reverse", entries: [GlossaryTerm(source: "quote", target: "見積もり")]),
        sourceLanguage: "en"
    ).first
    try expect(englishSourceHit?.occurrenceCount == 1, "frequency follows the configured source language")
    try expect(hits.first { $0.term.target == "定例" }?.lineCount == 1, "a spelling fix matches the corrected Japanese")
    try expect(
        !hits.contains { $0.term.target == "budget" || $0.term.target == "ignored" },
        "unused and blank entries are left out")

    var settings = MeetingSettings(
        glossaryProfiles: [GlossaryProfile(name: "Sales")], activeGlossaryProfile: "Sales")
    let added = settings.addGlossaryTerm(
        source: " weekly sync ", target: "定例", fallbackProfileName: "Meeting Terms")
    try expect(added == .added(profileName: "Sales"), "a new term goes to the active profile")
    try expect(settings.glossaryProfiles[0].entries.first?.source == "weekly sync", "saved terms are trimmed")
    let duplicate = settings.addGlossaryTerm(
        source: "WEEKLY SYNC", target: "定例", fallbackProfileName: "Meeting Terms")
    try expect(duplicate == .alreadyExists(profileName: "Sales"), "term additions avoid case-insensitive duplicates")
    var noProfile = MeetingSettings()
    let created = noProfile.addGlossaryTerm(
        source: "weekly sync", target: "定例", fallbackProfileName: "Meeting Terms")
    try expect(
        created == .added(profileName: "Meeting Terms") && noProfile.activeGlossaryProfile == "Meeting Terms",
        "term additions create and select a fallback profile")
}

func checkPinnedLines() throws {
    let events = [
        MeetingEvent(type: "AsrCompletedEvent", index: 1, start: 6.2, end: 8.9, text: "では始めます"),
        MeetingEvent(
            type: "TranslationCompletedEvent", index: 1, start: 6.2, end: 8.9, text: "Let's begin.", sourceIndexes: [1]),
        MeetingEvent(type: "AsrCompletedEvent", index: 2, start: 65.5, end: 67.0, text: "次です"),
        MeetingEvent(
            type: "TranslationCompletedEvent", index: 2, start: 65.5, end: 67.0, text: "Next.", sourceIndexes: [2]),
    ]
    let rows = events.bilingualTranscriptRows
    try expect(rows.pinnedMarkdown(pinned: []) == "", "no pins exports nothing")
    let markdown = rows.pinnedMarkdown(pinned: [2])
    try expect(
        markdown == "## Pinned lines\n\n- [01:05.50] Next.\n  - JA: 次です\n",
        "pinned markdown lists only pinned rows, got \(markdown)")
    try expect(
        !rows.pinnedMarkdown(pinned: [1, 2]).isEmpty && rows.pinnedMarkdown(pinned: [1, 2]).contains("Let's begin."),
        "multiple pins keep order")

    let json = #"{"id":"s1","pinned_ids":[2,5],"events":[]}"#
    let detail = try JSONDecoder().decode(ArchivedSessionDetail.self, from: Data(json.utf8))
    try expect(detail.pinnedIDs == [2, 5], "archived session decodes pinned_ids")
    let legacy = try JSONDecoder().decode(ArchivedSessionDetail.self, from: Data(#"{"id":"s0"}"#.utf8))
    try expect(legacy.pinnedIDs.isEmpty, "archives without pinned_ids decode as no pins")
}

func checkPinAPIRequests() async throws {
    let client = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8783/")!,
        transport: MockTransport { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            if request.url?.path == "/api/session/pins" {
                let body = try unwrap(request.httpBody, "pins body")
                let object = try unwrap(JSONSerialization.jsonObject(with: body) as? [String: Any], "pins JSON")
                try expect(object["pinned_ids"] as? [Int] == [1, 4], "pins are sent sorted")
                try expect(object["session_id"] as? String == "old", "archived session id is sent")
            } else {
                try expect(request.url?.query?.contains("pinned_only=true") == true, "pinned-only export sets the flag")
            }
            return (Data(#"{"message":"ok"}"#.utf8), response)
        }
    )
    _ = try await client.setPins([4, 1], sessionID: "old")
    _ = try await client.export(format: "markdown", pinnedOnly: true)
}

func checkArchivedSpeakerRenameRequest() async throws {
    let client = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8783/")!,
        transport: MockTransport { request in
            try expect(
                URLComponents(url: request.url!, resolvingAgainstBaseURL: false)?.percentEncodedPath
                    == "/api/sessions/meeting%201/speakers/rename",
                "archived speaker rename encodes the session id")
            let body = try unwrap(request.httpBody, "archived rename body")
            let object = try unwrap(JSONSerialization.jsonObject(with: body) as? [String: String], "rename JSON")
            try expect(object == ["id": "speaker-1", "label": "Sato"], "archived rename carries its label")
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (Data(#"{"message":"renamed"}"#.utf8), response)
        }
    )
    let response = try await client.renameArchivedSpeaker(
        id: "speaker-1", label: "Sato", sessionID: "meeting 1")
    try expect(response.message == "renamed", "archived speaker rename response decodes")
}

func lines(_ pairs: (String, String)...) -> [ReadAloudLine] {
    pairs.map { ReadAloudLine(id: $0.0, text: $0.1) }
}

func checkReadAloudQueue() throws {
    var queue = ReadAloudQueue()
    queue.sync(lines(("1", "One."), ("2", "Two.")))
    try expect(queue.next()?.text == "One.", "oldest line speaks first")
    try expect(queue.next() == nil, "nothing starts while a line is speaking")
    queue.finishCurrent()

    // A revision of a line that has not been spoken replaces its text.
    queue.sync(lines(("1", "One."), ("2", "Two, revised."), ("3", "Three.")))
    try expect(queue.next()?.text == "Two, revised.", "pending line speaks its revised text")
    queue.finishCurrent()

    // A revision of an already-spoken line is never re-spoken.
    queue.sync(lines(("1", "One revised late."), ("2", "Two, revised again."), ("3", "Three.")))
    try expect(queue.pending.map(\.id) == ["3"], "spoken lines are not queued again, pending \(queue.pending)")
    try expect(queue.next()?.text == "Three.", "unspoken line still speaks")
    // Revising the line that is speaking right now does not restart it.
    queue.sync(lines(("3", "Three, revised mid-speech.")))
    try expect(queue.pending.isEmpty, "the speaking line is not re-queued")
    queue.finishCurrent()
    try expect(queue.droppedCount == 0, "nothing was dropped so far")

    // A line merged away by a revision leaves the queue.
    var merged = ReadAloudQueue()
    merged.sync(lines(("a", "Fragment."), ("b", "Rest.")))
    merged.sync(lines(("a", "Fragment. Rest.")))
    try expect(
        merged.pending == lines(("a", "Fragment. Rest.")),
        "merged fragment is removed, survivor carries the merged text")

    // Never more than maxPending behind: the stale ones are dropped, newest kept.
    var behind = ReadAloudQueue()
    behind.sync(lines(("1", "One."), ("2", "Two."), ("3", "Three."), ("4", "Four."), ("5", "Five.")))
    try expect(behind.pending.map(\.id) == ["4", "5"], "queue keeps only the newest \(ReadAloudQueue.maxPending)")
    try expect(behind.droppedCount == 3, "dropped lines are counted for the UI")
    behind.sync(lines(("1", "One."), ("2", "Two."), ("3", "Three."), ("4", "Four."), ("5", "Five.")))
    try expect(
        behind.droppedCount == 3 && behind.pending.map(\.id) == ["4", "5"],
        "dropped lines stay dropped and are not counted twice")

    // Pause: nothing starts, lines queue under the cap, resume continues.
    var paused = ReadAloudQueue()
    paused.setPaused(true)
    paused.sync(lines(("1", "One."), ("2", "Two."), ("3", "Three.")))
    try expect(paused.next() == nil, "no speech while paused")
    try expect(paused.pending.map(\.id) == ["2", "3"] && paused.droppedCount == 1, "paused backlog obeys the cap")
    paused.setPaused(false)
    try expect(paused.next()?.text == "Two.", "resume speaks the oldest kept line")

    // Enabling mid-session reads only new captions.
    var primed = ReadAloudQueue()
    primed.prime(with: lines(("1", "Old."), ("2", "Older.")))
    primed.sync(lines(("1", "Old."), ("2", "Older."), ("3", "New.")))
    try expect(primed.pending.map(\.id) == ["3"], "history is not read aloud when read-aloud starts")

    var empty = ReadAloudQueue()
    empty.sync(lines(("1", "")))
    try expect(empty.next() == nil, "empty text is never spoken")
}

@MainActor
final class FakeSpeech: SpeechOutput {
    var log: [String] = []
    var finish: (@MainActor () -> Void)?

    func speak(_ text: String, voiceID: String?, rate: Float, onFinish: @escaping @MainActor () -> Void) {
        log.append("speak:\(text)|\(voiceID ?? "default")|\(rate)")
        finish = onFinish
    }
    func pause() { log.append("pause") }
    func resume() { log.append("resume") }
    func stop() {
        log.append("stop")
        finish = nil
    }
    func complete() {
        let done = finish
        finish = nil
        done?()
    }
}

@MainActor
func checkReadAloudCoordinator() throws {
    let speech = FakeSpeech()
    let coordinator = ReadAloudCoordinator(output: speech)
    coordinator.voiceID = "com.apple.voice.test"
    coordinator.rate = 0.4

    coordinator.update(lines: lines(("1", "Ignored while off.")))
    try expect(speech.log.isEmpty, "nothing speaks while read-aloud is off")

    coordinator.setEnabled(true, currentLines: lines(("1", "Existing.")), paused: false)
    coordinator.update(lines: lines(("1", "Existing."), ("2", "Hello."), ("3", "World.")))
    try expect(
        speech.log == ["stop", "speak:Hello.|com.apple.voice.test|0.4"],
        "speaks the first new line with the chosen voice and rate, got \(speech.log)")
    coordinator.update(lines: lines(("1", "Existing."), ("2", "Hello."), ("3", "World.")))
    try expect(speech.log.count == 2, "re-syncing does not speak the line again")
    speech.complete()
    try expect(
        speech.log.last == "speak:World.|com.apple.voice.test|0.4", "next line starts when the previous finishes")

    coordinator.setPaused(true)
    try expect(speech.log.last == "pause", "session pause pauses speech")
    coordinator.update(lines: lines(("1", "Existing."), ("2", "Hello."), ("3", "World."), ("4", "Later.")))
    speech.complete()
    try expect(!speech.log.contains { $0.hasPrefix("speak:Later") }, "no new line starts while paused")
    coordinator.setPaused(false)
    try expect(
        speech.log.suffix(2) == ["resume", "speak:Later.|com.apple.voice.test|0.4"],
        "resume continues with the waiting line, got \(speech.log.suffix(2))")

    // Stopping invalidates a stale finish callback from the old utterance.
    let stale = speech.finish
    coordinator.reset()
    let before = speech.log.count
    stale?()
    try expect(speech.log.count == before + 0, "a stopped utterance's finish callback is ignored")

    coordinator.setEnabled(false, currentLines: [], paused: false)
    coordinator.update(lines: lines(("9", "Off again.")))
    try expect(!speech.log.contains { $0.contains("Off again") }, "switching off stops reading")
}

func checkSpeechRenderer() async throws {
    // Writes speech to memory instead of the speakers.
    let slow = await SpeechRenderer.render("The meeting will begin shortly.", rate: 0.35)
    let fast = await SpeechRenderer.render("The meeting will begin shortly.", rate: 0.6)
    let slowResult = try unwrap(slow, "AVSpeechSynthesizer.write produced audio at the slow rate")
    let fastResult = try unwrap(fast, "AVSpeechSynthesizer.write produced audio at the fast rate")
    try expect(slowResult.frames > 0 && slowResult.sampleRate > 0, "rendered speech has samples")
    try expect(
        slowResult.seconds > fastResult.seconds * 1.2,
        "a lower rate renders longer speech (\(slowResult.seconds)s vs \(fastResult.seconds)s)")
    print(
        String(
            format: "  speech render: %.2fs at rate 0.35, %.2fs at rate 0.6 (%.0f Hz)", slowResult.seconds,
            fastResult.seconds, slowResult.sampleRate))
    try expect(!ReadAloudVoices.english().isEmpty, "at least one English voice is installed")
    let note = ReadAloudOutputNote.text(deviceName: "Test Speakers")
    try expect(
        note.contains("Test Speakers") && note.contains("headphones"),
        "output note names the device and suggests headphones")
}

/// Read-only: prints bundle IDs and running flags from Core Audio process
/// objects. Reads no audio and creates no tap.
func listAudioProcesses() {
    guard #available(macOS 14.2, *) else { return }
    for process in CoreAudioProcesses.snapshot().sorted(by: { $0.bundleID < $1.bundleID }) {
        let app = MeetingApp.app(forBundleID: process.bundleID)?.name ?? "-"
        print(
            "\(process.bundleID)\tpid=\(process.pid)\toutput=\(process.isRunningOutput)\tinput=\(process.isRunningInput)\tmeetingApp=\(app)"
        )
    }
}

func checkMeetingAutostartDetector() throws {
    var prefs = MeetingAutostartPrefs()
    prefs.detectionEnabled = true
    func zoom(_ out: Bool) -> [AudioProcessInfo] { [AudioProcessInfo(bundleID: "us.zoom.xos", isRunningOutput: out)] }
    let chromeOutOnly = [AudioProcessInfo(bundleID: "com.google.Chrome.helper", isRunningOutput: true)]
    let chromeCall =
        chromeOutOnly + [AudioProcessInfo(bundleID: "com.google.Chrome", isRunningOutput: false, isRunningInput: true)]

    var d = MeetingAutostartDetector()
    try expect(d.ingest(snapshot: zoom(true), now: 0, prefs: prefs, sessionActive: false).isEmpty, "no prompt at t=0")
    try expect(
        d.ingest(snapshot: zoom(true), now: 2.9, prefs: prefs, sessionActive: false).isEmpty, "no prompt before 3 s")
    try expect(
        d.ingest(snapshot: zoom(true), now: 3, prefs: prefs, sessionActive: false).map(\.id) == ["zoom"],
        "prompt after 3 s")
    try expect(
        d.ingest(snapshot: zoom(true), now: 5, prefs: prefs, sessionActive: false).isEmpty,
        "no repeat while audio continues")

    // Short blips (a notification chime) never prompt.
    d = MeetingAutostartDetector()
    _ = d.ingest(snapshot: zoom(true), now: 0, prefs: prefs, sessionActive: false)
    _ = d.ingest(snapshot: zoom(false), now: 2, prefs: prefs, sessionActive: false)
    try expect(
        d.ingest(snapshot: zoom(true), now: 4, prefs: prefs, sessionActive: false).isEmpty, "blip resets the 3 s clock")
    try expect(
        d.ingest(snapshot: zoom(true), now: 7, prefs: prefs, sessionActive: false).map(\.id) == ["zoom"],
        "sustained audio prompts after the reset")

    // 10 minute cooldown after a prompt, per app.
    _ = d.ingest(snapshot: zoom(false), now: 8, prefs: prefs, sessionActive: false)
    _ = d.ingest(snapshot: zoom(true), now: 9, prefs: prefs, sessionActive: false)
    try expect(
        d.ingest(snapshot: zoom(true), now: 100, prefs: prefs, sessionActive: false).isEmpty,
        "no re-prompt inside cooldown")
    var other = zoom(true) + [AudioProcessInfo(bundleID: "com.tinyspeck.slackmacgap", isRunningOutput: true)]
    _ = d.ingest(snapshot: other, now: 101, prefs: prefs, sessionActive: false)
    try expect(
        d.ingest(snapshot: other, now: 105, prefs: prefs, sessionActive: false).map(\.id) == ["slack"],
        "cooldown is per app")
    other = zoom(true)
    try expect(
        d.ingest(snapshot: other, now: 7 + 600 + 1, prefs: prefs, sessionActive: false).map(\.id) == ["zoom"],
        "prompts again once cooldown ends")

    // Never while a session runs, and the clock restarts afterwards.
    d = MeetingAutostartDetector()
    _ = d.ingest(snapshot: zoom(true), now: 0, prefs: prefs, sessionActive: true)
    try expect(
        d.ingest(snapshot: zoom(true), now: 10, prefs: prefs, sessionActive: true).isEmpty,
        "no prompt while a session runs")
    try expect(
        d.ingest(snapshot: zoom(true), now: 11, prefs: prefs, sessionActive: false).isEmpty,
        "session end restarts the 3 s clock")
    try expect(
        d.ingest(snapshot: zoom(true), now: 14, prefs: prefs, sessionActive: false).map(\.id) == ["zoom"],
        "prompts 3 s after the session ended")

    // Browsers need the mic open: a YouTube tab alone is not a call.
    d = MeetingAutostartDetector()
    _ = d.ingest(snapshot: chromeOutOnly, now: 0, prefs: prefs, sessionActive: false)
    try expect(
        d.ingest(snapshot: chromeOutOnly, now: 10, prefs: prefs, sessionActive: false).isEmpty,
        "browser audio without mic is not a call")
    _ = d.ingest(snapshot: chromeCall, now: 11, prefs: prefs, sessionActive: false)
    try expect(
        d.ingest(snapshot: chromeCall, now: 14, prefs: prefs, sessionActive: false).map(\.id) == ["chrome"],
        "browser audio with mic prompts")

    // Prefs: disabled, muted app, snooze, never-capture.
    var off = prefs
    off.detectionEnabled = false
    var muted = prefs
    muted.mutedPromptApps = ["zoom"]
    var snoozed = prefs
    snoozed.snoozedUntil = 100
    var never = prefs
    never.rules["zoom"] = .exclude
    for (name, p) in [("disabled", off), ("muted app", muted), ("never-capture app", never)] {
        d = MeetingAutostartDetector()
        _ = d.ingest(snapshot: zoom(true), now: 0, prefs: p, sessionActive: false)
        try expect(
            d.ingest(snapshot: zoom(true), now: 10, prefs: p, sessionActive: false).isEmpty, "\(name) never prompts")
    }
    d = MeetingAutostartDetector()
    _ = d.ingest(snapshot: zoom(true), now: 0, prefs: snoozed, sessionActive: false)
    try expect(
        d.ingest(snapshot: zoom(true), now: 50, prefs: snoozed, sessionActive: false).isEmpty, "snooze suppresses")
    try expect(
        d.ingest(snapshot: zoom(true), now: 100, prefs: snoozed, sessionActive: false).map(\.id) == ["zoom"],
        "prompt after snooze ends")

    for (bundle, id) in [
        ("us.zoom.xos", "zoom"), ("com.microsoft.teams2", "teams"), ("Cisco-Systems.Spark", "webex"),
        ("com.tinyspeck.slackmacgap.helper", "slack"), ("com.apple.FaceTime", "facetime"),
        ("com.google.Chrome.helper.Renderer", "chrome"), ("company.thebrowser.Browser.helper", "arc"),
        ("com.microsoft.edgemac", "edge"), ("com.brave.Browser", "brave"), ("com.apple.Safari", "safari"),
    ] {
        try expect(MeetingApp.app(forBundleID: bundle)?.id == id, "\(bundle) maps to \(id)")
    }
    try expect(MeetingApp.app(forBundleID: "com.google.ChromeRemoteDesktop") == nil, "prefix needs a dot boundary")
}

func checkCaptureScope() throws {
    var prefs = MeetingAutostartPrefs()
    try expect(prefs.scope() == .everything(excluding: []), "default excludes nothing")
    prefs.rules["chrome"] = .exclude
    try expect(prefs.scope() == .everything(excluding: ["chrome"]), "never-capture app is excluded")
    prefs.rules["zoom"] = .include
    try expect(prefs.scope() == .only(["zoom"]), "an include rule narrows to included apps")
    try expect(prefs.scope(forApp: MeetingApp.app(id: "teams")!) == .only(["teams"]), "prompt scope is the one app")
    try expect(
        prefs.scope(forApp: MeetingApp.app(id: "chrome")!) == .only([]), "excluded app stays excluded in prompt scope")

    let decoded = try JSONDecoder().decode(MeetingAutostartPrefs.self, from: JSONEncoder().encode(prefs))
    try expect(decoded == prefs, "prefs round-trip through JSON")

    guard #available(macOS 14.2, *) else { return }
    let processes = [
        AudioProcessInfo(objectID: 10, bundleID: "us.zoom.xos", isRunningOutput: true),
        AudioProcessInfo(objectID: 11, bundleID: "com.google.Chrome.helper", isRunningOutput: true),
        AudioProcessInfo(objectID: 12, bundleID: "com.google.Chrome", isRunningOutput: false),
        AudioProcessInfo(objectID: 13, bundleID: "org.example.custom", isRunningOutput: true),
    ]
    func ids(_ d: CATapDescription) -> [UInt32] { d.processes.sorted() }

    let global = try TapDescriptionBuilder.make(
        scope: .everything(excluding: []), processes: processes, ownObjectID: 99)
    try expect(global.isExclusive && ids(global) == [99], "default tap is global, excluding only MimiVox itself")
    let never = try TapDescriptionBuilder.make(
        scope: .everything(excluding: ["chrome", "org.example.custom"]), processes: processes, ownObjectID: 99)
    try expect(
        never.isExclusive && ids(never) == [11, 12, 13, 99],
        "never-capture excludes every matching process incl. custom bundle IDs")
    let only = try TapDescriptionBuilder.make(scope: .only(["zoom"]), processes: processes, ownObjectID: 99)
    try expect(!only.isExclusive && only.isMixdown && ids(only) == [10], "include scope taps only the chosen app")
    do {
        _ = try TapDescriptionBuilder.make(scope: .only(["teams"]), processes: processes, ownObjectID: 99)
        throw CheckFailure(description: "include scope with no running process should throw")
    } catch SystemAudioCaptureError.noMatchingProcesses {}
}

func checkLibrarySearch() async throws {
    let searchJSON = #"""
        {"query":"予算","total_sessions":1,"speakers":["田中","部長"],"tags":["Finance"],
         "results":[{"id":"s1","label":"Weekly sync","started_at":"2026-07-01T09:00:00+00:00",
           "line_count":2,"tags":["Finance"],"match_count":1,"matched_in":["ja","tag"],
           "lines":[{"line_id":7,"start":12.5,"speaker":"部長",
             "ja":"来期の予算です","en":"Next year's budget.",
             "ja_snippet":[{"t":"来期の","hit":false},{"t":"予算","hit":true},{"t":"です","hit":false}],
             "en_snippet":[]}]}]}
        """#
    let client = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8765/")!,
        transport: MockTransport { request in
            let url = try unwrap(request.url, "search url")
            try expect(url.path == "/api/sessions/search", "search path")
            let items = URLComponents(url: url, resolvingAgainstBaseURL: false)?.queryItems ?? []
            func value(_ name: String) -> String? { items.first { $0.name == name }?.value }
            try expect(value("q") == "予算 部長", "query round-trips with Japanese and a space")
            try expect(value("speaker") == "部長", "speaker filter sent")
            try expect(value("since") == "2026-07-01", "since sent")
            try expect(value("until") == nil && value("tag") == nil, "empty filters omitted")
            let response = HTTPURLResponse(url: url, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (Data(searchJSON.utf8), response)
        }
    )
    let found = try await client.searchSessions(query: "予算 部長", since: "2026-07-01", speaker: "部長")
    try expect(
        found.totalSessions == 1 && found.speakers == ["田中", "部長"] && found.tags == ["Finance"], "facets decode")
    let hit = try unwrap(found.results.first, "one session hit")
    try expect(
        hit.id == "s1" && hit.matchCount == 1 && hit.matchedIn == ["ja", "tag"] && hit.tags == ["Finance"],
        "session hit decodes")
    let line = try unwrap(hit.lines.first, "one line hit")
    try expect(line.lineID == 7 && line.start == 12.5 && line.speaker == "部長", "line hit decodes")
    try expect(line.jaSnippet.filter(\.hit).map(\.text) == ["予算"], "snippet marks the hit")
    try expect(line.jaSnippet.map(\.text).joined() == line.ja, "snippet reassembles the line")
    try expect(line.enSnippet.isEmpty, "unmatched field has no snippet")

    let minimal = try JSONDecoder().decode(LibrarySearchResponse.self, from: Data(#"{"results":[{"id":"x"}]}"#.utf8))
    try expect(minimal.results.first?.lines.isEmpty == true && minimal.speakers.isEmpty, "missing fields default")

    let tagClient = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8765/")!,
        transport: MockTransport { request in
            try expect(request.url?.path == "/api/sessions/s1/tags" && request.httpMethod == "POST", "tags path")
            let body = try unwrap(request.httpBody, "tags body")
            let object = try unwrap(JSONSerialization.jsonObject(with: body) as? [String: Any], "tags json")
            try expect(object["add"] as? [String] == ["Finance"] && object["remove"] as? [String] == [], "tags encode")
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (Data(#"{"session_id":"s1","tags":["Finance"]}"#.utf8), response)
        }
    )
    let tags = try await tagClient.updateSessionTags(id: "s1", add: ["Finance"])
    try expect(tags.sessionID == "s1" && tags.tags == ["Finance"], "tags response decodes")

    let detail = try JSONDecoder().decode(ArchivedSessionDetail.self, from: Data(#"{"id":"s1","tags":["a"]}"#.utf8))
    try expect(detail.tags == ["a"], "session detail carries tags")
    let old = try JSONDecoder().decode(ArchivedSessionDetail.self, from: Data(#"{"id":"s1"}"#.utf8))
    try expect(old.tags.isEmpty, "archives without tags still decode")
}

func checkPrivacyDecoding() async throws {
    let json = #"""
        {"state":"idle","events":[],"settings":{"retention_policy":"7d"},
         "model_readiness":{"ready":true,"next_steps":[]},
         "privacy":{"on_device":true,"retention_policy":"7d","retention_days":7,
                    "offline_locked":true,"network_egress_enabled":false,"egress_paths":[],
                    "reason":"models cached locally"},
         "health":{}}
        """#
    let status = try JSONDecoder().decode(MeetingStatusResponse.self, from: Data(json.utf8))
    let privacy = try unwrap(status.privacy, "privacy block should decode")
    try expect(privacy.offlineLocked && !privacy.networkEgressEnabled, "offline flags should decode")
    try expect(privacy.indicatorText == "On-device · offline", "locked server reads On-device · offline")
    try expect(privacy.retention == .days7, "retention policy should decode")
    try expect(status.settings.retentionPolicy == "7d", "settings retention_policy should decode")

    let open = PrivacyStatus(
        offlineLocked: false, networkEgressEnabled: true, egressPaths: ["POST /api/models/download"])
    try expect(open.indicatorText != "On-device · offline", "unlocked server must not claim offline")

    let old = try JSONDecoder().decode(
        MeetingStatusResponse.self,
        from: Data(
            #"{"state":"idle","events":[],"settings":{},"model_readiness":{"ready":false,"next_steps":[]},"health":{}}"#
                .utf8))
    try expect(old.privacy == nil, "older servers omit privacy")
    try expect(old.settings.retentionPolicy == "forever", "retention defaults to forever")
    try expect(RetentionPolicy(serverValue: "garbage") == .forever, "unknown policy falls back to forever")
    try expect(RetentionPolicy.none.label == "Don't save transcripts", "none is the don't-save choice")
    try expect(RetentionPolicy.days7.keepsLess(than: .days30), "a shorter policy deletes saved meetings")
    try expect(RetentionPolicy.none.keepsLess(than: .forever), "not saving deletes saved meetings")
    try expect(!RetentionPolicy.days30.keepsLess(than: .days7), "a longer policy deletes nothing")
    try expect(!RetentionPolicy.days7.keepsLess(than: .days7), "the same policy deletes nothing")

    let data = try JSONEncoder().encode(MeetingSettings(retentionPolicy: "1d"))
    let object = try unwrap(
        JSONSerialization.jsonObject(with: data) as? [String: Any], "settings json should be object")
    try expect(object["retention_policy"] as? String == "1d", "retention_policy key should encode")

    let client = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8786")!,
        transport: MockTransport { request in
            try expect(
                request.httpMethod == "DELETE" && request.url?.path == "/api/sessions",
                "delete all history is DELETE /api/sessions")
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            return (Data(#"{"message":"deleted"}"#.utf8), response)
        }
    )
    let result = try await client.deleteAllHistory()
    try expect(result.message == "deleted", "delete all history should decode the response")
}

func checkLatencyHealth() async throws {
    let json = #"""
        {"state":"listening","events":[],"settings":{"catch_up_mode":"auto"},
         "model_readiness":{"ready":true,"next_steps":[]},
         "health":{"latency":{"state":"behind","backlog_s":6.2,
           "stages":[{"stage":"vad"},{"stage":"asr"},{"stage":"cat","queue_depth":2}],
           "reasons":[{"code":"notes_gpu","message":"Notes or Ask is running; captions may lag briefly"},
                      {"code":"translate_slow","message":"Translation is the bottleneck"}],
           "catching_up":false,"catch_up_available":true,"recommended_mode":"fast",
           "skipped_translations":2,"gaps":1,"gap_audio_s":5.0}}}
        """#
    let status = try JSONDecoder().decode(MeetingStatusResponse.self, from: Data(json.utf8))
    let health = status.health.latency
    try expect(health.state == .behind && health.backlogSeconds == 6.2, "latency state and backlog decode")
    try expect(health.reasonText == "Notes or Ask is running; captions may lag briefly", "first reason leads")
    try expect(health.isNotesPause, "a notes request reads as a brief pause, not an overload")
    try expect(health.catchUpAvailable && health.recommendedMode == "fast", "catch-up availability decodes")
    try expect(
        health.detailText.contains("Translation is the bottleneck")
            && health.detailText.contains("Review can translate"), "detail lists causes and the fast-path promise")
    try expect(status.settings.catchUpMode == "auto", "catch_up_mode decodes")

    let skip = LatencyHealth(state: .overloaded, backlogSeconds: 14, recommendedMode: "skip")
    try expect(skip.reasonText == "Captions are 14s behind the speaker", "no reasons falls back to the backlog")
    try expect(LatencyHealth().isOK && LatencyHealth().reasonText == nil, "healthy snapshot is quiet")

    let old = try JSONDecoder().decode(
        MeetingStatusResponse.self,
        from: Data(
            #"{"state":"idle","events":[],"settings":{},"model_readiness":{"ready":false,"next_steps":[]},"health":{}}"#
                .utf8))
    try expect(old.health.latency.isOK && old.settings.catchUpMode == "manual", "older servers read as ok and manual")
    let unknown = try JSONDecoder().decode(LatencyHealth.self, from: Data(#"{"state":"melting"}"#.utf8))
    try expect(unknown.state == .ok, "an unknown state reads as ok")
    let encoded = try JSONEncoder().encode(MeetingSettings(catchUpMode: "off"))
    let object = try unwrap(JSONSerialization.jsonObject(with: encoded) as? [String: Any], "settings json")
    try expect(object["catch_up_mode"] as? String == "off", "catch_up_mode encodes")
    try expect(object["asr_engine"] as? String == "haas", "asr_engine defaults to haas")
    try expect(old.settings.asrEngine == "haas", "missing asr_engine decodes as haas")

    let events = try JSONDecoder().decode(
        [MeetingEvent].self,
        from: Data(
            #"""
            [{"type":"LatencyHealthEvent","health":{"state":"overloaded","backlog_s":11,"catch_up_available":true},"timestamp_s":3},
             {"type":"AsrCompletedEvent","index":1,"start":0,"end":2,"text":"最初"},
             {"type":"TranslationCompletedEvent","index":1,"start":0,"end":2,"text":"First.","source_indexes":[1]},
             {"type":"AsrCompletedEvent","index":2,"start":2,"end":4,"text":"翻訳なし","translation_skipped":"catch_up"},
             {"type":"CaptionGapEvent","index":3,"start":4,"end":9,"recoverable":true,"message":"Skipped 5s of speech while catching up"}]
            """#.utf8))
    try expect(events[0].health?.state == .overloaded, "LatencyHealthEvent carries the snapshot")
    let rows = events.bilingualTranscriptRows
    try expect(rows.count == 3, "gap row sits beside the two caption rows")
    try expect(rows[0].skipNote == nil, "translated row has no skip note")
    try expect(rows[1].skipNote == "Translation skipped while catching up", "untranslated row says so")
    try expect(
        rows[2].gap?.recoverable == true && rows[2].skipNote == "Skipped 5s of speech while catching up",
        "gap row shows its message")
    try expect(
        rows[2].japanese == nil && rows[2].english == nil && rows[2].start == 4,
        "gap row has no text and keeps its time")

    let client = MeetingTranslatorAPIClient(
        baseURL: URL(string: "http://127.0.0.1:8786")!,
        transport: MockTransport { request in
            let response = HTTPURLResponse(url: request.url!, statusCode: 200, httpVersion: nil, headerFields: nil)!
            if request.url?.path == "/api/session/catch-up" {
                try expect(request.httpMethod == "POST", "catch-up is a POST")
                return (
                    Data(#"{"message":"caught up","mode":"fast","translations_skipped":3,"gaps":0}"#.utf8), response
                )
            }
            try expect(
                request.url?.path == "/api/sessions/s%201/recover" || request.url?.path == "/api/sessions/s 1/recover",
                "recover addresses the session")
            return (Data(#"{"recovered_gaps":2,"retranslated":1,"remaining":0}"#.utf8), response)
        }
    )
    let caught = try await client.catchUp()
    try expect(caught.translationsSkipped == 3 && caught.mode == "fast", "catch-up response decodes")
    let recovered = try await client.recoverSkipped(sessionID: "s 1")
    try expect(recovered.summary == "Recovered 2 skipped spans and translated 1 line", "recover summary")
    try expect(RecoverResponse.none.summary == "Nothing to recover", "empty recovery says so")
}

func checkLanguagePairs() throws {
    try expect(MeetingSettings().languagePair == .japaneseToEnglish, "default pair is ja->en")
    try expect(!LanguagePair.japaneseToEnglish.needsTranslationModel, "ja->en needs no extra model")
    try expect(LanguagePair.supported.map(\.id) == ["ja-en", "zh-en", "ko-en", "en-ja"], "supported pairs")

    // Legacy settings JSON without the keys decodes to ja->en.
    let legacy = try JSONDecoder().decode(MeetingSettings.self, from: Data("{}".utf8))
    try expect(legacy.sourceLang == "ja" && legacy.targetLang == "en", "legacy settings default to ja->en")

    var settings = MeetingSettings()
    settings.languagePair = LanguagePair.resolve(source: "ko", target: "en")
    let data = try JSONEncoder().encode(settings)
    let object = try unwrap(try JSONSerialization.jsonObject(with: data) as? [String: Any], "settings object")
    try expect(
        object["source_lang"] as? String == "ko" && object["target_lang"] as? String == "en",
        "language pair encodes as source_lang/target_lang")
    let roundTrip = try JSONDecoder().decode(MeetingSettings.self, from: data)
    try expect(roundTrip.languagePair.id == "ko-en", "pair round-trips")
    try expect(LanguagePair.resolve(source: "fr", target: "en") == .japaneseToEnglish, "unknown pair falls back")

    let start = try JSONEncoder().encode(
        StartSessionRequest(
            source: "replay-file", languagePair: LanguagePair.resolve(source: "en", target: "ja")))
    let startObject = try unwrap(try JSONSerialization.jsonObject(with: start) as? [String: Any], "start object")
    try expect(
        startObject["source_lang"] as? String == "en" && startObject["target_lang"] as? String == "ja",
        "start request carries the pair")
    let bare = try JSONSerialization.jsonObject(with: JSONEncoder().encode(StartSessionRequest(source: "microphone")))
    try expect((bare as? [String: Any])?["source_lang"] == nil, "pair omitted when not given")
}

func checkLiveStatus() throws {
    func status(_ state: String, audio: Bool = true, degraded: Bool = false, error: String? = nil)
        -> MeetingStatusResponse
    {
        MeetingStatusResponse(
            state: state, health: RuntimeHealth(audioInputActive: audio, degraded: degraded), error: error
        )
    }
    func derive(
        helper: HelperPhase = .ready(port: 1), setup: SetupState? = .ready, _ status: MeetingStatusResponse?,
        latency: LatencyHealth = LatencyHealth()
    ) -> LiveStatus {
        LiveStatus(helper: helper, setup: setup, status: status, latency: latency)
    }
    let behind = LatencyHealth(state: .behind, backlogSeconds: 8.6, catchUpAvailable: true)

    try expect(
        derive(helper: .failed(.portInUse(port: 1)), status("idle")) == .failed, "a failed helper reads as failed")
    try expect(derive(helper: .starting(port: 1), setup: nil, nil) == .starting, "no status yet reads as starting")
    try expect(
        derive(helper: .starting(port: 1), status("listening")) == .starting,
        "a restarting helper reads as starting, not live")
    try expect(
        derive(setup: .needsDownload(totalBytes: 10, freeDiskBytes: 20), status("idle")) == .setupNeeded,
        "missing models read as setup")
    try expect(derive(setup: .loading, status("idle")) == .starting, "loading models reads as starting")
    try expect(derive(status("idle")) == .idle, "an idle server reads as idle")
    try expect(
        derive(status("completed", error: "stale error")) == .idle,
        "an error left on a finished session is not a failure")
    try expect(derive(status("loading_models")) == .starting, "loading models in a session reads as starting")
    try expect(derive(status("listening", audio: false)) == .starting, "listening without input yet reads as starting")
    try expect(derive(status("listening")) == .live, "listening with input reads as live")
    try expect(derive(status("paused")) == .paused, "paused reads as paused")
    try expect(derive(status("stopping")) == .stopping, "stopping reads as stopping")
    try expect(derive(status("listening"), latency: behind) == .behind(seconds: 9), "lag rounds to whole seconds")
    try expect(LiveStatus.behind(seconds: 9).title == "Behind 9 s", "behind title carries the lag")
    try expect(
        derive(status("listening"), latency: LatencyHealth(catchingUp: true)) == .catchingUp,
        "catch-up reads as catching up")
    try expect(
        derive(status("degraded", error: "Audio input stopped")) == .failed, "a degraded session reads as failed")
    try expect(derive(status("listening", degraded: true)) == .failed, "degraded health reads as failed")
    try expect(derive(status("listening", error: "boom")) == .failed, "an error on a running session reads as failed")

    let quiet: [LiveStatus] = [.idle, .setupNeeded]
    let signalling: [LiveStatus] = [.starting, .live, .paused, .stopping, .catchingUp, .behind(seconds: 9), .failed]
    try expect(
        quiet.allSatisfy { !$0.showsToolbarPill }, "idle and setup leave the toolbar pill to the window subtitle")
    try expect(signalling.allSatisfy(\.showsToolbarPill), "every other state earns the pill")
}

func checkArchivedSessionSummary() throws {
    let full =
        #"{"id":"a","started_at":"2026-11-22T11:33:00+00:00","label":"system-audio","event_count":12,"duration_seconds":61.5,"headline":"Quote is 20 percent higher."}"#
    let entry = try JSONDecoder().decode(ArchivedSessionSummary.self, from: Data(full.utf8))
    try expect(entry.durationSeconds == 61.5, "an index entry carries its duration")
    try expect(entry.headline == "Quote is 20 percent higher.", "an index entry carries the notes headline")

    let old = #"{"id":"b","started_at":"2026-11-20T09:00:00+00:00","label":"microphone","event_count":3,"headline":""}"#
    let legacy = try JSONDecoder().decode(ArchivedSessionSummary.self, from: Data(old.utf8))
    try expect(legacy.durationSeconds == nil, "an entry indexed before durations has none")
    try expect(legacy.headline == nil, "an empty headline reads as none")
}

func checkLiveTranscript() throws {
    func asr(_ line: Int, _ text: String, at start: Double) -> MeetingEvent {
        MeetingEvent(type: "AsrCompletedEvent", index: line, start: start, text: text)
    }
    func translation(_ line: Int, _ text: String, at start: Double, sources: [Int]? = nil) -> MeetingEvent {
        MeetingEvent(
            type: "TranslationCompletedEvent", index: line, start: start, text: text, sourceIndexes: sources ?? [line])
    }

    var transcript = LiveTranscript()
    transcript.apply(asr(0, "はい", at: 1))
    let pendingID = transcript.rows[0].id
    try expect(transcript.rows[0].english == nil, "a line shows before its translation")
    transcript.apply(translation(0, "Yes.", at: 1))
    try expect(
        transcript.rows.count == 1 && transcript.rows[0].id == pendingID, "a landing translation keeps the row id")
    try expect(transcript.rows[0].english?.text == "Yes.", "the translation fills its row")

    transcript.apply(translation(2, "Early.", at: 9))
    try expect(
        transcript.rows.map(\.index) == [0, 2] && transcript.rows[1].japanese == nil,
        "a translation without its ASR gets its own row")
    transcript.apply(asr(2, "早い", at: 9))
    try expect(transcript.rows.count == 2 && transcript.rows[1].japanese?.text == "早い", "the late ASR joins that row")

    transcript.apply(asr(1, "途中", at: 5))
    try expect(transcript.rows.map(\.index) == [0, 1, 2], "rows stay in time order when a line arrives late")
    try expect(
        transcript.rows.latestJapanese?.text == "早い" && transcript.rows.latestEnglish?.text == "Early.",
        "latest reads the newest row")

    transcript.apply(
        MeetingEvent(
            type: "TranscriptRevisionEvent", index: 1, end: 10, text: "Midway, early.", japanese: "途中早い",
            mergedIndexes: [2]))
    try expect(transcript.rows.map(\.index) == [0, 1], "a revision removes the merged row")
    try expect(
        transcript.rows[1].isRevised && transcript.rows[1].japanese?.text == "途中早い", "a revision rewrites its target")

    transcript.apply(MeetingEvent(type: "AsrPartialEvent", index: 3, start: 12, text: "次"))
    transcript.apply(MeetingEvent(type: "AudioLevelEvent", level: 0.4))
    try expect(transcript.rows.count == 2, "partials and levels never add rows")
    try expect(
        !LiveTranscript.changesRows("AsrPartialEvent") && LiveTranscript.changesRows("CaptionGapEvent"),
        "row-changing kinds are named")

    let gap = MeetingEvent(type: "CaptionGapEvent", index: 100, start: 3, end: 4, reason: "catch_up")
    transcript.apply(gap)
    try expect(
        transcript.rows.map(\.index) == [0, 100, 1] && transcript.rows[1].gap != nil,
        "a gap sits where the speech was skipped")
}

func checkChangelogParsing() throws {
    let md = """
        # Changelog
        ## [1.1.0] - 2026-02-01
        ### Added
        - First thing
          continued here
        - Second
        ### Fixed
        - Bug
        ## [1.0.0]
        - Plain
        """
    let r = Changelog.parse(md)
    try expect(r.count == 2, "release count")
    try expect(r[0].version == "1.1.0" && r[0].date == "2026-02-01", "heading parse")
    try expect(r[0].sections.map(\.title) == ["Added", "Fixed"], "sections")
    try expect(r[0].sections[0].items == ["First thing continued here", "Second"], "items")
    try expect(r[1].date == nil && r[1].sections[0].title == "Changes", "bare release")

    let withUnreleased = Changelog.parse("## [Unreleased]\n- Next\n## [1.0.0] - 2026-01-01\n- Shipped")
    try expect(Changelog.latestRelease(in: withUnreleased)?.version == "1.0.0", "latest release skips Unreleased")
}

/// The bundled changelog's newest release must match the app's Info.plist version.
func checkQuitConfirmation() throws {
    try expect(
        QuitConfirmation.prompt(meetingActive: true, paused: false, idleSuppressed: true)
            == .activeMeeting(paused: false),
        "a live meeting always asks, even after Don't ask again"
    )
    try expect(
        QuitConfirmation.prompt(meetingActive: true, paused: true, idleSuppressed: false)
            == .activeMeeting(paused: true),
        "a paused meeting still counts as in progress"
    )
    try expect(
        QuitConfirmation.prompt(meetingActive: false, paused: false, idleSuppressed: false) == .idle,
        "idle quit asks by default"
    )
    try expect(
        QuitConfirmation.prompt(meetingActive: false, paused: false, idleSuppressed: true) == nil,
        "Don't ask again skips the idle prompt"
    )
}

func checkChangelogMatchesBundleVersion() throws {
    let packageRoot = URL(fileURLWithPath: #filePath)
        .deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
    let changelog = try String(
        contentsOf: packageRoot.appendingPathComponent("Sources/MeetingTranslatorApp/Resources/CHANGELOG.md"),
        encoding: .utf8
    )
    let plistData = try Data(contentsOf: packageRoot.appendingPathComponent("BundleResources/Info.plist"))
    let plist = try PropertyListSerialization.propertyList(from: plistData, format: nil) as? [String: Any]
    let bundleVersion = plist?["CFBundleShortVersionString"] as? String
    let latest = Changelog.latestRelease(in: Changelog.parse(changelog))?.version
    try expect(
        latest == bundleVersion,
        "CHANGELOG.md latest release (\(latest ?? "none")) must match Info.plist CFBundleShortVersionString (\(bundleVersion ?? "none"))"
    )
}

@main
enum MeetingTranslatorCoreChecks {
    static func main() async throws {
        if CommandLine.arguments.contains("--list-audio-processes") {
            listAudioProcesses()
            return
        }
        try checkModelDecoding()
        try await checkSpeakerDecoding()
        try checkDeviceDecoding()
        try checkSettingsEncoding()
        try checkLanguagePairs()
        try await checkGlossaryProfiles()
        try await checkAPIRequests()
        try await checkPrivacyDecoding()
        try await checkNativeSystemAudioRequests()
        try await checkSystemAudioStreamer()
        try checkRevisionDecoding()
        try checkLiveTranscript()
        try checkLiveStatus()
        try checkArchivedSessionSummary()
        try checkMeetingAutostartDetector()
        try checkCaptureScope()
        try await checkLibrarySearch()
        try checkSetupState()
        try checkModelSource()
        try checkChangelogParsing()
        try checkChangelogMatchesBundleVersion()
        try checkQuitConfirmation()
        try checkHelperConfiguration()
        try checkBundledConfiguration()
        try await checkHelperLifecycle()
        try checkCaptionDisplayModes()
        try checkMeetingInsights()
        try checkPinnedLines()
        try await checkPinAPIRequests()
        try await checkArchivedSpeakerRenameRequest()
        try checkReadAloudQueue()
        try checkReadAloudCoordinator()
        try await checkSpeechRenderer()
        try await checkMeetingNotes()
        try await checkLatencyHealth()
        try await checkLearningMode()
        print("MeetingTranslatorCoreChecks passed")
    }
}
