import Foundation

public struct ChangelogSection: Equatable, Sendable {
    public var title: String
    public var items: [String]
}

public struct ChangelogRelease: Equatable, Sendable, Identifiable {
    public var version: String
    public var date: String?
    public var sections: [ChangelogSection]
    public var id: String { version }
}

public enum Changelog {
    /// Parses Keep-a-Changelog style markdown: `## [1.0.0] - 2026-01-01`,
    /// `### Added`, `- item` (indented lines continue the previous item).
    public static func parse(_ markdown: String) -> [ChangelogRelease] {
        var releases: [ChangelogRelease] = []
        for raw in markdown.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = String(raw)
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("## ") {
                let (version, date) = parseHeading(String(trimmed.dropFirst(3)))
                releases.append(ChangelogRelease(version: version, date: date, sections: []))
            } else if trimmed.hasPrefix("### "), !releases.isEmpty {
                releases[releases.count - 1].sections.append(
                    ChangelogSection(title: String(trimmed.dropFirst(4)), items: [])
                )
            } else if trimmed.hasPrefix("- "), !releases.isEmpty {
                let item = String(trimmed.dropFirst(2))
                if releases[releases.count - 1].sections.isEmpty {
                    releases[releases.count - 1].sections.append(ChangelogSection(title: "Changes", items: []))
                }
                let last = releases.count - 1
                releases[last].sections[releases[last].sections.count - 1].items.append(item)
            } else if !trimmed.isEmpty, line.first == " ", !releases.isEmpty,
                let last = releases.indices.last,
                let s = releases[last].sections.indices.last,
                let i = releases[last].sections[s].items.indices.last
            {
                releases[last].sections[s].items[i] += " " + trimmed
            }
        }
        return releases
    }

    /// The newest shipped release, skipping an `## [Unreleased]` section.
    public static func latestRelease(in releases: [ChangelogRelease]) -> ChangelogRelease? {
        releases.first { $0.version.caseInsensitiveCompare("Unreleased") != .orderedSame }
    }

    private static func parseHeading(_ text: String) -> (String, String?) {
        var version = text
        var date: String?
        if let range = text.range(of: " - ") {
            version = String(text[..<range.lowerBound])
            date = String(text[range.upperBound...]).trimmingCharacters(in: .whitespaces)
        }
        version = version.trimmingCharacters(in: CharacterSet(charactersIn: "[] "))
        return (version, date)
    }
}
