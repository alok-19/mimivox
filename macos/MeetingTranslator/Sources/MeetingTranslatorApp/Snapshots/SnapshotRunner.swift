import AppKit
import MeetingTranslatorCore
import SwiftUI

/// `MeetingTranslator --snapshot <outdir> [--only <substring>]`: renders every
/// `SnapshotCatalog` spec to `<outdir>/<name>.png` and exits. It never starts
/// the helper, opens the network, reads audio or captures the screen: each view
/// is drawn straight from its own backing store.
@MainActor
enum SnapshotRunner {
    struct Options {
        var outputDirectory: URL
        var only: String?

        init?(arguments: [String]) {
            guard let flag = arguments.firstIndex(of: "--snapshot"), arguments.indices.contains(flag + 1) else {
                return nil
            }
            outputDirectory = URL(fileURLWithPath: arguments[flag + 1], isDirectory: true)
            only = arguments.firstIndex(of: "--only").flatMap {
                arguments.indices.contains($0 + 1) ? arguments[$0 + 1] : nil
            }
        }
    }

    private static let suiteName = "com.mimivox.ui-snapshots"

    static func run(_ options: Options) -> Int32 {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        NSTimeZone.default = TimeZone(identifier: "UTC") ?? .gmt
        MeetingFormat.currentDate = { SnapshotFixtures.referenceDate }
        try? FileManager.default.createDirectory(at: options.outputDirectory, withIntermediateDirectories: true)
        let suite = UserDefaults(suiteName: suiteName) ?? .standard
        defer { suite.removePersistentDomain(forName: suiteName) }

        let specs = SnapshotCatalog.all.filter { options.only.map($0.name.contains) ?? true }
        var failures = 0
        for spec in specs {
            suite.removePersistentDomain(forName: suiteName)
            suite.set(AppLanguage.selected.rawValue, forKey: AppLanguage.defaultsKey)
            for (key, value) in spec.preferences { suite.set(value, forKey: key) }
            guard let png = render(spec, defaults: suite) else {
                print("FAILED  \(spec.fileName)")
                failures += 1
                continue
            }
            do {
                try png.write(to: options.outputDirectory.appendingPathComponent(spec.fileName))
                print("wrote   \(spec.fileName)")
            } catch {
                print("FAILED  \(spec.fileName): \(error.localizedDescription)")
                failures += 1
            }
        }
        print("\(specs.count - failures) of \(specs.count) snapshots in \(options.outputDirectory.path)")
        return failures == 0 ? 0 : 1
    }

    private static func render(_ spec: SnapshotSpec, defaults: UserDefaults) -> Data? {
        let rect = NSRect(origin: .zero, size: spec.size)
        let root = spec.makeView()
            .defaultAppStorage(defaults)
            .environment(\.colorScheme, spec.colorScheme)
            .frame(width: spec.size.width, height: spec.size.height)
        let hosting = NSHostingView(rootView: root)
        hosting.frame = rect
        // An unshown window gives the view its appearance and a backing store.
        let window = NSWindow(contentRect: rect, styleMask: [.borderless], backing: .buffered, defer: false)
        window.appearance = NSAppearance(named: spec.colorScheme == .dark ? .darkAqua : .aqua)
        window.contentView = hosting
        hosting.layoutSubtreeIfNeeded()
        // Let onAppear/task work and scroll-position state settle, then redraw.
        RunLoop.main.run(until: Date().addingTimeInterval(0.4))
        hosting.layoutSubtreeIfNeeded()
        hosting.displayIfNeeded()
        guard let rep = hosting.bitmapImageRepForCachingDisplay(in: hosting.bounds) else { return nil }
        hosting.cacheDisplay(in: hosting.bounds, to: rep)
        return rep.representation(using: .png, properties: [:])
    }
}
