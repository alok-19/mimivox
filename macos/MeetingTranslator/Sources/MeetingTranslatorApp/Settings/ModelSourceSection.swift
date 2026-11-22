import MeetingTranslatorCore
import Security
import SwiftUI

/// The access token for a private model mirror, kept in the login Keychain.
enum ModelSourceToken {
    private struct Credential: Codable {
        let token: String
        let origin: String
    }

    private static var query: [String: Any] {
        [
            kSecClass as String: kSecClassGenericPassword,
            kSecAttrService as String: "MimiVox model download token",
            kSecAttrAccount as String: "model-mirror",
        ]
    }

    private static func readData() -> Data? {
        var match = query
        match[kSecReturnData as String] = true
        match[kSecMatchLimit as String] = kSecMatchLimitOne
        var item: CFTypeRef?
        guard SecItemCopyMatching(match as CFDictionary, &item) == errSecSuccess,
            let data = item as? Data
        else { return nil }
        return data
    }

    static func read(for source: ModelSource) -> String? {
        guard let origin = source.accessTokenOrigin,
            let data = readData(),
            let credential = try? JSONDecoder().decode(Credential.self, from: data),
            credential.origin == origin
        else { return nil }
        return credential.token
    }

    static func save(_ token: String, for source: ModelSource) {
        guard !token.isEmpty, let origin = source.accessTokenOrigin,
            let data = try? JSONEncoder().encode(Credential(token: token, origin: origin))
        else { return }

        let result = SecItemUpdate(
            query as CFDictionary,
            [kSecValueData as String: data] as CFDictionary
        )
        guard result == errSecItemNotFound else { return }

        var item = query
        item[kSecValueData as String] = data
        SecItemAdd(item as CFDictionary, nil)
    }
}

private final class MirrorProbeRedirectDelegate: NSObject, URLSessionTaskDelegate, @unchecked Sendable {
    private let source: ModelSource
    private let carriesCredential: Bool

    init(source: ModelSource, carriesCredential: Bool) {
        self.source = source
        self.carriesCredential = carriesCredential
    }

    func urlSession(
        _ session: URLSession,
        task: URLSessionTask,
        willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest,
        completionHandler: @escaping (URLRequest?) -> Void
    ) {
        guard carriesCredential else {
            completionHandler(request)
            return
        }
        guard let destination = request.url, source.permitsCredentialedRedirect(to: destination) else {
            completionHandler(nil)
            return
        }
        completionHandler(request)
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

    private var hasTokenForDraft: Bool {
        hasToken && draft?.accessTokenOrigin == source.accessTokenOrigin
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
                prompt: Text(L10n.text(hasTokenForDraft ? "Saved" : "Optional"))
            )
            .onSubmit(apply)
            .disabled(draft?.allowsAccessToken != true)
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
                "Set by your organization. Use an HTTPS mirror to send an access token. HTTP mirrors work without a token."
            )
        case .build:
            L10n.text(
                "This copy of MimiVox was built to download from this mirror. Enter another address to override it. Use HTTPS for an access token."
            )
        case .user, .standard:
            L10n.text(
                "Leave empty for huggingface.co, or enter a Hugging Face mirror. Use HTTPS to send an access token. HTTP mirrors work without a token."
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
        hasToken = ModelSourceToken.read(for: source) != nil
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
        guard let selectedSource = draft else {
            error = L10n.text("Enter an address that starts with https:// or http://.")
            return
        }
        let enteredToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        guard enteredToken.isEmpty || selectedSource.allowsAccessToken else {
            error = L10n.text("An HTTPS address is required to use an access token.")
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
        if !enteredToken.isEmpty { ModelSourceToken.save(enteredToken, for: selectedSource) }
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
        let enteredToken = token.trimmingCharacters(in: .whitespacesAndNewlines)
        let secret = enteredToken.isEmpty ? ModelSourceToken.read(for: draft) : enteredToken
        let carriesCredential = draft.allowsAccessToken && secret?.isEmpty == false
        if carriesCredential, let secret {
            request.setValue("Bearer \(secret)", forHTTPHeaderField: "Authorization")
        }
        let delegate = MirrorProbeRedirectDelegate(source: draft, carriesCredential: carriesCredential)
        let session = URLSession(configuration: .ephemeral, delegate: delegate, delegateQueue: nil)
        defer { session.invalidateAndCancel() }
        do {
            let (_, response) = try await session.data(for: request)
            let code = response.statusCode
            check =
                switch code {
                case 200..<300: .reachable
                case 401, 403: .failed(String(format: L10n.text("Access denied (%@). Check the token."), "\(code)"))
                case 404: .failed(L10n.text("Translation model not found on this mirror (404)."))
                default: .failed(String(format: L10n.text("The mirror answered %@."), "\(code)"))
                }
        } catch {
            check = .failed(error.localizedDescription)
        }
    }
}
