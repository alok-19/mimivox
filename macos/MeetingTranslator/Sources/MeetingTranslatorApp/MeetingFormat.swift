import Foundation
import MeetingTranslatorCore

@MainActor
enum MeetingFormat {
    private static let internetDate = ISO8601DateFormatter()

    private static let relativeDate: DateFormatter = {
        let formatter = DateFormatter()
        formatter.dateStyle = .medium
        formatter.timeStyle = .short
        formatter.doesRelativeDateFormatting = true
        return formatter
    }()

    static func date(from iso: String) -> Date? {
        let whole = iso.replacingOccurrences(of: #"\.\d+"#, with: "", options: .regularExpression)
        return internetDate.date(from: whole)
    }

    static func title(startedAt: String, fallback: String) -> String {
        relativeDate.locale = AppLanguage.selected.locale
        return date(from: startedAt).map(relativeDate.string(from:)) ?? fallback
    }

    static func title(_ meeting: ArchivedSessionSummary) -> String {
        title(startedAt: meeting.startedAt, fallback: meeting.id)
    }

    private static let recordingExtensions: Set<String> = [
        "wav", "mp3", "m4a", "aac", "flac", "aiff", "caf", "mp4", "mov", "ogg", "opus",
    ]

    static var currentDate: () -> Date = { .now }

    private static var calendar: Calendar {
        var calendar = Calendar.current
        calendar.timeZone = NSTimeZone.default
        return calendar
    }

    private static func style() -> Date.FormatStyle {
        Date.FormatStyle(locale: AppLanguage.selected.locale, calendar: calendar, timeZone: NSTimeZone.default)
    }

    static func source(label: String) -> String {
        if let known = LiveSessionSource(rawValue: label) { return L10n.text(known.title) }
        if fileName(label: label) != nil { return L10n.text("Recording") }
        return label.isEmpty ? L10n.text("Unknown source") : label
    }

    static func fileName(label: String) -> String? {
        guard LiveSessionSource(rawValue: label) == nil else { return nil }
        let name = (label as NSString).lastPathComponent
        return recordingExtensions.contains((name as NSString).pathExtension.lowercased()) ? name : nil
    }

    static func time(_ date: Date) -> String {
        date.formatted(style().hour().minute())
    }

    static func stamp(_ date: Date) -> String {
        "\(date.formatted(style().month(.abbreviated).day().year())), \(time(date))"
    }

    static func day(_ date: Date) -> String {
        let now = currentDate()
        let calendar = calendar
        if calendar.isDate(date, inSameDayAs: now) { return L10n.text("Today") }
        if let yesterday = calendar.date(byAdding: .day, value: -1, to: now),
            calendar.isDate(date, inSameDayAs: yesterday)
        {
            return L10n.text("Yesterday")
        }
        return date.formatted(style().month(.abbreviated).day().year())
    }

    static func rowTitle(_ meeting: ArchivedSessionSummary) -> String {
        if let headline = meeting.headline { return headline }
        return date(from: meeting.startedAt).map(time) ?? meeting.id
    }

    static func rowDetail(_ meeting: ArchivedSessionSummary) -> String {
        var parts: [String] = []
        if meeting.headline != nil, let started = date(from: meeting.startedAt) { parts.append(time(started)) }
        parts.append(source(label: meeting.label))
        if let seconds = meeting.durationSeconds { parts.append(duration(seconds)) }
        return parts.joined(separator: " · ")
    }

    static func menuTitle(_ meeting: ArchivedSessionSummary) -> String {
        let source = source(label: meeting.label)
        guard let started = date(from: meeting.startedAt) else { return source }
        return "\(day(started)), \(time(started)) · \(source)"
    }

    static func days(_ meetings: [ArchivedSessionSummary]) -> [MeetingDay] {
        let calendar = calendar
        var days: [MeetingDay] = []
        for meeting in meetings {
            let started = date(from: meeting.startedAt)
            let key = started.map { calendar.startOfDay(for: $0) }
            if let last = days.last, last.key == key {
                days[days.count - 1].meetings.append(meeting)
            } else {
                days.append(MeetingDay(key: key, title: started.map(day) ?? L10n.text("Earlier"), meetings: [meeting]))
            }
        }
        return days
    }

    static func timecode(_ seconds: TimeInterval) -> String {
        let whole = Int(max(0, seconds).rounded(.down))
        return String(format: "%02d:%02d", whole / 60, whole % 60)
    }

    static func duration(_ seconds: TimeInterval) -> String {
        Duration.seconds(seconds.rounded()).formatted(
            .units(allowed: [.hours, .minutes, .seconds], width: .abbreviated, maximumUnitCount: 2)
                .locale(AppLanguage.selected.locale))
    }
}

struct MeetingDay: Identifiable {
    var key: Date?
    var title: String
    var meetings: [ArchivedSessionSummary]

    var id: String { key.map { String($0.timeIntervalSinceReferenceDate) } ?? title }
}

extension Array where Element == ArchivedSessionSummary {
    func neighbour(of id: String) -> String? {
        guard let index = firstIndex(where: { $0.id == id }) else { return nil }
        if indices.contains(index + 1) { return self[index + 1].id }
        return index > 0 ? self[index - 1].id : nil
    }
}
