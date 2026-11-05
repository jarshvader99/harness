// swift-tools-version: 6.0
import PackageDescription

let package = Package(
    name: "TurboHarness",
    platforms: [.macOS(.v14)],
    targets: [
        .executableTarget(
            name: "TurboHarness",
            path: "Sources/TurboHarness",
            swiftSettings: [.swiftLanguageMode(.v5)]
        )
    ]
)
