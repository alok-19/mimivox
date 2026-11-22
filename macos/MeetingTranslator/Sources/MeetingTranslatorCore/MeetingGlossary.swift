import Foundation

public enum GlossaryTermAddition: Equatable, Sendable {
    case added(profileName: String)
    case alreadyExists(profileName: String)
}

extension MeetingSettings {
    @discardableResult
    public mutating func addGlossaryTerm(
        source: String,
        target: String,
        fallbackProfileName: String
    ) -> GlossaryTermAddition? {
        let source = source.trimmingCharacters(in: .whitespacesAndNewlines)
        let target = target.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !source.isEmpty, !target.isEmpty else { return nil }

        let profileName: String
        if !activeGlossaryProfile.isEmpty,
            glossaryProfiles.contains(where: { $0.name == activeGlossaryProfile })
        {
            profileName = activeGlossaryProfile
        } else {
            profileName = fallbackProfileName.trimmingCharacters(in: .whitespacesAndNewlines)
            guard !profileName.isEmpty else { return nil }
            if !glossaryProfiles.contains(where: { $0.name == profileName }) {
                glossaryProfiles.append(GlossaryProfile(name: profileName))
            }
            activeGlossaryProfile = profileName
        }

        guard let index = glossaryProfiles.firstIndex(where: { $0.name == profileName }) else { return nil }
        guard
            !glossaryProfiles[index].entries.contains(where: {
                $0.source.caseInsensitiveCompare(source) == .orderedSame
            })
        else {
            return .alreadyExists(profileName: profileName)
        }
        glossaryProfiles[index].entries.append(GlossaryTerm(source: source, target: target))
        return .added(profileName: profileName)
    }
}
