import MeetingTranslatorCore
import Security
import SwiftUI

/// The access token for a private model mirror, kept in the login Keychain.
enum ModelSourceToken {
    private static var query: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "MimiVox model download token",
            kSecAttrAccount as String: "model-mirror",
        ]
    }

    static func read() -> String? {
        var match = query
        match[kSecReturnData as String] = true
        match[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(match as CFDictionary, &item) == errSecSuccess,
            let data = item as? Data
        else { return nil }
        return String(data: data, encoding: .utf8)
    }

    static var exists: Bool { read()?.isEmpty == false }

    /// An empty token removes the saved one.
    static func save(_ token: String) {
        SecItemDelete(query as CFDictionary)
        guard !token.isEmpty else { return }
        var item = query
        item[kSecValueData as String] = Data(token.utf8)
        SecItemAdd(item as CFDictionary, nil)
    }
}

/// Settings > Models > Download Source: the mirror that model downloads use.
struct ModelSourceSection: View {
    @EnvironmentObject private var store: TranslatorStore

    @State private var source = ModelSource.current()
    @State private var endpoint = ""
    @State private var token = ""
    @State private var hasToken = false
    @State private var check: Check = .idle
    @State private var error: String?

    private enum Check: Equatable {
        case idle, running, reachable
        case failed(String)
    }

    private var savedText: String {
        source.origin == .user ? source.effectiveEndpoint.absoluteString : ""
    }

    private var isEdited: Bool {
        endpoint.trimmingCharacters(in: .whitespaces) != savedText || !token.isEmpty
    }

    var body: some View {
        Section {
            if source.isLocked {
                LabeledContent(L10n.text("Download from"), value: source.effectiveEndpoint.absoluteString)
                    .textSelection(.enabled)
            } else {
                TextField(L10n.text("Download from"), text: $endpoint, prompt: Text(placeholder))
                    .onSubmit(apply)
            }
            SecureField(
                L10n.text("Access token"), text: $token,
                prompt: Text(L10n.text(hasToken ? "Saved" : "Optional"))
            )
            .onSubmit(apply)
            .disabled(source.endpoint == nil && endpoint.isEmpty)
            HStack {
                checkLabel
                Spacer()
                Button(L10n.text("Test")) { Task { await test() } }
                    .disabled(check == .running)
                if !source.isLocked || !token.isEmpty {
                    Button(L10n.text("Apply"), action: apply)
                        .disabled(!isEdited || store.hasActiveSession)
                }
            }
        } header: {
            Text(L10n.text("Download Source"))
        } footer: {
            SettingsFooter(footer)
        }
        .onAppear(perform: load)
    }

    private var placeholder: String {
        source.origin == .build ? source.effectiveEndpoint.absoluteString : ModelSource.standardEndpoint.absoluteString
    }

    private var footer: String {
        if let error { return error }
        if store.hasActiveSession && isEdited {
            return L10n.text("Apply after the meeting ends. It restarts the helper.")
        }
        return switch source.origin {
        case .managed:
            L10n.text(
                "Set by your organization. A Hugging Face mirror, such as Artifactory or Nexus. The token is only sent to this address."
            )
        case .build:
            L10n.text(
                "This copy of MimiVox was built to download from this mirror. Enter another address to override it.")
        case .user, .standard:
            L10n.text(
                "Leave empty for huggingface.co, or enter a Hugging Face mirror such as Artifactory or Nexus. The token is only sent to that mirror."
            )
        }
    }

    @ViewBuilder
    private var checkLabel: some View {
        switch check {
        case .idle: EmptyView()
        case .running: ProgressView().controlSize(.small)
        case .reachable:
            Label(L10n.text("Reachable"), systemImage: "checkmark.circle").foregroundStyle(.secondary)
        case .failed(let reason):
            Label(L10n.text(reason), systemImage: "exclamationmark.triangle").foregroundStyle(Palette.warning)
                .lineLimit(2)
        }
    }

    private func load() {
        source = ModelSource.current()
        endpoint = savedText
        hasToken = ModelSourceToken.exists
    }

    /// The source the fields describe, before they are saved.
    private var draft: ModelSource? {
        let text = endpoint.trimmingCharacters(in: .whitespaces)
        if source.isLocked { return source }
        if text.isEmpty {
            return ModelSource.resolve(
                managed: nil, user: nil,
                build: Bundle.main.object(forInfoDictionaryKey: ModelSource.infoPlistKey) as? String)
        }
        return ModelSource.normalize(text).map { ModelSource(endpoint: $0, origin: .user) }
    }

    private func apply() {
        guard isEdited, !store.hasActiveSession else { return }
        guard draft != nil else {
            error = L10n.text("Enter an address that starts with https:// or http://.")
            return
        }
        error = nil
        if !source.isLocked {
            let text = endpoint.trimmingCharacters(in: .whitespaces)
            if text.isEmpty {
                UserDefaults.standard.removeObject(forKey: ModelSource.defaultsKey)
            } else if let url = ModelSource.normalize(text) {
                UserDefaults.standard.set(url.absoluteString, forKey: ModelSource.defaultsKey)
            }
        }
        if !token.isEmpty { ModelSourceToken.save(token) }
        token = ""
        load()
        check = .idle
        // huggingface_hub reads the endpoint once, when the helper starts.
        Task { await store.retryHelper() }
    }

    private func test() async {
        guard let draft else {
            check = .failed(L10n.text("Not a valid address"))
            return
        }
        check = .running
        var request = URLRequest(url: draft.probeURL(), timeoutInterval: 10)
        request.httpMethod = "HEAD"
        let secret = token.isEmpty ? (draft.endpoint == nil ? nil : ModelSourceToken.read()) : token
        if let secret, !secret.isEmpty, draft.endpoint != nil {
            request.setValue("Bearer \(secret)", forHTTPHeaderField: "Authorization")
        }
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            let code = response.statusCode
            check =
                switch code {
                case 200..<400: .reachable
                case 401, 403: .failed(String(format: L10n.text("Access denied (%@). Check the token."), "\(code)"))
                case 404: .failed(L10n.text("Translation model not found on this mirror (404)."))
                default: .failed(String(format: L10n.text("The mirror answered %@."), "\(code)"))
                }
        } catch {
            check = .failed(error.localizedDescription)
        }
    }
}
