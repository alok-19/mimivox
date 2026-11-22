import AppKit
import Observation
import SwiftUI

@MainActor
enum FollowCheckRunner {
    static func requested(arguments: [String]) -> Bool {
        arguments.contains("--check-follow")
    }

    @Observable
    final class Rows {
        var count = 0
        var target: ScrollTarget?
        @ObservationIgnored var bottoms: [Int: CGFloat] = [:]
    }

    private struct RowBottoms: PreferenceKey {
        static let defaultValue: [Int: CGFloat] = [:]
        static func reduce(value: inout [Int: CGFloat], nextValue: () -> [Int: CGFloat]) {
            value.merge(nextValue()) { $1 }
        }
    }

    private struct Harness: View {
        var rows: Rows

        var body: some View {
            FollowingScrollView(rowCount: rows.count, tail: "\(rows.count)", spacing: 8, scrollTarget: rows.target) {
                ForEach(0..<rows.count, id: \.self) { index in
                    Text("Row \(index)")
                        .id("row-\(index)")
                        .frame(maxWidth: .infinity, minHeight: 40, alignment: .leading)
                        .background(
                            GeometryReader { geometry in
                                Color.clear.preference(
                                    key: RowBottoms.self, value: [index: geometry.frame(in: .named("harness")).maxY])
                            })
                }
            }
            .coordinateSpace(name: "harness")
            .onPreferenceChange(RowBottoms.self) { rows.bottoms.merge($0) { $1 } }
        }
    }

    private static let gaps: [Double] = [0.15, 0.45, 0.85, 1.1, 2.75].map { $0 * followAnimationDuration }
    private static let viewport = CGSize(width: 420, height: 300)

    static func run() -> Int32 {
        NSApplication.shared.setActivationPolicy(.prohibited)
        var failures = 0
        for gap in gaps {
            let result = trial(gap: gap, rows: 40)
            let ok = result.hiddenBelow <= 1
            print(
                String(
                    format: "gap=%.2fs rows=%d newest_row_hidden_below_pt=%.1f %@", gap, result.rows,
                    result.hiddenBelow, ok ? "ok" : "FAIL"))
            if !ok { failures += 1 }
        }
        for (label, row) in [("jumped_to_first_row", 0), ("jumped_to_recent_row", 29)] {
            let jumped = trial(gap: 0.2, rows: 12, jumpTo: row)
            let stayed = jumped.hiddenBelow > 100
            print("\(label) rows=\(jumped.rows) newest_row_left_offscreen=\(stayed) \(stayed ? "ok" : "FAIL")")
            if !stayed { failures += 1 }
        }
        return failures == 0 ? 0 : 1
    }

    private static func trial(gap: Double, rows count: Int, jumpTo row: Int? = nil) -> (rows: Int, hiddenBelow: CGFloat)
    {
        let model = Rows()
        let rect = NSRect(x: 100, y: 100, width: viewport.width, height: viewport.height)
        let hosting = NSHostingView(rootView: Harness(rows: model).frame(width: rect.width, height: rect.height))
        hosting.frame = rect
        let window = NSWindow(contentRect: rect, styleMask: [.borderless], backing: .buffered, defer: false)
        window.contentView = hosting
        window.setFrameOrigin(rect.origin)
        window.alphaValue = 0.01
        window.ignoresMouseEvents = true
        window.orderFrontRegardless()
        defer { window.orderOut(nil) }
        spin(0.3)

        if let row {
            model.count = 30
            spin(0.5)
            model.target = ScrollTarget(id: "row-\(row)", nonce: 1)
            spin(0.5)
        }
        for _ in 0..<count {
            model.count += 1
            spin(gap)
        }
        spin(0.8)

        guard let bottom = model.bottoms[model.count - 1] else { return (model.count, .infinity) }
        return (model.count, max(0, bottom - rect.height))
    }

    private static func spin(_ seconds: Double) {
        RunLoop.main.run(until: Date().addingTimeInterval(seconds))
    }
}
