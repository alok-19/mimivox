import AppKit
import SwiftUI

struct Notice: Equatable, Identifiable {
    enum Kind {
        case info, success, warning, error
    }

    enum Recovery: Equatable {
        case retryServer
        case openPrivacySettings(PrivacyPane)
    }

    let id = UUID()
    var kind: Kind
    var text: String
    var path: String?
    var recovery: Recovery?
    /// The command that failed, so its next success can clear the error.
    var command: String?

    /// Whether Settings should show this notice. Meeting and Live errors belong to the main window.
    var concernsSettings: Bool {
        if recovery != nil { return true }
        guard let command else { return false }
        return ["Save settings", "Download notes model", "Reset model cache", "Delete all history"].contains(command)
            || command.hasPrefix("Model ")
    }

    static func info(_ text: String) -> Notice {
        Notice(kind: .info, text: text)
    }

    static func info(_ text: String?) -> Notice? {
        text.flatMap { $0.isEmpty ? nil : info($0) }
    }

    static func success(_ text: String, path: String? = nil) -> Notice {
        Notice(kind: .success, text: text, path: path)
    }

    static func warning(_ text: String, recovery: Recovery? = nil) -> Notice {
        Notice(kind: .warning, text: text, recovery: recovery)
    }

    static func error(_ text: String, recovery: Recovery? = nil, command: String? = nil) -> Notice {
        Notice(kind: .error, text: text, recovery: recovery, command: command)
    }

    var lifetime: Duration? {
        switch kind {
        case .info: .seconds(4)
        case .success: path == nil ? .seconds(4) : .seconds(12)
        case .warning, .error: nil
        }
    }
}

/// A System Settings privacy page the viewer can be sent to.
enum PrivacyPane: Hashable {
    case systemAudio
    case microphone

    var url: URL {
        switch self {
        case .systemAudio: URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_ScreenCapture")!
        case .microphone: URL(string: "x-apple.systempreferences:com.apple.preference.security?Privacy_Microphone")!
        }
    }
}
