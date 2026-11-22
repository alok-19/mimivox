import Foundation
import MeetingTranslatorCore
import XCTest

final class LocalizationTests: XCTestCase {
    func testJapaneseCatalogAndEnglishFallback() {
        XCTAssertEqual(L10n.text("Home", language: .japanese), "ホーム")
        XCTAssertEqual(L10n.text("Home", language: .english), "Home")
    }

    func testSystemLanguageUsesPreferredSupportedLanguage() {
        let firstSupportedLanguage = Locale.preferredLanguages.first { language in
            let code = Locale(identifier: language).language.languageCode?.identifier
            return code == "en" || code == "ja"
        }
        let expected = firstSupportedLanguage?.hasPrefix("ja") == true ? "ホーム" : "Home"

        XCTAssertEqual(L10n.text("Home", language: .system), expected)
    }

    func testSavedLanguageCanBeLoadedFromAppPreferences() {
        let suiteName = "mimivox.localization-tests.\(UUID().uuidString)"
        let preferences = UserDefaults(suiteName: suiteName)!
        defer { preferences.removePersistentDomain(forName: suiteName) }

        preferences.set(AppLanguage.japanese.rawValue, forKey: AppLanguage.defaultsKey)
        XCTAssertEqual(AppLanguage.stored(in: preferences), .japanese)

        preferences.set(AppLanguage.english.rawValue, forKey: AppLanguage.defaultsKey)
        XCTAssertEqual(AppLanguage.stored(in: preferences), .english)
    }

    func testEnglishToJapaneseRemainsASelectableMeetingPair() {
        XCTAssertTrue(LanguagePair.supported.contains { $0.id == "en-ja" })
    }

    func testAskTheMeetingStringsAreTranslated() {
        for key in [
            "Ask the meeting", "Ask the room", "No transcript lines cited.",
            "Copy the Japanese", "Jump to this line in the transcript",
            "Ask needs the notes model. Download it in Settings, under Models.",
            "What did Tanaka-san say about the deadline?",
        ] {
            XCTAssertNotEqual(L10n.text(key, language: .japanese), key, key)
        }
    }
}
