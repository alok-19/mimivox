import AVFoundation
import CoreAudio
import Foundation

/// One finalized English caption to speak. `id` is stable across revisions.
public struct ReadAloudLine: Equatable, Sendable, Identifiable {
    public var id: String
    public var text: String

    public init(id: String, text: String) {
        self.id = id
        self.text = text
    }
}

/// Which lines still need speaking. Pure state so the rules are testable
/// without a speech engine.
///
/// - Only lines passed to `sync` are spoken; callers pass finalized English only.
/// - A line is spoken at most once: `spoken` records every line that started
///   speaking or was dropped, so a later revision of it is never re-spoken.
/// - A revision of a line that has not started yet replaces its pending text.
/// - At most `maxPending` lines wait behind the one speaking; older ones are
///   dropped (counted in `droppedCount`) so speech never falls far behind.
/// - While paused nothing starts; lines keep queuing under the same cap.
public struct ReadAloudQueue: Equatable, Sendable {
    public static let maxPending = 2

    public private(set) var pending: [ReadAloudLine] = []
    public private(set) var current: ReadAloudLine?
    public private(set) var spoken: Set<String> = []
    public private(set) var droppedCount = 0
    public private(set) var isPaused = false

    public init() {}

    /// Marks lines that already exist (read-aloud was switched on mid-session)
    /// as spoken so only new captions are read.
    public mutating func prime(with lines: [ReadAloudLine]) {
        pending = []
        spoken.formUnion(lines.map(\.id))
    }

    /// Reconciles the queue with the session's current finalized lines, oldest first.
    public mutating func sync(_ lines: [ReadAloudLine]) {
        pending = lines.filter { line in
            !line.text.isEmpty && !spoken.contains(line.id) && line.id != current?.id
        }
        while pending.count > Self.maxPending {
            spoken.insert(pending.removeFirst().id)
            droppedCount += 1
        }
    }

    /// The next line to speak, or nil while paused, busy or empty.
    public mutating func next() -> ReadAloudLine? {
        guard !isPaused, current == nil, !pending.isEmpty else { return nil }
        let line = pending.removeFirst()
        current = line
        spoken.insert(line.id)
        return line
    }

    public mutating func finishCurrent() {
        current = nil
    }

    public mutating func setPaused(_ paused: Bool) {
        isPaused = paused
    }

    /// Forgets everything (new session, or read-aloud switched off).
    public mutating func reset() {
        self = ReadAloudQueue()
    }
}

/// Speaks text. The seam between the queue logic and AVSpeechSynthesizer.
@MainActor
public protocol SpeechOutput: AnyObject {
    func speak(_ text: String, voiceID: String?, rate: Float, onFinish: @escaping @MainActor () -> Void)
    func pause()
    func resume()
    func stop()
}

/// Feeds finalized captions through a `ReadAloudQueue` into a `SpeechOutput`.
@MainActor
public final class ReadAloudCoordinator: ObservableObject {
    @Published public private(set) var queue = ReadAloudQueue()
    @Published public private(set) var isEnabled = false
    public var voiceID: String?
    public var rate: Float = AVSpeechUtteranceDefaultSpeechRate

    private let output: SpeechOutput
    /// Invalidates the finish callback of an utterance that was stopped.
    private var generation = 0

    public init(output: SpeechOutput) {
        self.output = output
    }

    public var droppedCount: Int { queue.droppedCount }

    public func setEnabled(_ enabled: Bool, currentLines: [ReadAloudLine], paused: Bool) {
        guard enabled != isEnabled else { return }
        isEnabled = enabled
        stopSpeaking()
        queue.reset()
        guard enabled else { return }
        queue.prime(with: currentLines)
        queue.setPaused(paused)
    }

    /// Call whenever the finalized English lines change.
    public func update(lines: [ReadAloudLine]) {
        guard isEnabled else { return }
        queue.sync(lines)
        pump()
    }

    public func setPaused(_ paused: Bool) {
        guard isEnabled, paused != queue.isPaused else { return }
        queue.setPaused(paused)
        if paused {
            output.pause()
        } else {
            output.resume()
            pump()
        }
    }

    /// New session: drop the queue and any speech in flight.
    public func reset(currentLines: [ReadAloudLine] = []) {
        stopSpeaking()
        queue.reset()
        guard isEnabled else { return }
        queue.prime(with: currentLines)
    }

    private func stopSpeaking() {
        generation += 1
        output.stop()
    }

    private func pump() {
        guard let line = queue.next() else { return }
        let token = generation
        output.speak(line.text, voiceID: voiceID, rate: rate) { [weak self] in
            guard let self, token == self.generation else { return }
            self.queue.finishCurrent()
            self.pump()
        }
    }
}

// MARK: - AVSpeechSynthesizer

/// Speaks through the system default output device.
@MainActor
public final class AVSpeechOutput: NSObject, SpeechOutput, @preconcurrency AVSpeechSynthesizerDelegate {
    private let synthesizer = AVSpeechSynthesizer()
    private var onFinish: (@MainActor () -> Void)?

    public override init() {
        super.init()
        synthesizer.delegate = self
    }

    public func speak(_ text: String, voiceID: String?, rate: Float, onFinish: @escaping @MainActor () -> Void) {
        self.onFinish = onFinish
        synthesizer.speak(ReadAloudVoices.utterance(text, voiceID: voiceID, rate: rate))
    }

    public func pause() {
        synthesizer.pauseSpeaking(at: .word)
    }

    public func resume() {
        synthesizer.continueSpeaking()
    }

    public func stop() {
        onFinish = nil
        synthesizer.stopSpeaking(at: .immediate)
    }

    public func speechSynthesizer(_ synthesizer: AVSpeechSynthesizer, didFinish utterance: AVSpeechUtterance) {
        let done = onFinish
        onFinish = nil
        done?()
    }
}

public struct ReadAloudVoice: Equatable, Identifiable, Sendable {
    public var id: String
    public var name: String
    public var language: String
    public var qualityLabel: String

    public var displayName: String {
        qualityLabel.isEmpty ? "\(name) (\(language))" : "\(name) (\(language), \(qualityLabel))"
    }
}

public enum ReadAloudVoices {
    /// Installed English voices, best quality first.
    public static func english() -> [ReadAloudVoice] {
        AVSpeechSynthesisVoice.speechVoices()
            .filter { $0.language.hasPrefix("en") }
            .sorted { lhs, rhs in
                if lhs.quality != rhs.quality { return lhs.quality.rawValue > rhs.quality.rawValue }
                return lhs.name < rhs.name
            }
            .map { voice in
                ReadAloudVoice(
                    id: voice.identifier,
                    name: voice.name,
                    language: voice.language,
                    qualityLabel: voice.quality == .premium ? "premium" : voice.quality == .enhanced ? "enhanced" : ""
                )
            }
    }

    public static func utterance(_ text: String, voiceID: String?, rate: Float) -> AVSpeechUtterance {
        let utterance = AVSpeechUtterance(string: text)
        utterance.voice =
            voiceID.flatMap { AVSpeechSynthesisVoice(identifier: $0) }
            ?? AVSpeechSynthesisVoice(language: "en-US")
        utterance.rate = min(max(rate, AVSpeechUtteranceMinimumSpeechRate), AVSpeechUtteranceMaximumSpeechRate)
        return utterance
    }
}

/// Renders speech to memory with `AVSpeechSynthesizer.write` instead of the
/// speakers, so tests can check voice and rate without making any sound.
public enum SpeechRenderer {
    public struct Result: Equatable, Sendable {
        public var frames: Int
        public var sampleRate: Double
        public var seconds: Double { sampleRate > 0 ? Double(frames) / sampleRate : 0 }
    }

    private final class Accumulator: @unchecked Sendable {
        private let lock = NSLock()
        private var frames = 0
        private var sampleRate = 0.0
        private var finished = false

        func add(_ buffer: AVAudioPCMBuffer) -> Bool {
            lock.lock()
            defer { lock.unlock() }
            if buffer.frameLength == 0 {
                finished = true
            } else {
                frames += Int(buffer.frameLength)
                sampleRate = buffer.format.sampleRate
            }
            return finished
        }

        var result: Result {
            lock.lock()
            defer { lock.unlock() }
            return Result(frames: frames, sampleRate: sampleRate)
        }
    }

    /// Nil when the synthesizer produced nothing before `timeout`.
    public static func render(_ text: String, voiceID: String? = nil, rate: Float, timeout: TimeInterval = 20) async
        -> Result?
    {
        let accumulator = Accumulator()
        let done = AsyncStream<Void>.makeStream()
        let synthesizer = AVSpeechSynthesizer()
        let utterance = ReadAloudVoices.utterance(text, voiceID: voiceID, rate: rate)
        synthesizer.write(utterance) { buffer in
            guard let pcm = buffer as? AVAudioPCMBuffer else { return }
            if accumulator.add(pcm) {
                done.continuation.yield()
                done.continuation.finish()
            }
        }
        let waiter = Task {
            for await _ in done.stream { return }
        }
        let timer = Task {
            try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
            done.continuation.finish()
        }
        await waiter.value
        timer.cancel()
        withExtendedLifetime(synthesizer) {}
        let result = accumulator.result
        return result.frames > 0 ? result : nil
    }
}

// MARK: - Output device note

public enum ReadAloudOutputNote {
    /// Name of the system default output device, e.g. "MacBook Pro Speakers".
    public static func defaultOutputDeviceName() -> String? {
        var deviceID = AudioDeviceID(0)
        var size = UInt32(MemoryLayout<AudioDeviceID>.size)
        var address = AudioObjectPropertyAddress(
            mSelector: kAudioHardwarePropertyDefaultOutputDevice,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        guard
            AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &deviceID)
                == noErr,
            deviceID != 0
        else { return nil }

        var nameRef: Unmanaged<CFString>?
        var nameSize = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        var nameAddress = AudioObjectPropertyAddress(
            mSelector: kAudioObjectPropertyName,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
        guard AudioObjectGetPropertyData(deviceID, &nameAddress, 0, nil, &nameSize, &nameRef) == noErr,
            let nameRef
        else { return nil }
        return nameRef.takeRetainedValue() as String
    }

    /// Shown next to the read-aloud controls.
    public static func text(deviceName: String?) -> String {
        let device =
            deviceName.map {
                String(format: L10n.text("It plays through the default output (%@)."), $0)
            } ?? L10n.text("It plays through the default output.")
        return String(
            format: L10n.text(
                "%@ If that is a speaker, your meeting microphone can pick it up and play it to the other people. Use headphones."
            ),
            device
        )
    }
}
