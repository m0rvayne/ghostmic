// Meeting Transcript MCP — Menu Bar Indicator
// Shows recording status: red dot when active, pause when idle.
// Reads watcher-status.json every 3 seconds.
// Compile: swiftc -framework AppKit statusbar.swift -o statusbar

import AppKit
import Foundation

class StatusBarDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var statusMenuItem: NSMenuItem!
    var timer: Timer?
    let statusPath: String

    override init() {
        let home = NSHomeDirectory()
        statusPath = home + "/.meeting-transcript-mcp/watcher-status.json"
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "⏸"

        let menu = NSMenu()
        statusMenuItem = NSMenuItem(title: "Status: Idle", action: nil, keyEquivalent: "")
        menu.addItem(statusMenuItem)
        menu.addItem(NSMenuItem.separator())
        menu.addItem(NSMenuItem(title: "Quit", action: #selector(NSApplication.terminate(_:)), keyEquivalent: "q"))
        statusItem.menu = menu

        timer = Timer.scheduledTimer(withTimeInterval: 3.0, repeats: true) { [weak self] _ in
            self?.pollStatus()
        }
        pollStatus()
    }

    func pollStatus() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: statusPath)),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let state = json["state"] as? String else {
            statusItem.button?.title = "⏸"
            statusMenuItem.title = "Watcher not running"
            return
        }

        if state == "RECORDING" {
            statusItem.button?.title = "🔴"
            let transcript = json["transcript"] as? String ?? "unknown"
            statusMenuItem.title = "Recording: \(transcript)"
        } else {
            statusItem.button?.title = "⏸"
            statusMenuItem.title = "Status: Idle"
        }
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = StatusBarDelegate()
app.delegate = delegate
app.run()
