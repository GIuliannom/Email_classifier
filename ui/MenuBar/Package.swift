// swift-tools-version:5.9
import PackageDescription

let package = Package(
    name: "EmailAI",
    platforms: [.macOS(.v14)],  // MenuBarExtra, inspector
    targets: [.executableTarget(name: "EmailAI", path: "Sources/EmailAI")]
)
