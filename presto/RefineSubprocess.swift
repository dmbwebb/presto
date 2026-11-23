import Darwin
import Foundation

struct RefineSubprocessOutput {
    let stdout: Data
    let stderr: Data
    let terminationStatus: Int32
    let stderrTruncated: Bool
}

private final class BoundedPipeCapture: @unchecked Sendable {
    private let limit: Int
    private let lock = NSLock()
    private var storage = Data()
    private var didOverflow = false
    private var readFailure: String?

    init(limit: Int) {
        self.limit = max(0, limit)
    }

    func drain(_ handle: FileHandle) {
        do {
            while let chunk = try handle.read(upToCount: 64 * 1024), !chunk.isEmpty {
                lock.lock()
                let remaining = max(0, limit - storage.count)
                if remaining > 0 {
                    storage.append(contentsOf: chunk.prefix(remaining))
                }
                if chunk.count > remaining {
                    didOverflow = true
                }
                lock.unlock()
            }
        } catch {
            lock.lock()
            readFailure = error.localizedDescription
            lock.unlock()
        }
        try? handle.close()
    }

    func snapshot() -> (data: Data, overflowed: Bool, failure: String?) {
        lock.lock()
        defer { lock.unlock() }
        return (storage, didOverflow, readFailure)
    }
}

private final class RefineInputWriter: @unchecked Sendable {
    private let lock = NSLock()
    private var writeFailure: String?

    func write(_ data: Data, to handle: FileHandle) {
        do {
            try handle.write(contentsOf: data)
        } catch {
            lock.lock()
            writeFailure = error.localizedDescription
            lock.unlock()
        }
        try? handle.close()
    }

    func failureDescription() -> String? {
        lock.lock()
        defer { lock.unlock() }
        return writeFailure
    }
}

private func waitForRefineProcess(_ process: Process,
                                  timeout: TimeInterval,
                                  killGrace: TimeInterval) throws {
    if !process.isRunning { return }

    let semaphore = DispatchSemaphore(value: 0)
    process.terminationHandler = { _ in semaphore.signal() }
    if !process.isRunning {
        process.terminationHandler = nil
        return
    }

    if semaphore.wait(timeout: .now() + timeout) == .success {
        process.terminationHandler = nil
        return
    }

    NSLog("Presto: refine subprocess timed out, terminating")
    if process.isRunning {
        process.terminate()
    }

    if semaphore.wait(timeout: .now() + killGrace) == .timedOut,
       process.isRunning {
        NSLog("Presto: refine subprocess ignored terminate, killing")
        kill(process.processIdentifier, SIGKILL)
        if semaphore.wait(timeout: .now() + 2) == .timedOut,
           process.isRunning {
            process.terminationHandler = nil
            throw NSError(domain: "Presto.refine", code: 15,
                          userInfo: [NSLocalizedDescriptionKey: "claude could not be reaped after SIGKILL"])
        }
    }
    process.terminationHandler = nil

    throw NSError(domain: "Presto.refine", code: 11,
                  userInfo: [NSLocalizedDescriptionKey: "claude timed out"])
}

func runRefineSubprocess(executableURL: URL,
                         arguments: [String],
                         environment: [String: String]?,
                         input: Data,
                         timeout: TimeInterval,
                         killGrace: TimeInterval = 5,
                         stdoutLimit: Int = 8 * 1024 * 1024,
                         stderrLimit: Int = 1024 * 1024) throws -> RefineSubprocessOutput {
    let process = Process()
    process.executableURL = executableURL
    process.arguments = arguments
    process.environment = environment

    let stdinPipe = Pipe()
    let stdoutPipe = Pipe()
    let stderrPipe = Pipe()
    process.standardInput = stdinPipe
    process.standardOutput = stdoutPipe
    process.standardError = stderrPipe

    try process.run()

    let stdoutCapture = BoundedPipeCapture(limit: stdoutLimit)
    let stderrCapture = BoundedPipeCapture(limit: stderrLimit)
    let inputWriter = RefineInputWriter()
    let ioGroup = DispatchGroup()
    let ioQueue = DispatchQueue(label: "com.webslides.presto.refine-io", attributes: .concurrent)

    ioGroup.enter()
    ioQueue.async {
        defer { ioGroup.leave() }
        stdoutCapture.drain(stdoutPipe.fileHandleForReading)
    }
    ioGroup.enter()
    ioQueue.async {
        defer { ioGroup.leave() }
        stderrCapture.drain(stderrPipe.fileHandleForReading)
    }
    ioGroup.enter()
    ioQueue.async {
        defer { ioGroup.leave() }
        inputWriter.write(input, to: stdinPipe.fileHandleForWriting)
    }

    var processError: Error?
    do {
        try waitForRefineProcess(process, timeout: timeout, killGrace: killGrace)
    } catch {
        processError = error
    }

    if ioGroup.wait(timeout: .now() + 2) == .timedOut {
        try? stdoutPipe.fileHandleForReading.close()
        try? stderrPipe.fileHandleForReading.close()
        try? stdinPipe.fileHandleForWriting.close()
        if let processError { throw processError }
        throw NSError(domain: "Presto.refine", code: 13,
                      userInfo: [NSLocalizedDescriptionKey: "claude pipe drain timed out"])
    }
    if let processError { throw processError }

    let stdout = stdoutCapture.snapshot()
    let stderr = stderrCapture.snapshot()
    if let failure = stdout.failure ?? stderr.failure {
        throw NSError(domain: "Presto.refine", code: 14,
                      userInfo: [NSLocalizedDescriptionKey: "claude pipe read failed: \(failure)"])
    }
    if stdout.overflowed {
        throw NSError(domain: "Presto.refine", code: 12,
                      userInfo: [NSLocalizedDescriptionKey: "claude output exceeded \(stdoutLimit) bytes"])
    }
    if process.terminationStatus == 0, let failure = inputWriter.failureDescription() {
        throw NSError(domain: "Presto.refine", code: 14,
                      userInfo: [NSLocalizedDescriptionKey: "failed to send refine prompt: \(failure)"])
    }

    return RefineSubprocessOutput(stdout: stdout.data,
                                  stderr: stderr.data,
                                  terminationStatus: process.terminationStatus,
                                  stderrTruncated: stderr.overflowed)
}
