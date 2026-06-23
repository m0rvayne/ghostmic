// Meeting Transcript MCP — Menu Bar Indicator
// Shows recording status with pause/restart/end controls.
// Reads watcher-status.json every 3 seconds.
// Compile: swiftc -framework AppKit statusbar.swift -o statusbar

import AppKit
import Foundation

class StatusBarDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var statusMenuItem: NSMenuItem!
    var pauseMenuItem: NSMenuItem!
    var restartMenuItem: NSMenuItem!
    var endMenuItem: NSMenuItem!
    var timer: Timer?

    let homePath = NSHomeDirectory()
    var statusPath: String { homePath + "/.meeting-transcript-mcp/watcher-status.json" }
    var controlPath: String { homePath + "/.meeting-transcript-mcp/watcher-control.json" }

    override init() {
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "🎙️"

        let menu = NSMenu()

        // User info
        let user = NSFullUserName()
        let userItem = NSMenuItem(title: user, action: nil, keyEquivalent: "")
        userItem.isEnabled = false
        menu.addItem(userItem)
        menu.addItem(NSMenuItem.separator())

        // Status
        statusMenuItem = NSMenuItem(title: "Status: Idle", action: nil, keyEquivalent: "")
        statusMenuItem.isEnabled = false
        menu.addItem(statusMenuItem)
        menu.addItem(NSMenuItem.separator())

        // Controls
        pauseMenuItem = NSMenuItem(title: "Pause", action: #selector(pauseRecording), keyEquivalent: "p")
        pauseMenuItem.target = self
        pauseMenuItem.isHidden = true
        menu.addItem(pauseMenuItem)

        restartMenuItem = NSMenuItem(title: "Resume", action: #selector(restartRecording), keyEquivalent: "r")
        restartMenuItem.target = self
        restartMenuItem.isHidden = true
        menu.addItem(restartMenuItem)

        endMenuItem = NSMenuItem(title: "End Recording", action: #selector(endRecording), keyEquivalent: "e")
        endMenuItem.target = self
        endMenuItem.isHidden = true
        menu.addItem(endMenuItem)

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
            statusItem.button?.title = "🎙️"
            statusMenuItem.title = "Watcher not running"
            pauseMenuItem.isHidden = true
            restartMenuItem.isHidden = true
            endMenuItem.isHidden = true
            return
        }

        switch state {
        case "RECORDING":
            statusItem.button?.title = "🎙️"
            if let button = statusItem.button {
                button.attributedTitle = NSAttributedString(
                    string: "🎙️",
                    attributes: [.foregroundColor: NSColor.systemRed]
                )
            }
            let transcript = json["transcript"] as? String ?? "unknown"
            statusMenuItem.title = "Recording: \(transcript)"
            pauseMenuItem.isHidden = false
            restartMenuItem.isHidden = true
            endMenuItem.isHidden = false

        case "PAUSED":
            statusItem.button?.title = "⏸️"
            statusMenuItem.title = "Paused"
            pauseMenuItem.isHidden = true
            restartMenuItem.isHidden = false
            endMenuItem.isHidden = false

        default:
            statusItem.button?.title = "🎙️"
            statusMenuItem.title = "Status: Idle"
            pauseMenuItem.isHidden = true
            restartMenuItem.isHidden = true
            endMenuItem.isHidden = true
        }
    }

    func writeControl(_ action: String) {
        let json = "{\"action\":\"\(action)\",\"timestamp\":\(Date().timeIntervalSince1970)}"
        try? json.write(toFile: controlPath, atomically: true, encoding: .utf8)
    }

    @objc func pauseRecording() {
        writeControl("pause")
    }

    @objc func restartRecording() {
        writeControl("resume")
    }

    @objc func endRecording() {
        writeControl("end")
    }
}

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = StatusBarDelegate()
app.delegate = delegate
app.run()
