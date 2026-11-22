import Foundation

/// What Cmd+Q asks before quitting.
public enum QuitPrompt: Equatable, Sendable {
    /// A meeting is captioning or paused: offer to end it first so its transcript is saved.
    case activeMeeting(paused: Bool)
    /// Nothing is running: a plain "Quit MimiVox?" the user can turn off.
    case idle
}

public enum QuitConfirmation {
    /// UserDefaults key set by the idle prompt's "Don't ask again".
    public static let idleSuppressedKey = "quitConfirmationIdleSuppressed"

    /// The prompt to show, or nil to quit straight away. An active meeting always asks.
    public static func prompt(meetingActive: Bool, paused: Bool, idleSuppressed: Bool) -> QuitPrompt? {
        if meetingActive { return .activeMeeting(paused: paused) }
        return idleSuppressed ? nil : .idle
    }
}
