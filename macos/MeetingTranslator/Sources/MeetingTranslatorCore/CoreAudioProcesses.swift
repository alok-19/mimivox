import AudioToolbox
import CoreAudio
import Foundation

/// Read-only enumeration of Core Audio process objects (macOS 14.2+).
/// Reads bundle IDs and running flags only, never audio.
@available(macOS 14.2, *)
public enum CoreAudioProcesses {
    public static func snapshot() -> [AudioProcessInfo] {
        var listAddress = address(kAudioHardwarePropertyProcessObjectList)
        let system = AudioObjectID(kAudioObjectSystemObject)
        var size: UInt32 = 0
        guard AudioObjectGetPropertyDataSize(system, &listAddress, 0, nil, &size) == noErr, size > 0 else {
            return []
        }
        var ids = [AudioObjectID](repeating: 0, count: Int(size) / MemoryLayout<AudioObjectID>.size)
        guard AudioObjectGetPropertyData(system, &listAddress, 0, nil, &size, &ids) == noErr else {
            return []
        }
        return ids.compactMap { id in
            guard let bundleID = string(id, kAudioProcessPropertyBundleID), !bundleID.isEmpty else { return nil }
            return AudioProcessInfo(
                objectID: id,
                pid: Int32(truncatingIfNeeded: uint32(id, kAudioProcessPropertyPID) ?? 0),
                bundleID: bundleID,
                isRunningOutput: uint32(id, kAudioProcessPropertyIsRunningOutput) == 1,
                isRunningInput: uint32(id, kAudioProcessPropertyIsRunningInput) == 1
            )
        }
    }

    private static func address(_ selector: AudioObjectPropertySelector) -> AudioObjectPropertyAddress {
        AudioObjectPropertyAddress(
            mSelector: selector,
            mScope: kAudioObjectPropertyScopeGlobal,
            mElement: kAudioObjectPropertyElementMain
        )
    }

    private static func uint32(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
        var address = address(selector)
        var value: UInt32 = 0
        var size = UInt32(MemoryLayout<UInt32>.size)
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value) == noErr else { return nil }
        return value
    }

    private static func string(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
        var address = address(selector)
        var value: Unmanaged<CFString>?
        var size = UInt32(MemoryLayout<Unmanaged<CFString>?>.size)
        guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value) == noErr else { return nil }
        return value?.takeRetainedValue() as String?
    }
}

@available(macOS 14.2, *)
public enum TapDescriptionBuilder {
    /// Builds the tap description for `scope` against the current processes.
    /// Pure (touches no hardware), so it can be inspected in tests.
    /// - Throws: `.noMatchingProcesses` when an include-only scope matches nothing running.
    public static func make(
        scope: CaptureScope,
        processes: [AudioProcessInfo],
        ownObjectID: UInt32?
    ) throws -> CATapDescription {
        func ids(_ keys: [String]) -> [AudioObjectID] {
            processes
                .filter { process in keys.contains { CaptureKey.matches($0, bundleID: process.bundleID) } }
                .map(\.objectID)
        }
        switch scope {
        case .everything(let excluding):
            let excluded = ids(excluding) + (ownObjectID.map { [$0] } ?? [])
            return CATapDescription(stereoGlobalTapButExcludeProcesses: excluded)
        case .only(let keys):
            let included = ids(keys)
            guard !included.isEmpty else { throw SystemAudioCaptureError.noMatchingProcesses }
            return CATapDescription(stereoMixdownOfProcesses: included)
        }
    }
}
