import Cocoa
import Darwin
import PDFKit

private func expect(_ condition: @autoclosure () -> Bool, _ message: String) {
    guard condition() else {
        fputs("FAIL: \(message)\n", stderr)
        exit(1)
    }
}

private func makeFourCornerSnapshot() -> NSImage {
    let pixelWidth = 512
    let pixelHeight = 288
    guard let bitmap = NSBitmapImageRep(
        bitmapDataPlanes: nil,
        pixelsWide: pixelWidth,
        pixelsHigh: pixelHeight,
        bitsPerSample: 8,
        samplesPerPixel: 4,
        hasAlpha: true,
        isPlanar: false,
        colorSpaceName: .deviceRGB,
        bytesPerRow: 0,
        bitsPerPixel: 0
    ), let context = NSGraphicsContext(bitmapImageRep: bitmap) else {
        fatalError("Unable to create PDF snapshot fixture")
    }

    NSGraphicsContext.saveGraphicsState()
    NSGraphicsContext.current = context
    NSColor.red.setFill()
    NSBezierPath(rect: NSRect(x: 0, y: 144, width: 256, height: 144)).fill()
    NSColor.green.setFill()
    NSBezierPath(rect: NSRect(x: 256, y: 144, width: 256, height: 144)).fill()
    NSColor.blue.setFill()
    NSBezierPath(rect: NSRect(x: 0, y: 0, width: 256, height: 144)).fill()
    NSColor.yellow.setFill()
    NSBezierPath(rect: NSRect(x: 256, y: 0, width: 256, height: 144)).fill()
    context.flushGraphics()
    NSGraphicsContext.restoreGraphicsState()

    // Match the live failure: WebKit reports a high-resolution snapshot with
    // logical dimensions larger than the 1280x720 slide frame.
    let image = NSImage(size: NSSize(width: 2560, height: 1440))
    image.addRepresentation(bitmap)
    return image
}

private func colorName(_ color: NSColor?) -> String {
    guard let rgb = color?.usingColorSpace(.deviceRGB) else { return "missing" }
    let r = rgb.redComponent
    let g = rgb.greenComponent
    let b = rgb.blueComponent
    if r > 0.7, g > 0.7, b < 0.55 { return "yellow" }
    if r > 0.65, g < 0.5, b < 0.5 { return "red" }
    if g > 0.65, r < 0.65, b < 0.65 { return "green" }
    if b > 0.65, r < 0.5, g < 0.5 { return "blue" }
    return "other"
}

private func colorDescription(_ color: NSColor?) -> String {
    guard let rgb = color?.usingColorSpace(.deviceRGB) else { return "missing" }
    return String(
        format: "(%.2f,%.2f,%.2f,%.2f)",
        rgb.redComponent,
        rgb.greenComponent,
        rgb.blueComponent,
        rgb.alphaComponent
    )
}

@main
struct PDFSnapshotGeometryTests {
    static func main() {
        _ = NSApplication.shared
        let snapshot = makeFourCornerSnapshot()
        let sourceRep = snapshot.representations.first as? NSBitmapImageRep
        expect(snapshot.size == NSSize(width: 2560, height: 1440),
               "fixture must start with oversized logical dimensions")
        expect(sourceRep?.pixelsWide == 512 && sourceRep?.pixelsHigh == 288,
               "fixture must carry independent high-DPI pixel dimensions")
        let sourceSamples = [
            sourceRep?.colorAt(x: 128, y: 72),
            sourceRep?.colorAt(x: 384, y: 72),
            sourceRep?.colorAt(x: 128, y: 216),
            sourceRep?.colorAt(x: 384, y: 216),
        ]
        expect(Set(sourceSamples.map(colorName)) == Set(["red", "green", "blue", "yellow"]),
               "fixture must contain four colored corners; got \(sourceSamples.map(colorDescription))")

        let normalized = normalizedPDFSnapshotImage(snapshot)
        var normalizedRect = NSRect(origin: .zero, size: normalized.size)
        let normalizedPixels = normalized.cgImage(
            forProposedRect: &normalizedRect,
            context: nil,
            hints: nil
        )
        expect(snapshot.size == NSSize(width: 2560, height: 1440),
               "normalization must not mutate the WebKit snapshot")
        expect(normalized.size == prestoPDFPageSize,
               "normalized image must use the 1280x720 slide frame")
        expect(normalizedPixels?.width == 512 && normalizedPixels?.height == 288,
               "normalization must preserve source bitmap pixels")

        guard let page = makePDFPage(fromSnapshot: snapshot) else {
            fputs("FAIL: normalized snapshot did not create a PDF page\n", stderr)
            exit(1)
        }
        expect(page.bounds(for: .mediaBox) == NSRect(origin: .zero, size: prestoPDFPageSize),
               "PDF media box must be exactly 1280x720")

        let document = PDFDocument()
        document.insert(page, at: 0)
        guard let data = document.dataRepresentation(),
              let reloaded = PDFDocument(data: data),
              let reloadedPage = reloaded.page(at: 0) else {
            fputs("FAIL: normalized PDF page did not round-trip\n", stderr)
            exit(1)
        }
        if ProcessInfo.processInfo.environment["PRESTO_PDF_SNAPSHOT_DEBUG"] == "1" {
            try? data.write(to: URL(fileURLWithPath: "/tmp/presto-pdf-snapshot-debug.pdf"))
        }
        let rendered = reloadedPage.thumbnail(of: prestoPDFPageSize, for: .mediaBox)
        guard let renderedData = rendered.tiffRepresentation,
              let renderedRep = NSBitmapImageRep(data: renderedData) else {
            fputs("FAIL: normalized PDF page did not render\n", stderr)
            exit(1)
        }

        let w = renderedRep.pixelsWide
        let h = renderedRep.pixelsHigh
        let samples = [
            renderedRep.colorAt(x: w / 4, y: h / 4),
            renderedRep.colorAt(x: 3 * w / 4, y: h / 4),
            renderedRep.colorAt(x: w / 4, y: 3 * h / 4),
            renderedRep.colorAt(x: 3 * w / 4, y: 3 * h / 4),
        ]
        let sampleNames = samples.map(colorName)
        expect(Set(sampleNames) == Set(["red", "green", "blue", "yellow"]),
               "rendered PDF must preserve all four snapshot corners without cropping; got \(sampleNames) \(samples.map(colorDescription))")

        print("PDF_SNAPSHOT_GEOMETRY: passed")
    }
}
