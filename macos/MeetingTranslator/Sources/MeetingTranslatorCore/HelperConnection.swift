import Darwin
import Foundation
import Security

struct HelperConnection {
    private struct LaunchCredentials: Encodable {
        let token: String
        let pem: Data
    }

    private let credentials: LaunchCredentials
    private let certificate: Data

    init() throws {
        var random = [UInt8](repeating: 0, count: 32)
        guard SecRandomCopyBytes(kSecRandomDefault, random.count, &random) == errSecSuccess else {
            throw CocoaError(.fileWriteUnknown)
        }
        let output = Pipe()
        let openssl = Process()
        openssl.executableURL = URL(fileURLWithPath: "/usr/bin/openssl")
        openssl.arguments = [
            "req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1",
            "-pkeyopt", "ec_param_enc:named_curve", "-nodes", "-days", "365", "-subj", "/CN=127.0.0.1",
            "-addext", "subjectAltName=IP:127.0.0.1", "-keyout", "/dev/stdout", "-out", "/dev/stdout",
        ]
        openssl.standardInput = FileHandle.nullDevice
        openssl.standardOutput = output
        openssl.standardError = FileHandle.nullDevice
        try openssl.run()
        let pem = output.fileHandleForReading.readDataToEndOfFile()
        openssl.waitUntilExit()

        guard openssl.terminationStatus == 0,
            let text = String(data: pem, encoding: .utf8),
            let start = text.range(of: "-----BEGIN CERTIFICATE-----"),
            let end = text.range(of: "-----END CERTIFICATE-----", range: start.upperBound..<text.endIndex),
            let der = Data(
                base64Encoded: text[start.upperBound..<end.lowerBound].split(whereSeparator: \.isNewline).joined())
        else { throw CocoaError(.fileReadCorruptFile) }

        credentials = LaunchCredentials(token: Data(random).base64EncodedString(), pem: pem)
        certificate = der
    }

    func writeCredentials(to pipe: FileHandle) throws {
        defer { try? pipe.close() }
        guard fcntl(pipe.fileDescriptor, F_SETNOSIGPIPE, 1) != -1 else {
            throw POSIXError(POSIXErrorCode(rawValue: errno) ?? .EIO)
        }
        try pipe.write(contentsOf: JSONEncoder().encode(credentials))
    }

    func client(baseURL: URL) -> MeetingTranslatorAPIClient {
        let session = URLSession(
            configuration: .ephemeral, delegate: HelperServerTrust(certificate: certificate), delegateQueue: nil)
        return MeetingTranslatorAPIClient(
            baseURL: baseURL, transport: session, bearerToken: credentials.token)
    }
}

private final class HelperServerTrust: NSObject, URLSessionTaskDelegate {
    private let certificate: Data

    init(certificate: Data) {
        self.certificate = certificate
    }

    func urlSession(
        _ session: URLSession, didReceive challenge: URLAuthenticationChallenge
    ) async -> (URLSession.AuthChallengeDisposition, URLCredential?) {
        guard challenge.protectionSpace.authenticationMethod == NSURLAuthenticationMethodServerTrust,
            let trust = challenge.protectionSpace.serverTrust,
            let chain = SecTrustCopyCertificateChain(trust) as? [SecCertificate],
            let leaf = chain.first,
            SecCertificateCopyData(leaf) as Data == certificate
        else { return (.cancelAuthenticationChallenge, nil) }
        return (.useCredential, URLCredential(trust: trust))
    }

    func urlSession(
        _ session: URLSession, task: URLSessionTask, willPerformHTTPRedirection response: HTTPURLResponse,
        newRequest request: URLRequest
    ) async -> URLRequest? {
        nil
    }
}
