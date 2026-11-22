import Foundation

public enum AppLanguage: String, CaseIterable, Identifiable, Sendable {
    case system
    case english = "en"
    case japanese = "ja"

    public static let defaultsKey = "mimivox.appLanguage"

    public var id: String { rawValue }

    public static var selected: AppLanguage {
        stored(in: .standard)
    }

    public static func stored(in preferences: UserDefaults) -> AppLanguage {
        preferences.string(forKey: defaultsKey).flatMap(AppLanguage.init(rawValue:)) ?? .system
    }

    public var title: String {
        switch self {
        case .system: L10n.text("System (automatic)")
        case .english: L10n.text("English")
        case .japanese: L10n.text("Japanese")
        }
    }

    public var locale: Locale {
        switch self {
        case .system: .autoupdatingCurrent
        case .english: Locale(identifier: "en")
        case .japanese: Locale(identifier: "ja")
        }
    }

    fileprivate var localizationCode: String? {
        switch self {
        case .system:
            let preferredCode = Locale.preferredLanguages
                .compactMap { Locale(identifier: $0).language.languageCode?.identifier }
                .first { $0 == "en" || $0 == "ja" }
            return preferredCode == "ja" ? "ja" : nil
        case .english:
            return nil
        case .japanese:
            return "ja"
        }
    }
}

public enum L10n {
    public static func text(_ sourceEnglish: String) -> String {
        text(sourceEnglish, language: .selected)
    }

    public static func text(_ sourceEnglish: String, language: AppLanguage) -> String {
        guard let code = language.localizationCode,
            let localizationPath = Bundle.module.path(forResource: code, ofType: "lproj"),
            let localizationBundle = Bundle(path: localizationPath)
        else {
            return sourceEnglish
        }

        return localizationBundle.localizedString(
            forKey: sourceEnglish, value: sourceEnglish, table: "Localizable")
    }
}
