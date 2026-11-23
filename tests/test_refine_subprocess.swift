import Foundation

@main
struct RefineSubprocessTests {
    static var passed = 0
    static var failed = 0

    static func expect(_ condition: @autoclosure () -> Bool, _ label: String) {
        if condition() {
            passed += 1
            print("PASS: \(label)")
        } else {
            failed += 1
            print("FAIL: \(label)")
        }
    }

    static func pythonResult(script: String,
                             input: Data = Data(),
                             timeout: TimeInterval = 5,
                             killGrace: TimeInterval = 0.2,
                             stdoutLimit: Int = 8 * 1024 * 1024,
                             stderrLimit: Int = 1024 * 1024) throws -> RefineSubprocessOutput {
        return try runRefineSubprocess(
            executableURL: URL(fileURLWithPath: "/usr/bin/env"),
            arguments: ["python3", "-c", script],
            environment: ProcessInfo.processInfo.environment,
            input: input,
            timeout: timeout,
            killGrace: killGrace,
            stdoutLimit: stdoutLimit,
            stderrLimit: stderrLimit
        )
    }

    static func testBidirectionalPipePressure() {
        let script = #"""
import os, sys, threading
def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
def write_stdout():
    write_all(1, b' ' * 1200000)
def write_stderr():
    write_all(2, b'E' * 1200000)
threads = [threading.Thread(target=write_stdout), threading.Thread(target=write_stderr)]
for thread in threads: thread.start()
for thread in threads: thread.join()
sys.stdin.buffer.read()
write_all(1, b'{"result":"<section><h1>ok</h1></section>"}')
"""#
        let started = Date()
        do {
            let result = try pythonResult(
                script: script,
                input: Data(repeating: 80, count: 1_200_000)
            )
            expect(Date().timeIntervalSince(started) < 5, "large stdin/stdout/stderr completes promptly")
            expect(result.terminationStatus == 0, "saturation child exits successfully")
            expect(result.stdout.count > 1_000_000, "stdout drains beyond pipe capacity")
            expect(result.stderr.count == 1024 * 1024, "stderr capture is bounded")
            expect(result.stderrTruncated, "stderr overflow is reported as truncated")
            let object = try JSONSerialization.jsonObject(with: result.stdout) as? [String: Any]
            let refined = object?["result"] as? String
            expect(refined == "<section><h1>ok</h1></section>", "large captured stdout preserves refine JSON")
        } catch {
            expect(false, "bidirectional pressure unexpectedly threw: \(error)")
        }
    }

    static func testNonzeroExitStillDrains() {
        let script = #"""
import os, threading
def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
threads = [threading.Thread(target=write_all, args=(1, b'O' * 300000)),
           threading.Thread(target=write_all, args=(2, b'problem:' + b'E' * 300000))]
for thread in threads: thread.start()
for thread in threads: thread.join()
raise SystemExit(7)
"""#
        let started = Date()
        do {
            let result = try pythonResult(script: script)
            expect(Date().timeIntervalSince(started) < 5, "nonzero child drains promptly")
            expect(result.terminationStatus == 7, "nonzero termination status is preserved")
            expect(result.stdout.count == 300000, "nonzero child stdout is captured")
            expect(String(decoding: result.stderr.prefix(8), as: UTF8.self) == "problem:",
                   "nonzero child stderr diagnostics are captured")
        } catch {
            expect(false, "nonzero drain unexpectedly threw: \(error)")
        }
    }

    static func testForcedKillAfterTimeout() {
        let script = #"""
import os, signal, threading, time
signal.signal(signal.SIGTERM, signal.SIG_IGN)
def write_all(fd, data):
    view = memoryview(data)
    while view:
        view = view[os.write(fd, view):]
threads = [threading.Thread(target=write_all, args=(1, b'O' * 1200000)),
           threading.Thread(target=write_all, args=(2, b'E' * 1200000))]
for thread in threads: thread.start()
for thread in threads: thread.join()
while True: time.sleep(1)
"""#
        let started = Date()
        do {
            _ = try pythonResult(script: script, timeout: 0.5, killGrace: 0.2)
            expect(false, "timeout child should throw")
        } catch let error as NSError {
            let elapsed = Date().timeIntervalSince(started)
            expect(error.domain == "Presto.refine" && error.code == 11,
                   "timeout preserves Presto.refine code 11")
            expect(elapsed >= 0.6, "timeout exercises SIGTERM grace")
            expect(elapsed < 4, "SIGKILL reaps the blocked child promptly")
        }
    }

    static func testStdoutLimitStillDrains() {
        let script = "import os; os.write(1, b'X' * 400000)"
        do {
            _ = try pythonResult(script: script, stdoutLimit: 128 * 1024)
            expect(false, "oversized stdout should throw")
        } catch let error as NSError {
            expect(error.domain == "Presto.refine" && error.code == 12,
                   "oversized stdout fails with bounded-output error")
        }
    }

    static func main() {
        testBidirectionalPipePressure()
        testNonzeroExitStillDrains()
        testForcedKillAfterTimeout()
        testStdoutLimitStillDrains()
        print("RESULT: \(passed) passed, \(failed) failed")
        if failed > 0 { exit(1) }
    }
}
