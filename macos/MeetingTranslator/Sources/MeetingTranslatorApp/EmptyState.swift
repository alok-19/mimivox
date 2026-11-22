import SwiftUI

/// `ContentUnavailableView` greys out its whole label, which makes a ready state read as disabled.
struct EmptyState<Actions: View>: View {
    var title: String
    var systemImage: String
    var message: String?
    var tint: Color = .accentColor
    var showsProgress = false
    @ViewBuilder var actions: Actions

    var body: some View {
        VStack(spacing: 16) {
            if showsProgress {
                ProgressView()
                    .controlSize(.large)
                    .frame(height: 40)
            } else {
                Image(systemName: systemImage)
                    .font(.system(size: 40))
                    .foregroundStyle(tint)
                    .accessibilityHidden(true)
            }
            VStack(spacing: 8) {
                Text(title)
                    .font(.title2.weight(.semibold))
                    .multilineTextAlignment(.center)
                if let message {
                    Text(message)
                        .font(.body)
                        .foregroundStyle(.secondary)
                        .multilineTextAlignment(.center)
                }
            }
            actions
        }
        .frame(width: 380)
        .padding(24)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

extension EmptyState where Actions == EmptyView {
    init(
        title: String, systemImage: String, message: String? = nil, tint: Color = .accentColor,
        showsProgress: Bool = false
    ) {
        self.init(title: title, systemImage: systemImage, message: message, tint: tint, showsProgress: showsProgress) {
            EmptyView()
        }
    }
}
