import AppKit
import Combine
import MeetingTranslatorCore
import SwiftUI

@MainActor
enum LiveBenchRunner {
    static func count(arguments: [String]) -> Int? {
        guard let flag = arguments.firstIndex(of: "--bench-live"), arguments.indices.contains(flag + 1) else {
            return nil
        }
        return Int(arguments[flag + 1])
    }

    private static func only(arguments: [String]) -> String? {
        arguments.firstIndex(of: "--only").flatMap { arguments.indices.contains($0 + 1) ? arguments[$0 + 1] : nil }
    }

    static func run(count: Int, arguments: [String]) -> Int32 {
        let only = only(arguments: arguments)
        NSApplication.shared.setActivationPolicy(.prohibited)
        let events = SyntheticMeeting.events(count: count)
        let store = TranslatorStore.snapshotStore(
            SnapshotState(
                status: SnapshotFixtures.status("listening", health: SnapshotFixtures.health(label: "Translating")),
                helperPhase: .ready(port: 1),
                liveEvents: [events[0]]
            ))
        var hosts: [NSView] = []
        if only != "hud" {
            hosts.append(
                host(
                    RootView(
                        selection: .constant(.live), quickTranslate: QuickTranslateDraft(),
                        meetingsSearch: MeetingsSearch()
                    ).environmentObject(store), size: CGSize(width: 1280, height: 800)))
        }
        if only != "live" {
            hosts.append(host(makeCaptionHUDSnapshotView(store: store), size: CGSize(width: 820, height: 320)))
        }
        hosts.reserveCapacity(2)
        var storeChanges = 0
        let subscription = store.objectWillChange.sink { storeChanges += 1 }
        defer { subscription.cancel() }

        var durations: [Double] = []
        var stageDurations = [[Double]](repeating: [], count: hosts.count + 2)
        durations.reserveCapacity(events.count)
        func ms(since start: UInt64) -> Double { Double(DispatchTime.now().uptimeNanoseconds - start) / 1_000_000 }
        for event in events {
            let start = DispatchTime.now().uptimeNanoseconds
            store.ingest(event)
            stageDurations[0].append(ms(since: start))
            let runloopStart = DispatchTime.now().uptimeNanoseconds
            CFRunLoopRunInMode(.defaultMode, 0, false)
            stageDurations[1].append(ms(since: runloopStart))
            for (slot, host) in hosts.enumerated() {
                let hostStart = DispatchTime.now().uptimeNanoseconds
                host.layoutSubtreeIfNeeded()
                host.displayIfNeeded()
                stageDurations[slot + 2].append(ms(since: hostStart))
            }
            durations.append(ms(since: start))
        }

        func mean(_ values: [Double]) -> Double { values.reduce(0, +) / Double(max(1, values.count)) }
        func lastQuarterMean(_ values: [Double]) -> Double { mean(Array(values.suffix(values.count / 4))) }
        let sorted = durations.sorted()
        func percentile(_ p: Double) -> Double { sorted[min(sorted.count - 1, Int(Double(sorted.count) * p))] }
        let total = durations.reduce(0, +)
        print(
            String(
                format:
                    "events=%d rows=%d total_ms=%.0f mean_ms=%.3f p50_ms=%.3f p95_ms=%.3f max_ms=%.1f last_quarter_mean_ms=%.3f store_changes=%d store_changes_per_event=%.2f",
                events.count, store.bilingualTranscriptRows.count, total, total / Double(events.count), percentile(0.5),
                percentile(0.95), sorted.last ?? 0,
                lastQuarterMean(durations), storeChanges, Double(storeChanges) / Double(events.count)
            ))
        print(
            String(
                format:
                    "  ingest mean_ms=%.3f last_quarter_ms=%.3f | runloop mean_ms=%.3f last_quarter_ms=%.3f | live layout mean_ms=%.3f | hud layout mean_ms=%.3f",
                mean(stageDurations[0]), lastQuarterMean(stageDurations[0]),
                mean(stageDurations[1]), lastQuarterMean(stageDurations[1]),
                mean(stageDurations[2]), mean(stageDurations[hosts.count == 2 ? 3 : 2])
            ))
        return 0
    }

    private static func host<V: View>(_ view: V, size: CGSize) -> NSView {
        let rect = NSRect(origin: .zero, size: size)
        let hosting = NSHostingView(rootView: view.frame(width: size.width, height: size.height))
        hosting.frame = rect
        let window = NSWindow(contentRect: rect, styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = hosting
        hosting.layoutSubtreeIfNeeded()
        return hosting
    }
}

enum SyntheticMeeting {
    static func events(count: Int) -> [MeetingEvent] {
        let speakers = [SnapshotFixtures.tanaka, SnapshotFixtures.suzuki]
        var events = [MeetingEvent(type: "SessionStartedEvent", message: "Session started")]
        var line = 0
        var t = 1.0
        while events.count < count {
            let speaker = speakers[line % 2]
            let ja = "これは\(line)番目の文で、会議の内容を話しています。"
            let en = "This is sentence \(line), talking about the meeting."
            events += [
                MeetingEvent(type: "AudioLevelEvent", level: 0.3, timestampSeconds: t),
                MeetingEvent(type: "AsrPartialEvent", index: line, start: t, text: "これは\(line)番目", speaker: speaker),
                MeetingEvent(type: "AsrPartialEvent", index: line, start: t, text: "これは\(line)番目の文で", speaker: speaker),
                MeetingEvent(type: "AsrCompletedEvent", index: line, start: t, end: t + 3, text: ja, speaker: speaker),
                MeetingEvent(type: "TranslationStartedEvent", index: line, start: t, text: ja),
                MeetingEvent(type: "AudioLevelEvent", level: 0.5, timestampSeconds: t + 2),
                MeetingEvent(
                    type: "TranslationCompletedEvent", index: line, start: t, end: t + 3, text: en,
                    sourceIndexes: [line], speaker: speaker),
            ]
            if line % 5 == 4 {
                events.append(MeetingEvent(type: "LatencyHealthEvent", health: LatencyHealth()))
            }
            if line % 20 == 19 {
                events.append(
                    MeetingEvent(
                        type: "TranscriptRevisionEvent", index: line - 1, start: t - 4, end: t + 3,
                        text: "Sentences \(line - 1) and \(line), merged.", japanese: "\(line - 1)と\(line)の文。",
                        mergedIndexes: [line]
                    ))
            }
            if line % 50 == 49 {
                events.append(
                    MeetingEvent(
                        type: "CaptionGapEvent", index: 10_000 + line, start: t + 3.5, end: t + 4, reason: "catch_up"))
            }
            line += 1
            t += 4.5
        }
        return Array(events.prefix(count))
    }
}
