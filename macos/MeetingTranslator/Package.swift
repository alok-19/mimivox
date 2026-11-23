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
    dependencies: [
        .package(url: "https://github.com/sparkle-project/Sparkle", exact: "2.10.1")
    ],
    targets: [
        .target(
            name: "MeetingTranslatorCore",
            resources: [.process("Resources")]
        ),
        .executableTarget(
            name: "MeetingTranslatorApp",
            dependencies: [
                "MeetingTranslatorCore",
                .product(name: "Sparkle", package: "sparkle"),
            ],
            resources: [.copy("Resources")],
            linkerSettings: [
                .unsafeFlags([
                    "-Xlinker", "-rpath", "-Xlinker", "@executable_path",
                    "-Xlinker", "-rpath", "-Xlinker", "@executable_path/../Frameworks",
                ])
            ]
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
