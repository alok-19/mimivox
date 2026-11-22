import MeetingTranslatorCore
import SwiftUI

enum Palette {
    static let live = Color(nsColor: .systemRed)
    static let warning = Color(nsColor: .systemOrange)
    static let error = Color(nsColor: .systemRed)
    static let info = Color.secondary

    static let currentRow = Color.accentColor.opacity(0.08)

    private static let speakers: [Color] = [.blue, .purple, .indigo, .teal, .brown, .cyan]

    static func speaker(_ tag: SpeakerTag) -> Color {
        speakers[abs(tag.number) % speakers.count]
    }

    static func dot(for status: LiveStatus) -> Color {
        switch status {
        case .live: live
        case .paused, .setupNeeded, .catchingUp, .behind: warning
        case .failed: error
        case .starting, .idle, .stopping: info
        }
    }
}
