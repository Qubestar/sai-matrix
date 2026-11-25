// Sai Matrix launcher: starts the bundled Python server (if not already running),
// opens the UI in the browser, and keeps a menu-bar item for quitting cleanly.
import Cocoa
import Foundation

let port: UInt16 = ProcessInfo.processInfo.environment["SAI_MATRIX_PORT"].flatMap(UInt16.init) ?? 8765
let appURL = URL(string: "http://127.0.0.1:\(port)")!
let stateURL = appURL.appendingPathComponent("api/state")

func portIsOpen() -> Bool {
    let fd = socket(AF_INET, SOCK_STREAM, 0)
    if fd < 0 { return false }
    defer { close(fd) }
    var addr = sockaddr_in()
    addr.sin_family = sa_family_t(AF_INET)
    addr.sin_port = port.bigEndian
    addr.sin_addr.s_addr = inet_addr("127.0.0.1")
    let r = withUnsafePointer(to: &addr) { p in
        p.withMemoryRebound(to: sockaddr.self, capacity: 1) {
            connect(fd, $0, socklen_t(MemoryLayout<sockaddr_in>.size))
        }
    }
    return r == 0
}

func httpReady() -> Bool {
    var req = URLRequest(url: stateURL)
    req.timeoutInterval = 1.0
    let sem = DispatchSemaphore(value: 0)
    var ok = false
    URLSession.shared.dataTask(with: req) { _, resp, _ in
        if let h = resp as? HTTPURLResponse, h.statusCode == 200 { ok = true }
        sem.signal()
    }.resume()
    _ = sem.wait(timeout: .now() + 2)
    return ok
}

final class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem?
    var child: Process?
    var sigSource: DispatchSourceSignal?
    var quitting = false

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.regular)
        setupStatusItem()
        installSignalHandler()
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            self?.launchFlow()
        }
    }

    func setupStatusItem() {
        let item = NSStatusBar.system.statusItem(withLength: NSStatusItem.squareLength)
        if let button = item.button {
            button.image = NSImage(systemSymbolName: "square.grid.2x2", accessibilityDescription: "Sai Matrix")
        }
        let menu = NSMenu()
        let open = NSMenuItem(title: "Open Sai Matrix", action: #selector(openUI), keyEquivalent: "o")
        open.target = self
        menu.addItem(open)
        menu.addItem(.separator())
        let quit = NSMenuItem(title: "Quit Sai Matrix", action: #selector(quit), keyEquivalent: "q")
        quit.target = self
        menu.addItem(quit)
        item.menu = menu
        statusItem = item
    }

    func installSignalHandler() {
        signal(SIGTERM, SIG_IGN)
        let src = DispatchSource.makeSignalSource(signal: SIGTERM, queue: .main)
        src.setEventHandler { [weak self] in
            self?.stopServer()
            exit(0)
        }
        src.resume()
        sigSource = src
    }

    func dataDir() -> URL {
        let home = ProcessInfo.processInfo.environment["HOME"] ?? NSHomeDirectory()
        return URL(fileURLWithPath: home)
            .appendingPathComponent("Library/Application Support/Sai Matrix", isDirectory: true)
    }

    func launchFlow() {
        if portIsOpen() {
            // Another instance already serves the port: never spawn a duplicate.
            DispatchQueue.main.async {
                NSWorkspace.shared.open(appURL)
                NSApp.terminate(nil)
            }
            return
        }
        guard let res = Bundle.main.resourceURL else { fail("Bundle resources are missing."); return }
        let py = res.appendingPathComponent("python/bin/python3")
        let server = res.appendingPathComponent("app/server.py")
        guard FileManager.default.isExecutableFile(atPath: py.path) else {
            fail("Bundled Python is missing at \(py.path)"); return
        }
        guard FileManager.default.fileExists(atPath: server.path) else {
            fail("server.py is missing at \(server.path)"); return
        }
        let data = dataDir()
        do {
            try FileManager.default.createDirectory(at: data, withIntermediateDirectories: true)
        } catch {
            fail("Cannot create data directory: \(error.localizedDescription)"); return
        }

        let p = Process()
        p.executableURL = py
        p.arguments = ["-I", "-B", server.path, "--data-dir", data.path]
        p.currentDirectoryURL = res.appendingPathComponent("app")
        var env = ProcessInfo.processInfo.environment
        let extra = "/usr/local/bin:/opt/homebrew/bin"
        env["PATH"] = (env["PATH"].map { $0 + ":" } ?? "") + extra
        p.environment = env
        let logURL = data.appendingPathComponent("sai-matrix.log")
        FileManager.default.createFile(atPath: logURL.path, contents: nil)
        if let fh = try? FileHandle(forWritingTo: logURL) {
            p.standardOutput = fh
            p.standardError = fh
        }
        p.terminationHandler = { [weak self] proc in
            DispatchQueue.main.async {
                guard let self = self, !self.quitting else { return }
                // Server died on its own; leave the menu item so the user can quit.
                self.child = nil
            }
        }
        do {
            try p.run()
        } catch {
            fail("Could not start the server: \(error.localizedDescription)"); return
        }
        child = p

        let deadline = Date().addingTimeInterval(30)
        while Date() < deadline {
            if httpReady() {
                DispatchQueue.main.async { NSWorkspace.shared.open(appURL) }
                return
            }
            if !p.isRunning { break }
            Thread.sleep(forTimeInterval: 0.25)
        }
        stopServer()
        fail("The Sai Matrix server did not start within 30 seconds. See \(logURL.path) for details.")
    }

    func fail(_ message: String) {
        DispatchQueue.main.async {
            let alert = NSAlert()
            alert.messageText = "Sai Matrix"
            alert.informativeText = message
            alert.alertStyle = .warning
            alert.runModal()
            NSApp.terminate(nil)
        }
    }

    func stopServer() {
        guard let p = child else { return }
        quitting = true
        if p.isRunning {
            p.terminate()                      // SIGTERM
            let deadline = Date().addingTimeInterval(3)
            while p.isRunning && Date() < deadline {
                Thread.sleep(forTimeInterval: 0.1)
            }
            if p.isRunning { kill(p.processIdentifier, SIGKILL) }
        }
        child = nil
    }

    @objc func openUI() {
        NSWorkspace.shared.open(appURL)
    }

    @objc func quit() {
        stopServer()
        NSApp.terminate(nil)
    }

    func applicationWillTerminate(_ notification: Notification) {
        stopServer()
    }
}

let app = NSApplication.shared
let delegate = AppDelegate()
app.delegate = delegate
app.run()
