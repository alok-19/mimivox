import AVFoundation
import AudioToolbox
import CoreAudio
import Foundation

public enum SystemAudioCaptureError: Error, LocalizedError, Equatable {
    case coreAudio(String, OSStatus)
    case unsupportedFormat
    case noMatchingProcesses

    public var errorDescription: String? {
        switch self {
        case .coreAudio(let step, let status):
            return "System audio capture failed to \(step) (OSStatus \(status))"
        case .unsupportedFormat:
            return "System audio tap delivered an unsupported format"
        case .noMatchingProcesses:
            return "None of the apps chosen for capture are playing audio"
        }
    }
}

/// Captures everything the Mac plays through a Core Audio process tap
/// (macOS 14.2+) and delivers it as mono float32 little-endian PCM at
/// `sampleRate` — the format `/api/session/audio` expects.
///
/// No virtual loopback driver (BlackHole/Loopback) is involved. The first
/// start triggers the "System Audio Recording" privacy prompt; until it is
/// granted the tap delivers silence.
@available(macOS 14.2, *)
public final class SystemAudioCapture: @unchecked Sendable {
    public static let defaultSampleRate: Double = 16_000

    private let sampleRate: Double
    private let scope: CaptureScope
    private let queue = DispatchQueue(label: "mimivox.system-audio-tap", qos: .userInitiated)
    private let lock = NSLock()
    private var tapID = AudioObjectID(kAudioObjectUnknown)
    private var aggregateID = AudioObjectID(kAudioObjectUnknown)
    private var ioProcID: AudioDeviceIOProcID?

    public init(sampleRate: Double = defaultSampleRate, scope: CaptureScope = .everything(excluding: [])) {
        self.sampleRate = sampleRate
        self.scope = scope
    }

    deinit {
        stop()
    }

    public var isRunning: Bool {
        lock.lock()
        defer { lock.unlock() }
        return ioProcID != nil
    }

    /// Start capturing. `onSamples` runs on a private serial queue, in order.
    public func start(onSamples: @escaping @Sendable (Data) -> Void) throws {
        lock.lock()
        defer { lock.unlock() }
        guard ioProcID == nil else { return }
        do {
            try startLocked(onSamples: onSamples)
        } catch {
            stopLocked()
            throw error
        }
    }

    public func stop() {
        lock.lock()
        defer { lock.unlock() }
        stopLocked()
    }

    private func startLocked(onSamples: @escaping @Sendable (Data) -> Void) throws {
        // Everything except our own UI sounds and the user's "never capture"
        // apps, or only the chosen apps.
        let description = try TapDescriptionBuilder.make(
            scope: scope,
            processes: CoreAudioProcesses.snapshot(),
            ownObjectID: Self.currentProcessObjectID()
        )
        description.uuid = UUID()
        description.name = "MimiVox System Audio"
        description.isPrivate = true
        description.muteBehavior = .unmuted
        try check(AudioHardwareCreateProcessTap(description, &tapID), "create process tap")

        var tapFormat = AudioStreamBasicDescription()
        var formatSize = UInt32(MemoryLayout<AudioStreamBasicDescription>.size)
        var formatAddress = Self.address(kAudioTapPropertyFormat)
        try check(
            AudioObjectGetPropertyData(tapID, &formatAddress, 0, nil, &formatSize, &tapFormat),
            "read tap format"
        )

        // A tap is read through a private aggregate device clocked by the
        // current output device.
        let outputUID = try Self.defaultOutputDeviceUID()
        let aggregate: [String: Any] = [
            kAudioAggregateDeviceNameKey: "MimiVox System Audio",
            kAudioAggregateDeviceUIDKey: UUID().uuidString,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [[kAudioSubDeviceUIDKey: outputUID]],
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapDriftCompensationKey: true,
                    kAudioSubTapUIDKey: description.uuid.uuidString,
                ]
            ],
        ]
        try check(
            AudioHardwareCreateAggregateDevice(aggregate as CFDictionary, &aggregateID),
            "create aggregate device"
        )

        guard let converter = TapConverter(input: tapFormat, sampleRate: sampleRate) else {
            throw SystemAudioCaptureError.unsupportedFormat
        }
        var procID: AudioDeviceIOProcID?
        try check(
            AudioDeviceCreateIOProcIDWithBlock(&procID, aggregateID, queue) { _, inputData, _, _, _ in
                if let data = converter.convert(inputData) {
                    onSamples(data)
                }
            },
            "create IO proc"
        )
        ioProcID = procID
        try check(AudioDeviceStart(aggregateID, procID), "start aggregate device")
    }

    private func stopLocked() {
        if aggregateID != kAudioObjectUnknown {
            if let ioProcID {
                AudioDeviceStop(aggregateID, ioProcID)
                AudioDeviceDestroyIOProcID(aggregateID, ioProcID)
            }
            AudioHardwareDestroyAggregateDevice(aggregateID)
        }
        if tapID != kAudioObjectUnknown {
            AudioHardwareDestroyProcessTap(tapID)
        }
        ioProcID = nil
        aggregateID = AudioObjectID(kAudioObjectUnknown)
        tapID = AudioObjectID(kAudioObjectUnknown)
    }

    private func check(_ status: OSStatus, _ step: String) throws {
        guard status == noErr else {
            throw SystemAudioCaptureError.coreAudio(step, status)
        }
    }

    private static func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
    }

    private static func currentProcessObjectID() -> AudioObjectID? {
        var pid = ProcessInfo.processInfo.processIdentifier
        var objectID = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var address = address(kAudioHardwarePropertyTranslatePIDToProcessObject)
        let status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address,
            UInt32(MemoryLayout<pid_t>.size), &pid, &size, &objectID
        )
        guard status == noErr, objectID != kAudioObjectUnknown else { return nil }
        return objectID
    }

    private static func defaultOutputDeviceUID() throws -> String {
        var deviceID = AudioObjectID(kAudioObjectUnknown)
        var size = UInt32(MemoryLayout<AudioObjectID>.size)
        var address = address(kAudioHardwarePropertyDefaultOutputDevice)
        var status = AudioObjectGetPropertyData(
            AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &deviceID
        )
        guard status == noErr else {
            throw SystemAudioCaptureError.coreAudio("find default output device", status)
        }
        var uid: Unmanaged<CFString>?
        size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        address = Self.address(kAudioDevicePropertyDeviceUID)
        status = AudioObjectGetPropertyData(deviceID, &address, 0, nil, &size, &uid)
        guard status == noErr, let uid else {
            throw SystemAudioCaptureError.coreAudio("read output device UID", status)
        }
        return uid.takeRetainedValue() as String
    }
}

/// Converts tap buffers (usually 48 kHz stereo float) to mono float32 at the
/// target rate. Only ever called from the capture's serial IO queue.
private final class TapConverter: @unchecked Sendable {
    private let inputFormat: AVAudioFormat
    private let outputFormat: AVAudioFormat
    private let converter: AVAudioConverter

    init?(input: AudioStreamBasicDescription, sampleRate: Double) {
        var input = input
        guard let inputFormat = AVAudioFormat(streamDescription: &input),
            let outputFormat = AVAudioFormat(
                commonFormat: .pcmFormatFloat32,
                sampleRate: sampleRate,
                channels: 1,
                interleaved: false
            ),
            let converter = AVAudioConverter(from: inputFormat, to: outputFormat)
        else {
            return nil
        }
        converter.downmix = true
        self.inputFormat = inputFormat
        self.outputFormat = outputFormat
        self.converter = converter
    }

    func convert(_ bufferList: UnsafePointer<AudioBufferList>) -> Data? {
        guard
            let input = AVAudioPCMBuffer(
                pcmFormat: inputFormat, bufferListNoCopy: bufferList, deallocator: nil
            ), input.frameLength > 0
        else {
            return nil
        }
        let ratio = outputFormat.sampleRate / inputFormat.sampleRate
        let capacity = AVAudioFrameCount((Double(input.frameLength) * ratio).rounded(.up)) + 64
        guard let output = AVAudioPCMBuffer(pcmFormat: outputFormat, frameCapacity: capacity) else {
            return nil
        }
        // The converter keeps resampler state across calls; hand it this
        // buffer exactly once, then report "no data now" (not end of stream).
        let pending = PendingInput(input)
        var error: NSError?
        let status = converter.convert(to: output, error: &error) { _, inputStatus in
            guard let buffer = pending.take() else {
                inputStatus.pointee = .noDataNow
                return nil
            }
            inputStatus.pointee = .haveData
            return buffer
        }
        guard status != .error, output.frameLength > 0, let channel = output.floatChannelData else {
            return nil
        }
        return Data(bytes: channel[0], count: Int(output.frameLength) * MemoryLayout<Float>.size)
    }
}

/// AVAudioConverter calls its input block synchronously inside convert(),
/// so handing the buffer across the @Sendable boundary is safe.
private final class PendingInput: @unchecked Sendable {
    private var buffer: AVAudioPCMBuffer?

    init(_ buffer: AVAudioPCMBuffer) {
        self.buffer = buffer
    }

    func take() -> AVAudioPCMBuffer? {
        defer { buffer = nil }
        return buffer
    }
}

/// Batches captured PCM into ~100 ms uploads and sends them strictly in
/// order, one request at a time. Stops when `send` reports the session is
/// gone (returns false) or after sustained failures.
public final class SystemAudioStreamer: Sendable {
    private let continuation: AsyncStream<Data>.Continuation
    private let task: Task<Void, Never>

    /// - Parameters:
    ///   - minChunkBytes: bytes per upload (default 100 ms of 16 kHz float32).
    ///   - maxBufferedChunks: capture callbacks kept while an upload is slow;
    ///     the oldest are dropped beyond this so latency cannot grow unbounded.
    ///   - send: uploads one chunk; return false to stop streaming.
    ///   - onStopped: called once if streaming stops on its own.
    public init(
        minChunkBytes: Int = 1_600 * MemoryLayout<Float>.size,
        maxBufferedChunks: Int = 500,
        maxConsecutiveFailures: Int = 50,
        send: @escaping @Sendable (Data) async throws -> Bool,
        onStopped: @escaping @Sendable () -> Void = {}
    ) {
        let (stream, continuation) = AsyncStream<Data>.makeStream(
            bufferingPolicy: .bufferingNewest(maxBufferedChunks)
        )
        self.continuation = continuation
        self.task = Task {
            var pending = Data()
            var failures = 0

            func upload(_ body: Data) async -> Bool {
                do {
                    guard try await send(body) else { return false }
                    failures = 0
                } catch {
                    failures += 1
                    if failures >= maxConsecutiveFailures { return false }
                }
                return true
            }

            for await chunk in stream {
                pending.append(chunk)
                guard pending.count >= minChunkBytes else { continue }
                let body = pending
                pending = Data()
                if await !upload(body) {
                    onStopped()
                    return
                }
            }
            if !pending.isEmpty {
                _ = await upload(pending)
            }
        }
    }

    public func append(_ chunk: Data) {
        continuation.yield(chunk)
    }

    /// Flush what is buffered and end the stream.
    public func finish() {
        continuation.finish()
    }

    public func cancel() {
        continuation.finish()
        task.cancel()
    }

    /// Wait for the final flush after `finish()`.
    public func waitUntilFinished() async {
        await task.value
    }
}
