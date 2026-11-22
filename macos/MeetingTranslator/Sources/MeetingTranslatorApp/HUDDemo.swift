import Foundation

/// Debug hooks for screenshotting the HUD with real captions, with no clicks
/// and no live audio. Documented in docs/hud-readout.md.
///
/// - `MIMIVOX_DEMO_REPLAY=<wav>`: on launch, start a replay-file session of
///   that WAV (never the microphone or system audio) and open the HUD.
/// - `MIMIVOX_DEMO_DOWNLOAD=1`: press "Download models" once models are reported missing.
/// - `MIMIVOX_DEMO_HUD=pin-first,reveal-first`: pin the first line and show
///   its other language, so those states can be captured.
/// - Display mode and size come from launch arguments such as
///   `-hud.displayMode englishOnly`.
/// Read-aloud is never switched on by these hooks, so a demo makes no sound.
enum HUDDemo {
    private static let environment = ProcessInfo.processInfo.environment

    static let replayWAV: String? = environment["MIMIVOX_DEMO_REPLAY"].flatMap { $0.isEmpty ? nil : $0 }
    static let downloadsModels = environment["MIMIVOX_DEMO_DOWNLOAD"] == "1"

    private static let flags = Set((environment["MIMIVOX_DEMO_HUD"] ?? "").split(separator: ",").map(String.init))

    static let pinsFirstLine = flags.contains("pin-first")
    static let revealsFirstLine = flags.contains("reveal-first")
}
