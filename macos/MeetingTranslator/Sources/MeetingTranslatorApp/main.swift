import AppKit
import MeetingTranslatorCore

if CommandLine.arguments.contains("--check-bundled-localization") {
    guard L10n.text("Home", language: .japanese) == "ホーム" else {
        print("MimiVox could not load the Japanese translation for Home.")
        exit(1)
    }
    exit(0)
}

// Dev tool: render every screen to PNGs and exit, without the helper.
if let options = SnapshotRunner.Options(arguments: CommandLine.arguments) {
    exit(SnapshotRunner.run(options))
}
if let count = LiveBenchRunner.count(arguments: CommandLine.arguments) {
    exit(LiveBenchRunner.run(count: count, arguments: CommandLine.arguments))
}
if FollowCheckRunner.requested(arguments: CommandLine.arguments) {
    exit(FollowCheckRunner.run())
}
MimiVoxApp.main()
