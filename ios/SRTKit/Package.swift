// swift-tools-version: 5.9
// SRTKit: 提供 libsrt v1.5.4 二进制 xcframework 给 PhoneCam iOS 应用。
//
// 仅包含一个 binary target `libsrt`，应用通过 `import libsrt` 直接调用 C API。
// 这样保持依赖最小，避免引入额外 Swift 封装层（MPEG-TS 耦合等）。
// 实际 SRT 业务逻辑封装在 ios/PhoneCam/SRTStreamServer.swift。

import PackageDescription

let package = Package(
    name: "SRTKit",
    products: [
        .library(name: "libsrt", targets: ["libsrt"]),
    ],
    targets: [
        .binaryTarget(
            name: "libsrt",
            url: "https://github.com/HaishinKit/libsrt-xcframework/releases/download/v1.5.4/libsrt.xcframework.zip",
            checksum: "76879e2802e45ce043f52871a0a6764d57f833bdb729f2ba6663f4e31d658c4a"
        ),
    ]
)
