import MeetingTranslatorCore
import SwiftUI

/// The only thing on the Live page until the speech models are ready.
struct SetupCard: View {
    var body: some View {
        VStack(spacing: 24) {
            Image(systemName: "captions.bubble.fill")
                .font(.system(size: 40))
                .symbolRenderingMode(.hierarchical)
                .foregroundStyle(Color.accentColor)
            VStack(spacing: 8) {
                Text(L10n.text("Set up MimiVox"))
                    .font(.title.weight(.semibold))
                Text(L10n.text("MimiVox turns Japanese meetings into English captions, entirely on this Mac."))
                    .font(.callout)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
            }
            SetupStateView()
        }
        .frame(maxWidth: 440)
        .padding(32)
        .frame(maxWidth: .infinity, maxHeight: .infinity)
    }
}

struct SetupStateView: View {
    @EnvironmentObject private var store: TranslatorStore

    var body: some View {
        switch store.setup {
        case .needsDownload(let total, let free):
            needsDownload(total: total, free: free)
        case .downloading(let progress):
            downloading(progress)
        case .failed(let failure):
            failed(failure)
        case .loading, .ready, nil:
            HStack(spacing: 8) {
                ProgressView().controlSize(.small)
                Text(L10n.text("Getting ready…"))
                    .foregroundStyle(.secondary)
            }
        }
    }

    private func needsDownload(total: Int64, free: Int64) -> some View {
        VStack(spacing: 12) {
            Button {
                Task { await store.downloadModels() }
            } label: {
                Text(String(format: L10n.text("Download models (%@)"), SetupState.byteText(total)))
                    .frame(minWidth: 220)
            }
            .buttonStyle(.borderedProminent)
            .controlSize(.large)
            Text(diskLine(total: total, free: free))
                .font(.caption)
                .foregroundStyle(free > 0 && free < total ? Palette.warning : Color.secondary)
        }
    }

    private func diskLine(total: Int64, free: Int64) -> String {
        let offline = L10n.text("One-time download, then MimiVox works offline.")
        guard free > 0 else { return offline }
        return free < total
            ? String(
                format: L10n.text("Needs %@ of free space. This Mac has %@."),
                SetupState.byteText(total), SetupState.byteText(free))
            : String(format: L10n.text("%@ free on this Mac. %@"), SetupState.byteText(free), offline)
    }

    private func downloading(_ progress: SetupState.Progress) -> some View {
        VStack(spacing: 8) {
            ProgressView(value: progress.fraction)
            Text(downloadLine(progress))
                .font(.caption.monospacedDigit())
                .foregroundStyle(.secondary)
            Button(L10n.text("Cancel")) { Task { await store.cancelModelDownload() } }
                .controlSize(.regular)
                .padding(.top, 4)
        }
    }

    private func downloadLine(_ progress: SetupState.Progress) -> String {
        let parts = [
            String(
                format: L10n.text("%@ of %@"),
                SetupState.byteText(progress.doneBytes), SetupState.byteText(progress.totalBytes)),
            progress.speedText,
            progress.etaText,
        ]
        return parts.compactMap { $0 }.joined(separator: " · ")
    }

    private func failed(_ failure: SetupState.Failure) -> some View {
        VStack(spacing: 12) {
            Text(failureText(failure))
                .multilineTextAlignment(.center)
                .fixedSize(horizontal: false, vertical: true)
            if failure.reason == .error, !failure.message.isEmpty {
                Text(failure.message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .multilineTextAlignment(.center)
                    .lineLimit(3)
                    .textSelection(.enabled)
            }
            Button(L10n.text("Retry")) { Task { await store.downloadModels() } }
                .buttonStyle(.borderedProminent)
                .controlSize(.large)
        }
    }

    private func failureText(_ failure: SetupState.Failure) -> String {
        switch failure.reason {
        case .offline: L10n.text("No internet connection. MimiVox needs it once to download the speech models.")
        case .diskFull:
            L10n.text("There isn't enough free disk space for the speech models. Free up some space, then try again.")
        case .error: L10n.text("The speech models couldn't be downloaded.")
        }
    }
}
