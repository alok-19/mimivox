import AVFoundation
import Foundation
import Observation

/// Records one short clip from the microphone for the Translate tab, as mono
/// float32 at 16 kHz, the format `/api/dictate` reads.
@MainActor
@Observable
final class MicDictation {
    enum Phase: Equatable {
        case idle
        case recording
        case transcribing
    }

    private(set) var phase: Phase = .idle
    /// Input level, 0...1, while recording.
    private(set) var level: Double = 0
    var error: String?

    static let maxSeconds = 60
    private static let sampleRate: Double = 16_000

    @ObservationIgnored private var engine: AVAudioEngine?
    @ObservationIgnored private var clip = ClipBuffer()
    @ObservationIgnored private var limit: Task<Void, Never>?

    /// Starts recording; asks for the microphone the first time. `onLimit` runs
    /// if the clip reaches `maxSeconds` before the user stops it.
    func start(onLimit: @escaping @MainActor () -> Void) async {
        guard phase == .idle else { return }
        error = nil
        guard await Self.hasMicrophoneAccess() else {
            error =
                "Microphone access is off for MimiVox. Turn it on in System Settings, Privacy & Security, Microphone."
            return
        }
        do {
            try startEngine()
            phase = .recording
            limit = Task { [weak self] in
                try? await Task.sleep(for: .seconds(Self.maxSeconds))
                guard !Task.isCancelled, self?.phase == .recording else { return }
                onLimit()
            }
        } catch {
            stopEngine()
            self.error = "Could not start the microphone: \(error.localizedDescription)"
        }
    }

    /// Stops recording and returns the clip, or nil when it is too short to use.
    func finish() -> Data? {
        guard phase == .recording else { return nil }
        stopEngine()
        let samples = clip.take()
        guard samples.count >= Int(Self.sampleRate / 4) else {
            phase = .idle
            error = "That was too short. Speak, then click the mic again."
            return nil
        }
        phase = .transcribing
        return samples.withUnsafeBufferPointer { Data(buffer: $0) }
    }

    /// The clip came back from the server, with text or not.
    func done() {
        phase = .idle
    }

    func cancel() {
        stopEngine()
        _ = clip.take()
        phase = .idle
    }

    private func startEngine() throws {
        let engine = AVAudioEngine()
        let input = engine.inputNode
        let inputFormat = input.outputFormat(forBus: 0)
        guard inputFormat.sampleRate > 0,
            let target = AVAudioFormat(
                commonFormat: .pcmFormatFloat32, sampleRate: Self.sampleRate, channels: 1, interleaved: false),
            let converter = AVAudioConverter(from: inputFormat, to: target)
        else {
            throw NoInput()
        }
        let clip = ClipBuffer()
        self.clip = clip
        let ratio = Self.sampleRate / inputFormat.sampleRate
        input.installTap(onBus: 0, bufferSize: 4096, format: inputFormat) { [weak self] pcm, _ in
            let capacity = AVAudioFrameCount(Double(pcm.frameLength) * ratio) + 32
            guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
            var fed = false
            _ = converter.convert(to: out, error: nil) { _, status in
                if fed {
                    status.pointee = .noDataNow
                    return nil
                }
                fed = true
                status.pointee = .haveData
                return pcm
            }
            guard let channel = out.floatChannelData?[0], out.frameLength > 0 else { return }
            let samples = Array(UnsafeBufferPointer(start: channel, count: Int(out.frameLength)))
            clip.append(samples)
            let rms = (samples.reduce(0) { $0 + $1 * $1 } / Float(samples.count)).squareRoot()
            let level = min(1, Double(rms) * 8)
            Task { @MainActor [weak self] in
                guard let self, self.phase == .recording else { return }
                self.level = level
            }
        }
        engine.prepare()
        try engine.start()
        self.engine = engine
    }

    private func stopEngine() {
        limit?.cancel()
        limit = nil
        engine?.inputNode.removeTap(onBus: 0)
        engine?.stop()
        engine = nil
        level = 0
    }

    private static func hasMicrophoneAccess() async -> Bool {
        switch AVCaptureDevice.authorizationStatus(for: .audio) {
        case .authorized: true
        case .notDetermined: await AVCaptureDevice.requestAccess(for: .audio)
        default: false
        }
    }

    private struct NoInput: LocalizedError {
        var errorDescription: String? { "No microphone input is available." }
    }
}

/// Samples written on the audio thread and taken on the main actor.
private final class ClipBuffer: @unchecked Sendable {
    private let lock = NSLock()
    private var samples: [Float] = []

    func append(_ more: [Float]) {
        lock.lock()
        samples.append(contentsOf: more)
        lock.unlock()
    }

    func take() -> [Float] {
        lock.lock()
        defer {
            samples = []
            lock.unlock()
        }
        return samples
    }
}
