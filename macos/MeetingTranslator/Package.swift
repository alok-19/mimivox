// swift-tools-version: 6.0

import PackageDescription

let package = Package(
    name: "MeetingTranslator",
    defaultLocalization: "en",
    platforms: [
        .macOS(.v14)
    ],
    products: [
        .library(
            name: "MeetingTranslatorCore",
            targets: ["MeetingTranslatorCore"]
        ),
        .executable(
            name: "MeetingTranslator",
            targets: ["MeetingTranslatorApp"]
        ),
        .executable(
            name: "MeetingTranslatorCoreChecks",
            targets: ["MeetingTranslatorCoreChecks"]
        ),
    ],
    targets: [
        .target(
            name: "MeetingTranslatorCore",
            resources: [.process("Resources")]
        ),
        .executableTarget(
            name: "MeetingTranslatorApp",
            dependencies: ["MeetingTranslatorCore"],
            resources: [.copy("Resources")]
        ),
        .executableTarget(
            name: "MeetingTranslatorCoreChecks",
            dependencies: ["MeetingTranslatorCore"]
        ),
        .testTarget(
            name: "MeetingTranslatorCoreTests",
            dependencies: ["MeetingTranslatorCore"]
        ),
    ]
)
