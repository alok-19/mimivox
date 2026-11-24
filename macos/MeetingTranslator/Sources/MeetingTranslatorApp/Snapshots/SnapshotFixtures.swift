import Foundation
import MeetingTranslatorCore

/// Canned data for the UI snapshot tool. Pure values: nothing here starts the
/// helper, touches the network or reads audio.
enum SnapshotFixtures {
    static let referenceDate =
        ISO8601DateFormatter().date(from: "2026-11-22T10:42:00Z") ?? Date(timeIntervalSince1970: 0)
    static let tanaka = SpeakerTag(id: "speaker-1", label: "Tanaka")
    static let suzuki = SpeakerTag(id: "speaker-2", label: "Suzuki")
    static let you = SpeakerTag(id: "speaker-3", label: "Speaker 3")

    private struct Line {
        var ja: String
        var en: String
        var speaker: SpeakerTag?
        var confidence: CaptionConfidence.Level = .high
        var late = false
    }

    private static let lines: [Line] = [
        Line(ja: "それでは、定例ミーティングを始めます。", en: "Let's get started with the regular meeting.", speaker: tanaka),
        Line(
            ja: "先週のリリースについて、まず状況を共有させてください。", en: "First, let me share the status of last week's release.",
            speaker: tanaka),
        Line(ja: "はい、お願いします。", en: "Yes, please go ahead.", speaker: suzuki),
        Line(
            ja: "ビルドは問題なく通りましたが、一部の端末で表示が崩れています。",
            en: "The build passed without issues, but the layout breaks on some devices.", speaker: tanaka),
        Line(ja: "それは古いOSのバージョンだけですか。", en: "Is that only on older OS versions?", speaker: suzuki),
        Line(
            ja: "そうですね、原因はまだ特定できていませんが、", en: "Right, we haven't pinned down the cause yet, but", speaker: tanaka,
            confidence: .medium),
        Line(ja: "たぶんフォントの問題だと思います。", en: "I think it's probably a font issue.", speaker: tanaka),
        Line(ja: "来週の月曜日までに修正版を出せますか。", en: "Can you get a fix out by next Monday?", speaker: suzuki),
        Line(ja: "努力しますが、保証はできません。", en: "I'll try, but I can't promise.", speaker: tanaka, confidence: .low),
        Line(ja: "では、予算の件に移りましょう。", en: "Then let's move on to the budget.", speaker: you, late: true),
        Line(
            ja: "追加のテスト端末を五台購入したいと考えています。", en: "We would like to purchase five additional test devices.",
            speaker: suzuki),
        Line(ja: "承知しました。見積もりを送ってください。", en: "Understood. Please send me a quote.", speaker: you),
    ]

    /// Twelve bilingual rows, a revision (rows 5 and 6 merged), a low-confidence
    /// line, a late translation, a gap, and a trailing partial (being spoken now).
    static func liveEvents(withPartial: Bool = true, learning: Bool = false) -> [MeetingEvent] {
        var events: [MeetingEvent] = [MeetingEvent(type: "SessionStartedEvent", message: "Session started")]
        var t = 4.0
        for (index, line) in lines.enumerated() {
            let confidence = CaptionConfidence(
                level: line.confidence,
                reasons: line.confidence == .high ? [] : ["asr_uncertain"],
                asr: line.confidence == .low ? 0.38 : 0.82,
                mt: 0.74
            )
            events.append(
                MeetingEvent(
                    type: "AsrCompletedEvent", index: index, start: t, end: t + 3.0, text: line.ja,
                    speaker: line.speaker, confidence: confidence,
                    tokens: learning ? learningTokens(for: line.ja) : nil
                ))
            events.append(
                MeetingEvent(
                    type: "TranslationCompletedEvent", index: index, start: t, end: t + 3.0, text: line.en,
                    sourceIndexes: [index], displayLatencySeconds: line.late ? 4.6 : 1.1, isLate: line.late,
                    speaker: line.speaker, confidence: confidence
                ))
            t += 4.5
            if index == 8 {
                events.append(
                    MeetingEvent(
                        type: "CaptionGapEvent", index: 100, start: t - 1.0, end: t + 3.5,
                        reason: "catch_up", recoverable: true
                    ))
                t += 5
            }
        }
        events.append(MeetingEvent(type: "AudioLevelEvent", level: 0.62))
        events.append(
            MeetingEvent(
                type: "TranscriptRevisionEvent", index: 5, start: 26.5, end: 36.0,
                text: "Right, we haven't pinned down the cause yet, but I think it's probably a font issue on iOS 15.",
                japanese: "そうですね、原因はまだ特定できていませんが、たぶんiOS15のフォントの問題だと思います。",
                mergedIndexes: [6],
                confidence: CaptionConfidence(level: .medium, reasons: ["merged"], asr: 0.8, mt: 0.7)
            ))
        if withPartial {
            events.append(
                MeetingEvent(type: "AsrPartialEvent", index: 12, start: t, text: "その件については来月の", speaker: tanaka))
        }
        return events
    }

    private static func learningTokens(for text: String) -> [LearningToken]? {
        guard text == lines[0].ja else { return nil }
        return [
            LearningToken(surface: "それでは", base: "それでは", gloss: "well then"),
            LearningToken(surface: "、"),
            LearningToken(surface: "定例", reading: "ていれい", base: "定例", gloss: "regular"),
            LearningToken(surface: "ミーティング", base: "ミーティング", gloss: "meeting"),
            LearningToken(surface: "を"),
            LearningToken(surface: "始め", reading: "はじめ", base: "始める", gloss: "to begin"),
            LearningToken(surface: "ます"),
            LearningToken(surface: "。"),
        ]
    }

    static let readyReadiness = ModelReadiness(
        ready: true,
        whisper: ModelReadinessItem(
            ready: true, message: "Whisper large-v3-turbo ready", path: "models/whisper", modelID: "large-v3-turbo",
            displayName: "Whisper large-v3-turbo"),
        catTranslate: ModelReadinessItem(
            ready: true, message: "CAT translate ready", path: "models/cat", displayName: "CAT-Translate 0.8B"),
        glossary: ModelReadinessItem(ready: true, message: "3 glossary terms", termCount: 3),
        notesLlm: ModelReadinessItem(ready: true, message: "Qwen notes model ready", displayName: "Qwen3 4B")
    )

    static let missingReadiness = ModelReadiness(
        ready: false,
        nextSteps: [
            "Download the Whisper speech model (about 1.6 GB)",
            "Download the translation model",
            "Run Verify Models once the downloads finish",
        ],
        whisper: ModelReadinessItem(ready: false, message: "Model not downloaded"),
        catTranslate: ModelReadinessItem(ready: false, message: "Model not downloaded"),
        glossary: ModelReadinessItem(ready: true, message: "No glossary terms", termCount: 0),
        notesLlm: ModelReadinessItem(ready: false, message: "Optional: not downloaded")
    )

    static let privacy = PrivacyStatus(
        onDevice: true, retentionPolicy: "forever", offlineLocked: true, networkEgressEnabled: false)

    static let devices = [
        AudioDevice(id: "mic-1", name: "MacBook Pro Microphone"),
        AudioDevice(id: "mic-2", name: "Studio Display Microphone"),
    ]

    static let glossary = [
        GlossaryTerm(source: "リリース", target: "release"),
        GlossaryTerm(source: "定例", target: "weekly sync"),
        GlossaryTerm(source: "見積もり", target: "quote"),
    ]

    static func settings(learning: Bool = false) -> MeetingSettings {
        MeetingSettings(
            microphoneDevice: "mic-1",
            glossaryTerms: glossary,
            glossaryProfiles: [
                GlossaryProfile(name: "Engineering", entries: glossary), GlossaryProfile(name: "Sales", entries: []),
            ],
            activeGlossaryProfile: "Engineering",
            learningMode: learning
        )
    }

    static func health(
        label: String? = nil, audio: Bool = true, asr: Bool = true, translation: Bool = true,
        degraded: Bool = false, catchingUp: Bool = false, latency: LatencyHealth = LatencyHealth()
    ) -> RuntimeHealth {
        RuntimeHealth(
            audioInputActive: audio, asrWorkerActive: asr, translationWorkerActive: translation,
            queueLagSeconds: latency.backlogSeconds, realtimeLagSeconds: 0.9, activeSpeech: audio,
            modelReady: true, degraded: degraded, sessionLabel: label, catchingUp: catchingUp, latency: latency
        )
    }

    static let summary = SessionPerformanceSummary(
        asrCount: 12, translationCount: 12, emptyAsrCount: 1, staleDrops: 0, lateTranslations: 1,
        displayLatencySeconds: LatencyPercentileSummary(p50: 1.1, p90: 3.8), asrBackend: "whisper-mlx"
    )

    static func status(
        _ state: String, readiness: ModelReadiness = readyReadiness, health: RuntimeHealth = health(),
        error: String? = nil, learning: Bool = false
    ) -> MeetingStatusResponse {
        MeetingStatusResponse(
            state: state, sessionID: state == "idle" ? nil : "live-0001", settings: settings(learning: learning),
            modelReadiness: readiness, privacy: privacy, health: health, performanceSummary: summary, error: error,
            activeModels: state == "listening" ? activeModels : nil
        )
    }

    static let activeModels = ActiveModels(
        asr: ActiveModel(id: "basyauma-tech/Haas-ja-v1.0-mlx-8bit", label: "Haas-ja"),
        translation: ActiveModel(id: "hotchpotch/CAT-Translate-1.4b-mlx-q4", label: "CAT-Translate")
    )

    static let archived = [
        ArchivedSessionSummary(
            id: "2026-11-20T09-30-00", startedAt: "2026-11-22T08:15:00+00:00", label: "system-audio", eventCount: 214,
            durationSeconds: 62, headline: "The team reviewed last week's release."
        ),
        ArchivedSessionSummary(
            id: "2026-11-22T06-50-00", startedAt: "2026-11-22T06:50:00+00:00", label: "microphone", eventCount: 58,
            durationSeconds: 312
        ),
        ArchivedSessionSummary(
            id: "2026-11-18T14-00-00", startedAt: "2026-11-21T14:00:00+00:00", label: "microphone", eventCount: 96,
            durationSeconds: 1620
        ),
        ArchivedSessionSummary(
            id: "2026-11-12T10-15-00", startedAt: "2026-11-12T10:15:00+00:00", label: "meeting-recording.wav",
            eventCount: 143, durationSeconds: 95
        ),
    ]

    static let notes = MeetingNotes(
        summaryEn:
            "The team reviewed last week's release. The build passed, but the layout breaks on some older devices, likely a font issue. A fix is targeted for next Monday, and five extra test devices will be purchased.",
        summaryJa: "先週のリリースを確認しました。ビルドは成功しましたが、古い端末で表示が崩れています。",
        decisions: [
            NotesDecision(text: "Aim to ship the font fix by next Monday", lineIDs: [7]),
            NotesDecision(text: "Buy five additional test devices", lineIDs: [10]),
        ],
        actionItems: [
            NotesActionItem(
                owner: "Tanaka", text: "Investigate the font rendering issue on iOS 15", due: "Mon", lineIDs: [5]),
            NotesActionItem(owner: "Suzuki", text: "Send a quote for the test devices", lineIDs: [11]),
        ],
        meta: NotesMeta(cached: true, model: "Qwen3 4B", latencyS: 8.2)
    )

    static func reviewDetail(withGap: Bool) -> ArchivedSessionDetail {
        var events = liveEvents(withPartial: false)
        if withGap {
            events = events.map { event in
                var copy = event
                if event.type == "AsrCompletedEvent", event.index == 8 { copy.translationSkipped = "catch_up" }
                return copy
            }
        } else {
            events.removeAll { $0.type == "CaptionGapEvent" }
        }
        return ArchivedSessionDetail(
            id: archived[0].id, startedAt: archived[0].startedAt, label: archived[0].label,
            eventCount: events.count, events: events, settings: settings(), performanceSummary: summary,
            notes: withGap ? nil : notes, tags: ["weekly", "engineering"], pinnedIDs: [5]
        )
    }

    /// Models without public initializers are built the way the server sends them.
    private static func decode<T: Decodable>(_ type: T.Type, _ json: Any) -> T {
        let data = try! JSONSerialization.data(withJSONObject: json)
        return try! JSONDecoder().decode(T.self, from: data)
    }

    static let vocabulary: [VocabularyWord] = decode(
        [VocabularyWord].self,
        [
            [
                "word": "定例", "reading": "ていれい", "gloss": "regular, routine", "count": 3, "line_id": 0,
                "example_ja": "定例ミーティングを始めます。", "example_en": "We'll start the regular meeting.",
            ],
            [
                "word": "特定", "reading": "とくてい", "gloss": "to identify, to pin down", "count": 2, "line_id": 5,
                "example_ja": "原因はまだ特定できていません。", "example_en": "We haven't pinned down the cause yet.",
            ],
            [
                "word": "予算", "reading": "よさん", "gloss": "budget", "count": 1, "line_id": 9,
                "example_ja": "予算の件に移りましょう。", "example_en": "Let's move on to the budget.",
            ],
        ])

    static func librarySearch(query: String = "cause") -> LibrarySearchResponse {
        func hit(_ session: ArchivedSessionSummary, tags: [String], matches: Int) -> [String: Any] {
            [
                "id": session.id, "label": session.label, "started_at": session.startedAt,
                "line_count": session.eventCount / 2, "tags": tags, "match_count": matches, "matched_in": ["en"],
                "lines": [
                    [
                        "line_id": 5, "start": 26.5, "speaker": "Tanaka",
                        "ja": "原因はまだ特定できていません", "en": "We haven't pinned down the cause yet",
                        "ja_snippet": [["t": "原因はまだ特定できていません", "hit": false]],
                        "en_snippet": [
                            ["t": "We haven't pinned down the ", "hit": false], ["t": "cause", "hit": true],
                            ["t": " yet", "hit": false],
                        ],
                    ] as [String: Any]
                ],
            ]
        }
        return decode(
            LibrarySearchResponse.self,
            [
                "query": query, "total_sessions": 3,
                "results": [
                    hit(archived[0], tags: ["weekly", "engineering"], matches: 2),
                    hit(archived[2], tags: ["design"], matches: 1),
                ],
                "speakers": ["Tanaka", "Suzuki"], "tags": ["weekly", "engineering", "design"],
            ])
    }

    static let emptyLibrary = LibrarySearchResponse(query: "", totalSessions: 0)
}
