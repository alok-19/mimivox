import AppKit
import MeetingTranslatorCore
import SwiftUI

/// Wraps its children left to right like text.
struct WrappingLayout: Layout {
    var lineSpacing: CGFloat = 2

    func sizeThatFits(proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) -> CGSize {
        arrange(subviews, width: proposal.width ?? .infinity).size
    }

    func placeSubviews(in bounds: CGRect, proposal: ProposedViewSize, subviews: Subviews, cache: inout ()) {
        for (subview, origin) in zip(subviews, arrange(subviews, width: bounds.width).origins) {
            subview.place(at: CGPoint(x: bounds.minX + origin.x, y: bounds.minY + origin.y), proposal: .unspecified)
        }
    }

    private func arrange(_ subviews: Subviews, width: CGFloat) -> (origins: [CGPoint], size: CGSize) {
        var origins: [CGPoint] = []
        var x: CGFloat = 0
        var y: CGFloat = 0
        var rowHeight: CGFloat = 0
        var maxX: CGFloat = 0
        for subview in subviews {
            let size = subview.sizeThatFits(.unspecified)
            if x > 0, x + size.width > width {
                x = 0
                y += rowHeight + lineSpacing
                rowHeight = 0
            }
            origins.append(CGPoint(x: x, y: y))
            x += size.width
            maxX = max(maxX, x)
            rowHeight = max(rowHeight, size.height)
        }
        return (origins, CGSize(width: maxX, height: y + rowHeight))
    }
}

/// A JA caption with furigana over kanji. Hover (or click to pin) a word for its
/// reading and English gloss. Without tokens that spell `text` exactly it is
/// plain selectable text, so normal captions and old sessions look as before.
struct FuriganaText: View {
    var text: String
    var tokens: [LearningToken]?
    var size: CGFloat
    var color: Color = .secondary

    var body: some View {
        if let tokens = tokens?.matching(text) {
            WrappingLayout(lineSpacing: 2) {
                ForEach(Array(tokens.enumerated()), id: \.offset) { _, token in
                    WordCell(token: token, size: size, color: color)
                }
            }
        } else {
            Text(text)
                .font(.system(size: size))
                .foregroundStyle(color)
                .textSelection(.enabled)
        }
    }
}

private struct WordCell: View {
    var token: LearningToken
    var size: CGFloat
    var color: Color
    @State private var hovering = false
    @State private var pinned = false

    private var hasDetail: Bool { token.gloss != nil || token.rubyText != nil }

    var body: some View {
        VStack(spacing: 0) {
            // Always reserve the ruby line so every word shares one baseline.
            Text(token.rubyText ?? " ")
                .font(.system(size: size * 0.5))
                .foregroundStyle(color.opacity(0.9))
                .lineLimit(1)
            Text(token.surface)
                .font(.system(size: size))
                .foregroundStyle(color)
                .lineLimit(1)
                .underline(hasDetail && (hovering || pinned), color: color.opacity(0.6))
        }
        .fixedSize()
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
        .onTapGesture { if hasDetail { pinned.toggle() } }
        .popover(isPresented: .constant(hasDetail && (hovering || pinned)), arrowEdge: .bottom) {
            WordDetail(token: token)
        }
    }
}

private struct WordDetail: View {
    var token: LearningToken

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(token.base ?? token.surface).font(.title3.weight(.semibold))
            if let reading = token.rubyText {
                Text(reading).font(.callout).foregroundStyle(.secondary)
            }
            if let gloss = token.gloss {
                Text(gloss).font(.callout).fixedSize(horizontal: false, vertical: true)
            }
        }
        .padding(12)
        .frame(maxWidth: 280, alignment: .leading)
    }
}
