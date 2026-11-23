import AppKit
import MeetingTranslatorCore
import Sparkle
import SwiftUI

/// The About, Help, and Changelog windows, each a single-instance `Window` scene.
enum InfoWindow: String, CaseIterable {
    case about, help, changelog

    var id: String { "info-\(rawValue)" }

    var title: String {
        switch self {
        case .about: L10n.text("About MimiVox")
        case .help: L10n.text("MimiVox Help")
        case .changelog: L10n.text("MimiVox Changelog")
        }
    }
}

/// Replaces the stock About item (which ignores the changelog) and the empty Help menu.
struct InfoCommands: Commands {
    let updaterController: SPUStandardUpdaterController?
    @Environment(\.openWindow) private var openWindow

    var body: some Commands {
        CommandGroup(replacing: .appInfo) {
            Button(InfoWindow.about.title) { open(.about) }
        }
        if let updaterController {
            CommandGroup(after: .appInfo) {
                Button(L10n.text("Check for Updates…")) {
                    updaterController.checkForUpdates(nil)
                }
            }
        }
        CommandGroup(replacing: .help) {
            Button(InfoWindow.help.title) { open(.help) }
                .keyboardShortcut("?", modifiers: .command)
            Button(L10n.text("Changelog")) { open(.changelog) }
        }
    }

    private func open(_ window: InfoWindow) {
        openWindow(id: window.id)
        NSApp.activate(ignoringOtherApps: true)
    }
}

enum AppInfo {
    static let name = "MimiVox"

    static var version: String {
        Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String
            ?? Changelog.latestRelease(in: Changelog.parse(text(of: "CHANGELOG")))?.version
            ?? "dev"
    }

    /// Markdown files bundled from Sources/MeetingTranslatorApp/Resources.
    /// Looks in the .app's Resources first, then the SwiftPM resource bundle
    /// (checked manually: `Bundle.module` traps when the bundle is missing).
    static func text(of resource: String) -> String {
        let name = resource.replacingOccurrences(of: ".md", with: "")
        let bundleName = "MeetingTranslator_MeetingTranslatorApp.bundle"
        let candidates: [Bundle?] = [
            Bundle.main,
            Bundle.main.resourceURL.flatMap { Bundle(url: $0.appendingPathComponent(bundleName)) },
            Bundle(url: Bundle.main.bundleURL.appendingPathComponent(bundleName)),
            Bundle(url: Bundle.main.bundleURL.deletingLastPathComponent().appendingPathComponent(bundleName)),
        ]
        for bundle in candidates {
            if let url = bundle?.url(forResource: name, withExtension: "md")
                ?? bundle?.url(forResource: name, withExtension: "md", subdirectory: "Resources"),
                let text = try? String(contentsOf: url, encoding: .utf8)
            {
                return text
            }
        }
        return ""
    }
}

/// Renders inline Markdown (`code`, **bold**, links); block syntax is handled by the caller.
private func inline(_ markdown: String) -> Text {
    let options = AttributedString.MarkdownParsingOptions(interpretedSyntax: .inlineOnlyPreservingWhitespace)
    return Text((try? AttributedString(markdown: markdown, options: options)) ?? AttributedString(markdown))
}

private struct Bullet: View {
    let text: String

    var body: some View {
        HStack(alignment: .firstTextBaseline, spacing: 6) {
            Text("•")
            inline(text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
        }
    }
}

struct HelpView: View {
    private let blocks = HelpView.blocks(AppInfo.text(of: L10n.text("HELP.md")))

    private enum Block {
        case title(String)
        case heading(String)
        case bullet(String)
        case paragraph(String)
    }

    /// Groups lines into blocks: `#`/`##` headings, `- ` bullets (indented lines continue
    /// the bullet), and paragraphs of consecutive plain lines.
    private static func blocks(_ markdown: String) -> [Block] {
        var blocks: [Block] = []
        for raw in markdown.split(separator: "\n", omittingEmptySubsequences: false) {
            let line = raw.trimmingCharacters(in: .whitespaces)
            if line.isEmpty {
                blocks.append(.paragraph(""))
            } else if line.hasPrefix("## ") {
                blocks.append(.heading(String(line.dropFirst(3))))
            } else if line.hasPrefix("# ") {
                blocks.append(.title(String(line.dropFirst(2))))
            } else if line.hasPrefix("- ") {
                blocks.append(.bullet(String(line.dropFirst(2))))
            } else if case .bullet(let text)? = blocks.last, raw.first == " " {
                blocks[blocks.count - 1] = .bullet(text + " " + line)
            } else if case .paragraph(let text)? = blocks.last, !text.isEmpty {
                blocks[blocks.count - 1] = .paragraph(text + " " + line)
            } else {
                blocks.append(.paragraph(line))
            }
        }
        return blocks.filter { if case .paragraph("") = $0 { false } else { true } }
    }

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 8) {
                if blocks.isEmpty {
                    Text(L10n.text("Help unavailable.")).foregroundStyle(.secondary)
                }
                ForEach(Array(blocks.enumerated()), id: \.offset) { _, block in
                    switch block {
                    case .title(let text): Text(text).font(.title.bold())
                    case .heading(let text): Text(text).font(.title3.bold()).padding(.top, 12)
                    case .bullet(let text): Bullet(text: text)
                    case .paragraph(let text):
                        inline(text).textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(24)
        }
        .frame(minWidth: 520, minHeight: 480)
    }
}

struct ChangelogView: View {
    private let releases = Changelog.parse(AppInfo.text(of: "CHANGELOG"))

    var body: some View {
        ScrollView {
            VStack(alignment: .leading, spacing: 20) {
                if releases.isEmpty {
                    Text(L10n.text("Changelog unavailable.")).foregroundStyle(.secondary)
                }
                ForEach(releases) { release in
                    VStack(alignment: .leading, spacing: 8) {
                        HStack(alignment: .firstTextBaseline) {
                            Text(release.version).font(.title2.bold())
                            if let date = release.date {
                                Text(date).foregroundStyle(.secondary)
                            }
                        }
                        ForEach(release.sections, id: \.title) { section in
                            Text(section.title).font(.headline).padding(.top, 4)
                            ForEach(Array(section.items.enumerated()), id: \.offset) { _, item in
                                Bullet(text: item)
                            }
                        }
                        Divider().padding(.top, 8)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
            .padding(24)
        }
        .frame(minWidth: 460, minHeight: 360)
    }
}

struct AboutView: View {
    @Environment(\.openWindow) private var openWindow

    /// Unbundled runs (`swift run`, snapshots) have only the generic executable icon.
    @ViewBuilder private var appIcon: some View {
        if Bundle.main.bundleURL.pathExtension == "app" {
            Image(nsImage: NSApp.applicationIconImage).resizable()
        } else {
            Image(systemName: "captions.bubble").resizable().scaledToFit().foregroundStyle(.tint).padding(12)
        }
    }

    var body: some View {
        VStack(spacing: 10) {
            appIcon.frame(width: 96, height: 96)
            Text(AppInfo.name).font(.title.bold())
            Text(String(format: L10n.text("Version %@"), AppInfo.version)).foregroundStyle(.secondary)
            Text(L10n.text("Live meeting captions and translation, on this Mac."))
                .font(.callout).multilineTextAlignment(.center).foregroundStyle(.secondary)
            Button(L10n.text("View Changelog…")) { openWindow(id: InfoWindow.changelog.id) }.padding(.top, 8)
        }
        .padding(28)
        .frame(width: 340)
    }
}
