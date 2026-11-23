import Cocoa
import PDFKit

let prestoPDFPageSize = NSSize(width: 1280, height: 720)

func normalizedPDFSnapshotImage(_ snapshot: NSImage) -> NSImage {
    var proposedRect = NSRect(origin: .zero, size: snapshot.size)
    if let pixels = snapshot.cgImage(
        forProposedRect: &proposedRect,
        context: nil,
        hints: nil
    ) {
        return NSImage(cgImage: pixels, size: prestoPDFPageSize)
    }
    let normalized = snapshot.copy() as! NSImage
    normalized.size = prestoPDFPageSize
    return normalized
}

func makePDFPage(fromSnapshot snapshot: NSImage) -> PDFPage? {
    let normalized = normalizedPDFSnapshotImage(snapshot)
    var sourceRect = NSRect(origin: .zero, size: normalized.size)
    guard let pixels = normalized.cgImage(
        forProposedRect: &sourceRect,
        context: nil,
        hints: nil
    ) else {
        return nil
    }

    var mediaBox = CGRect(origin: .zero, size: prestoPDFPageSize)
    let pdfData = NSMutableData()
    guard let consumer = CGDataConsumer(data: pdfData as CFMutableData),
          let context = CGContext(
              consumer: consumer,
              mediaBox: &mediaBox,
              nil
          ) else {
        return nil
    }

    context.beginPDFPage(nil)
    context.interpolationQuality = .high
    context.draw(pixels, in: mediaBox)
    context.endPDFPage()
    context.closePDF()

    return PDFDocument(data: pdfData as Data)?.page(at: 0)
}
