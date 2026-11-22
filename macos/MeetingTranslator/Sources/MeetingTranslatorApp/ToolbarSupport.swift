import SwiftUI

/// A fixed gap between two toolbar items, so bordered controls do not read as one segmented control.
/// Older systems pack the items on their own, so the gap only exists where the toolbar supports it.
struct ToolbarGap: ToolbarContent {
    var body: some ToolbarContent {
        if #available(macOS 26.0, *) {
            ToolbarSpacer(.fixed)
        }
    }
}

private struct ToolbarIconStyle: ViewModifier {
    var isOn: Bool
    @Environment(\.controlActiveState) private var activeState
    @Environment(\.colorScheme) private var colorScheme

    private var restingOpacity: Double { colorScheme == .dark ? 0.56 : 0.65 }
    private var inactiveOpacity: Double { colorScheme == .dark ? 0.28 : 0.3 }

    private var style: AnyShapeStyle {
        if activeState == .inactive { return AnyShapeStyle(Color.primary.opacity(inactiveOpacity)) }
        if isOn { return AnyShapeStyle(Color.accentColor) }
        return AnyShapeStyle(Color.primary.opacity(restingOpacity))
    }

    func body(content: Content) -> some View {
        content
            .buttonStyle(.borderless)
            .imageScale(.large)
            .foregroundStyle(style)
            .padding(.horizontal, 6)
    }
}

extension View {
    /// An icon-only toolbar button or toggle. Under the default style the toolbar draws these dimmed, like a
    /// disabled control, so they take the borderless style and keep the hit area of a standard toolbar button.
    /// The borderless style draws no pressed state, so a toggle that is on takes the accent colour, and the icon
    /// takes the colour and the inactive dimming of the system-drawn toolbar items beside it.
    func toolbarIcon(isOn: Bool = false) -> some View {
        modifier(ToolbarIconStyle(isOn: isOn))
    }
}
