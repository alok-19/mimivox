import SwiftUI

/// One PNG the snapshot tool renders: a named view at a fixed size in one
/// color scheme. `preferences` seed `@AppStorage` in an isolated suite, never
/// the app's real defaults.
struct SnapshotSpec {
    let name: String
    let size: CGSize
    let colorScheme: ColorScheme
    let preferences: [String: Any]
    let makeView: @MainActor () -> AnyView

    var fileName: String { "\(name).png" }
}

/// A screen before it is expanded into light and dark specs.
struct SnapshotScreen {
    let name: String
    let size: CGSize
    var preferences: [String: Any] = [:]
    let makeView: @MainActor () -> AnyView

    func specs() -> [SnapshotSpec] {
        [(ColorScheme.light, "light"), (.dark, "dark")].map { scheme, label in
            SnapshotSpec(
                name: "\(name)-\(label)", size: size, colorScheme: scheme,
                preferences: preferences, makeView: makeView
            )
        }
    }
}
