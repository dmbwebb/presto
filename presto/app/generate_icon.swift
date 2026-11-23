#!/usr/bin/env swift
// Generate a macOS .icns icon for Presto: a light tile with a white slide and
// a blue play button (the accent #2F6BD8 from presto/ui-tokens.css), so it
// sits with Portal and Clear in the Dock.

import AppKit
import Darwin
import Foundation

let scriptDir = URL(fileURLWithPath: CommandLine.arguments[0]).deletingLastPathComponent()
let iconsetDir = scriptDir.appendingPathComponent("AppIcon.iconset")

func fail(_ message: String) -> Never {
    FileHandle.standardError.write(Data((message + "\n").utf8))
    exit(1)
}

func trashPath(_ url: URL) throws {
    guard FileManager.default.fileExists(atPath: url.path) else { return }

    let process = Process()
    process.executableURL = URL(fileURLWithPath: "/usr/bin/trash")
    process.arguments = [url.path]
    try process.run()
    process.waitUntilExit()

    if process.terminationStatus != 0 {
        throw NSError(
            domain: "PrestoIconGenerator",
            code: Int(process.terminationStatus),
            userInfo: [NSLocalizedDescriptionKey: "Failed to move \(url.path) to Trash"]
        )
    }
}

func temporarySibling(for url: URL) -> URL {
    let directory = url.deletingLastPathComponent()
    return directory.appendingPathComponent(".\(url.lastPathComponent).tmp-\(UUID().uuidString)")
}

func temporaryIconSibling(for url: URL) -> URL {
    let directory = url.deletingLastPathComponent()
    return directory.appendingPathComponent(".\(url.deletingPathExtension().lastPathComponent).tmp-\(UUID().uuidString).icns")
}

func publishFileAtomically(tempURL: URL, finalURL: URL) throws {
    let fileManager = FileManager.default
    if fileManager.fileExists(atPath: finalURL.path) {
        _ = try fileManager.replaceItemAt(finalURL,
                                          withItemAt: tempURL,
                                          backupItemName: nil,
                                          options: [])
    } else {
        try fileManager.moveItem(at: tempURL, to: finalURL)
    }
}

func writeDataAtomically(_ data: Data, to finalURL: URL) throws {
    let tempURL = temporarySibling(for: finalURL)
    do {
        try data.write(to: tempURL, options: .atomic)
        try publishFileAtomically(tempURL: tempURL, finalURL: finalURL)
    } catch {
        try? trashPath(tempURL)
        throw error
    }
}

// Create iconset directory
do {
    try trashPath(iconsetDir)
} catch {
    fail("Failed to clean existing iconset: \(error.localizedDescription)")
}
do {
    try FileManager.default.createDirectory(at: iconsetDir, withIntermediateDirectories: true)
} catch {
    fail("Failed to create iconset directory: \(error.localizedDescription)")
}

let sizes: [(Int, String)] = [
    (16, "icon_16x16.png"),
    (32, "icon_16x16@2x.png"),
    (32, "icon_32x32.png"),
    (64, "icon_32x32@2x.png"),
    (128, "icon_128x128.png"),
    (256, "icon_128x128@2x.png"),
    (256, "icon_256x256.png"),
    (512, "icon_256x256@2x.png"),
    (512, "icon_512x512.png"),
    (1024, "icon_512x512@2x.png"),
]

func color(_ hex: UInt32, _ alpha: CGFloat = 1) -> NSColor {
    NSColor(srgbRed: CGFloat((hex >> 16) & 0xFF) / 255, green: CGFloat((hex >> 8) & 0xFF) / 255,
            blue: CGFloat(hex & 0xFF) / 255, alpha: alpha)
}

// Drawn on a 1024 grid and scaled, following the macOS tile proportions.
func drawIcon(size: Int) {
    let scale = CGFloat(size) / 1024
    let transform = NSAffineTransform()
    transform.scale(by: scale)
    transform.concat()

    // Light tile with a soft shadow.
    let tile = NSRect(x: 100, y: 100, width: 824, height: 824)
    let tilePath = NSBezierPath(roundedRect: tile, xRadius: 185, yRadius: 185)
    NSGraphicsContext.saveGraphicsState()
    let shadow = NSShadow()
    shadow.shadowColor = color(0x101828, 0.22)
    shadow.shadowBlurRadius = 28
    shadow.shadowOffset = NSSize(width: 0, height: -12)
    shadow.set()
    color(0xFFFFFF).setFill()
    tilePath.fill()
    NSGraphicsContext.restoreGraphicsState()
    NSGradient(starting: color(0xFFFFFF), ending: color(0xE7EAEE))?.draw(in: tilePath, angle: -90)
    color(0x000000, 0.08).setStroke()
    tilePath.lineWidth = 3
    tilePath.stroke()

    // A 16:9 slide: white, hairline edge, a heading bar and two text lines.
    let slide = NSRect(x: 200, y: 400, width: 624, height: 351)
    let slidePath = NSBezierPath(roundedRect: slide, xRadius: 26, yRadius: 26)
    NSGraphicsContext.saveGraphicsState()
    let slideShadow = NSShadow()
    slideShadow.shadowColor = color(0x101828, 0.16)
    slideShadow.shadowBlurRadius = 22
    slideShadow.shadowOffset = NSSize(width: 0, height: -8)
    slideShadow.set()
    color(0xFFFFFF).setFill()
    slidePath.fill()
    NSGraphicsContext.restoreGraphicsState()
    color(0xC8CCD3).setStroke()
    slidePath.lineWidth = 4
    slidePath.stroke()
    color(0x1C2532).setFill()
    NSBezierPath(roundedRect: NSRect(x: 252, y: 640, width: 300, height: 44), xRadius: 14, yRadius: 14).fill()
    color(0xA9AFB8).setFill()
    NSBezierPath(roundedRect: NSRect(x: 252, y: 560, width: 420, height: 26), xRadius: 13, yRadius: 13).fill()
    NSBezierPath(roundedRect: NSRect(x: 252, y: 508, width: 330, height: 26), xRadius: 13, yRadius: 13).fill()

    // The accent, once: a play button over the slide's corner.
    let button = NSRect(x: 596, y: 210, width: 250, height: 250)
    NSGraphicsContext.saveGraphicsState()
    let buttonShadow = NSShadow()
    buttonShadow.shadowColor = color(0x2F6BD8, 0.35)
    buttonShadow.shadowBlurRadius = 24
    buttonShadow.shadowOffset = NSSize(width: 0, height: -8)
    buttonShadow.set()
    color(0x2F6BD8).setFill()
    NSBezierPath(ovalIn: button).fill()
    NSGraphicsContext.restoreGraphicsState()
    let triangle = NSBezierPath()
    triangle.move(to: NSPoint(x: button.minX + 98, y: button.minY + 68))
    triangle.line(to: NSPoint(x: button.minX + 98, y: button.maxY - 68))
    triangle.line(to: NSPoint(x: button.maxX - 62, y: button.midY))
    triangle.close()
    triangle.lineJoinStyle = .round
    triangle.lineWidth = 18
    color(0xFFFFFF).setFill()
    color(0xFFFFFF).setStroke()
    triangle.fill()
    triangle.stroke()
}

for (size, filename) in sizes {
    let cgSize = CGSize(width: size, height: size)
    let image = NSImage(size: cgSize)
    image.lockFocus()
    drawIcon(size: size)
    image.unlockFocus()

    // Save as PNG
    guard let tiffData = image.tiffRepresentation,
          let bitmap = NSBitmapImageRep(data: tiffData),
          let pngData = bitmap.representation(using: .png, properties: [:])
    else {
        fail("Failed to render icon PNG \(filename)")
    }

    let outPath = iconsetDir.appendingPathComponent(filename)
    do {
        try writeDataAtomically(pngData, to: outPath)
    } catch {
        fail("Failed to write icon PNG \(filename): \(error.localizedDescription)")
    }
}

// Convert to icns using iconutil
let icnsPath = scriptDir.appendingPathComponent("Presto.app/Contents/Resources/AppIcon.icns")
let tempIcnsPath = temporaryIconSibling(for: icnsPath)
let process = Process()
process.executableURL = URL(fileURLWithPath: "/usr/bin/iconutil")
process.arguments = ["-c", "icns", iconsetDir.path, "-o", tempIcnsPath.path]
do {
    try process.run()
} catch {
    try? trashPath(tempIcnsPath)
    fail("Failed to launch iconutil: \(error.localizedDescription)")
}
process.waitUntilExit()

if process.terminationStatus == 0 {
    do {
        try publishFileAtomically(tempURL: tempIcnsPath, finalURL: icnsPath)
    } catch {
        try? trashPath(tempIcnsPath)
        fail("Failed to publish icon: \(error.localizedDescription)")
    }
    print("Created \(icnsPath.path)")
    do {
        try trashPath(iconsetDir)
    } catch {
        fail("Created icon but failed to clean iconset: \(error.localizedDescription)")
    }
} else {
    try? trashPath(tempIcnsPath)
    let message = "iconutil failed with status \(process.terminationStatus)\n"
    FileHandle.standardError.write(Data(message.utf8))
    exit(1)
}
