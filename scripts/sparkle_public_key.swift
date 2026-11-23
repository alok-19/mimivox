import CryptoKit
import Darwin
import Foundation

guard let encodedPrivateKey = ProcessInfo.processInfo.environment["SPARKLE_PRIVATE_KEY"] else {
    fputs("SPARKLE_PRIVATE_KEY is not set\n", stderr)
    exit(EXIT_FAILURE)
}

guard let privateSeed = Data(
    base64Encoded: encodedPrivateKey.trimmingCharacters(in: .whitespacesAndNewlines)
), privateSeed.count == 32 else {
    fputs("SPARKLE_PRIVATE_KEY must be a base64-encoded 32-byte EdDSA seed\n", stderr)
    exit(EXIT_FAILURE)
}

do {
    let privateKey = try Curve25519.Signing.PrivateKey(rawRepresentation: privateSeed)
    print(privateKey.publicKey.rawRepresentation.base64EncodedString())
} catch {
    fputs("SPARKLE_PRIVATE_KEY is not a valid EdDSA seed\n", stderr)
    exit(EXIT_FAILURE)
}
