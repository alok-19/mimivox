import AppKit
import CoreText
import Foundation

public enum MeetingNotesPDF {
    public static func render(
        _ notes: MeetingNotes,
        title: String,
        language: NotesLanguage,
        lineLabel: (Int) -> String? = { "#\($0)" }
    ) throws -> Data {
        let content = makeContent(notes, title: title, language: language, lineLabel: lineLabel)
        let output = NSMutableData()
        guard let consumer = CGDataConsumer(data: output as CFMutableData) else {
            throw PDFError.cannotCreateContext
        }
        var page = CGRect(x: 0, y: 0, width: 595, height: 842)
        guard let context = CGContext(consumer: consumer, mediaBox: &page, nil) else {
            throw PDFError.cannotCreateContext
        }

        let framesetter = CTFramesetterCreateWithAttributedString(content)
        var location = 0
        while location < content.length {
            context.beginPDFPage(nil)
            let bounds = CGRect(x: 54, y: 54, width: 487, height: 734)
            let path = CGPath(rect: bounds, transform: nil)
            let frame = CTFramesetterCreateFrame(
                framesetter, CFRange(location: location, length: 0), path, nil)
            let visible = CTFrameGetVisibleStringRange(frame)
            guard visible.length > 0 else {
                context.endPDFPage()
                throw PDFError.cannotLayoutContent
            }
            CTFrameDraw(frame, context)
            context.endPDFPage()
            location += visible.length
        }
        context.closePDF()
        return output as Data
    }

    private static func makeContent(
        _ notes: MeetingNotes,
        title: String,
        language: NotesLanguage,
        lineLabel: (Int) -> String?
    ) -> NSAttributedString {
        let content = NSMutableAttributedString()
        let japanese = language == .ja
        let titleStyle = NSParagraphStyle.default.mutableCopy() as! NSMutableParagraphStyle
        titleStyle.paragraphSpacing = 14
        append(title, font: .systemFont(ofSize: 23, weight: .semibold), style: titleStyle, to: content)

        appendSection(japanese ? "要約" : "Summary", in: content)
        append(notes.summary(in: language), in: content)

        appendSection(japanese ? "決定事項" : "Decisions", in: content)
        if notes.decisions.isEmpty {
            append(japanese ? "なし" : "None", in: content)
        } else {
            for decision in notes.decisions {
                append(
                    "• \(decision.text(in: language))\(citations(decision.lineIDs, lineLabel))",
                    in: content,
                    indentation: 14
                )
            }
        }

        appendSection(japanese ? "アクションアイテム" : "Action items", in: content)
        if notes.actionItems.isEmpty {
            append(japanese ? "なし" : "None", in: content)
        } else {
            for item in notes.actionItems {
                let labels = [
                    item.owner.map { "\(japanese ? "担当" : "Owner"): \($0)" },
                    item.due.map { "\(japanese ? "期限" : "Due"): \($0)" },
                ].compactMap { $0 }
                let details = labels.isEmpty ? "" : " (\(labels.joined(separator: ", ")))"
                append(
                    "• \(item.text(in: language))\(details)\(citations(item.lineIDs, lineLabel))",
                    in: content,
                    indentation: 14
                )
            }
        }
        return content
    }

    private static func appendSection(_ title: String, in content: NSMutableAttributedString) {
        let style = NSParagraphStyle.default.mutableCopy() as! NSMutableParagraphStyle
        style.paragraphSpacingBefore = 8
        style.paragraphSpacing = 5
        append(title, font: .systemFont(ofSize: 14, weight: .semibold), style: style, to: content)
    }

    private static func append(
        _ text: String,
        in content: NSMutableAttributedString,
        indentation: CGFloat = 0
    ) {
        let style = NSParagraphStyle.default.mutableCopy() as! NSMutableParagraphStyle
        style.lineSpacing = 3
        style.paragraphSpacing = 7
        style.headIndent = indentation
        append(text, font: .systemFont(ofSize: 11), style: style, to: content)
    }

    private static func append(
        _ text: String,
        font: NSFont,
        style: NSParagraphStyle,
        to content: NSMutableAttributedString
    ) {
        content.append(
            NSAttributedString(
                string: text + "\n",
                attributes: [.font: font, .paragraphStyle: style]
            ))
    }

    private static func citations(_ ids: [Int], _ lineLabel: (Int) -> String?) -> String {
        let labels = ids.compactMap(lineLabel)
        return labels.isEmpty ? "" : " (\(labels.joined(separator: ", ")))"
    }

    private enum PDFError: Error {
        case cannotCreateContext
        case cannotLayoutContent
    }
}
