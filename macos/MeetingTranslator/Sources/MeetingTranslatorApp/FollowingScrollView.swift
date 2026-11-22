import AppKit
import MeetingTranslatorCore
import SwiftUI

struct ScrollTarget: Equatable {
    var id: String
    var nonce: Int
}

struct FollowingScrollView<Content: View>: View {
    var rowCount: Int
    var tail: String
    var spacing: CGFloat
    var maxContentWidth: CGFloat?
    var topInset: CGFloat = 0
    var bottomInset: CGFloat = 1
    var scrollTarget: ScrollTarget?
    @ViewBuilder var content: Content

    @State private var isAtBottom = true

    private static var bottomID: String { "following-scroll-bottom" }
    private static var pinTolerance: CGFloat { 24 }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: spacing) {
                    content
                    Color.clear
                        .frame(height: bottomInset)
                        .id(Self.bottomID)
                }
                .padding(.top, topInset)
                .frame(maxWidth: maxContentWidth ?? .infinity, alignment: .leading)
                .frame(maxWidth: .infinity, alignment: .center)
                .background(
                    ViewerScrollObserver { distance in
                        isAtBottom = distance <= Self.pinTolerance
                    })
            }
            .bottomAnchor(followingSizeChanges: isAtBottom)
            .scrollIndicators(.visible)
            .onGeometryChange(for: CGFloat.self) { geometry in
                geometry.size.height
            } action: { _ in
                if isAtBottom { follow(proxy, animated: false) }
            }
            .onChange(of: rowCount) { _, _ in
                if isAtBottom { follow(proxy, animated: true) }
            }
            .onChange(of: tail) { _, _ in
                if isAtBottom { follow(proxy, animated: false) }
            }
            .onChange(of: scrollTarget) { _, target in
                guard let target else { return }
                isAtBottom = false
                withAnimation(.easeOut(duration: 0.2)) { proxy.scrollTo(target.id, anchor: .center) }
            }
            .overlay(alignment: .bottomTrailing) {
                if !isAtBottom {
                    Button {
                        isAtBottom = true
                        follow(proxy, animated: true)
                    } label: {
                        Label(L10n.text("Jump to latest"), systemImage: "arrow.down")
                            .font(.caption.weight(.semibold))
                            .padding(.horizontal, 10)
                            .padding(.vertical, 5)
                            .background(.regularMaterial, in: Capsule())
                    }
                    .buttonStyle(.plain)
                    .padding(6)
                }
            }
        }
    }

    /// Scrolls after one runloop turn, once the new row is laid out.
    private func follow(_ proxy: ScrollViewProxy, animated: Bool) {
        DispatchQueue.main.async {
            if animated {
                withAnimation(.easeOut(duration: followAnimationDuration)) {
                    proxy.scrollTo(Self.bottomID, anchor: .bottom)
                }
            } else {
                proxy.scrollTo(Self.bottomID, anchor: .bottom)
            }
        }
    }
}

let followAnimationDuration = 0.18

extension View {
    /// Opens at the bottom, and keeps the bottom in place as content grows only while pinned.
    /// An unpinned viewer resting at the bottom (say, after jumping to a recent line) stays put.
    @ViewBuilder
    fileprivate func bottomAnchor(followingSizeChanges pinned: Bool) -> some View {
        if #available(macOS 15, *) {
            defaultScrollAnchor(.bottom, for: .initialOffset)
                .defaultScrollAnchor(.bottom, for: .alignment)
                .defaultScrollAnchor(pinned ? .bottom : nil, for: .sizeChanges)
        } else {
            defaultScrollAnchor(pinned ? .bottom : nil)
        }
    }
}

private struct ViewerScrollObserver: NSViewRepresentable {
    var onViewerScroll: (_ distanceFromBottom: CGFloat) -> Void

    func makeNSView(context: Context) -> ObserverView { ObserverView() }

    func updateNSView(_ view: ObserverView, context: Context) {
        view.onViewerScroll = onViewerScroll
    }

    final class ObserverView: NSView {
        var onViewerScroll: ((CGFloat) -> Void)?
        private var observer: NSObjectProtocol?

        override func viewDidMoveToWindow() {
            super.viewDidMoveToWindow()
            stopObserving()
            guard let scroller = enclosingScrollView else { return }
            observer = NotificationCenter.default.addObserver(
                forName: NSScrollView.didLiveScrollNotification, object: scroller, queue: .main
            ) { [weak self] _ in
                MainActor.assumeIsolated {
                    self?.onViewerScroll?(Self.distanceFromBottom(of: scroller))
                }
            }
        }

        private static func distanceFromBottom(of scroller: NSScrollView) -> CGFloat {
            guard let document = scroller.documentView else { return 0 }
            let clip = scroller.contentView
            let visibleBottom = clip.bounds.maxY - clip.contentInsets.bottom
            return document.isFlipped
                ? document.bounds.maxY - visibleBottom
                : clip.bounds.minY - document.bounds.minY
        }

        private func stopObserving() {
            if let observer { NotificationCenter.default.removeObserver(observer) }
            observer = nil
        }

        isolated deinit {
            stopObserving()
        }
    }
}
