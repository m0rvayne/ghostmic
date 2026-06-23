// Meeting Transcript MCP — Menu Bar Indicator
// Custom styled popover menu with pause/resume/end controls.
// Compile: swiftc -O -framework AppKit statusbar.swift -o statusbar

import AppKit
import Foundation

// MARK: - Colors

struct Theme {
    static let bg = NSColor(red: 0.12, green: 0.13, blue: 0.16, alpha: 1.0)        // dark bg
    static let headerBg = NSColor(red: 0.15, green: 0.22, blue: 0.35, alpha: 1.0)   // blue header
    static let accent = NSColor(red: 0.25, green: 0.52, blue: 0.96, alpha: 1.0)     // blue accent
    static let textPrimary = NSColor.white
    static let textSecondary = NSColor(white: 0.6, alpha: 1.0)
    static let endRed = NSColor(red: 0.9, green: 0.3, blue: 0.3, alpha: 1.0)
    static let pauseYellow = NSColor(red: 0.95, green: 0.77, blue: 0.25, alpha: 1.0)
    static let resumeGreen = NSColor(red: 0.3, green: 0.85, blue: 0.45, alpha: 1.0)
    static let buttonBg = NSColor(red: 0.18, green: 0.20, blue: 0.25, alpha: 1.0)
    static let separator = NSColor(white: 0.25, alpha: 1.0)
}

// MARK: - Custom Menu View

class MenuContentView: NSView {
    var state: String = "IDLE"
    var transcriptName: String = ""
    var onPause: (() -> Void)?
    var onResume: (() -> Void)?
    var onEnd: (() -> Void)?
    var onQuit: (() -> Void)?

    override var intrinsicContentSize: NSSize {
        return NSSize(width: 260, height: state == "IDLE" ? 120 : 200)
    }

    override func draw(_ dirtyRect: NSRect) {
        let bounds = self.bounds

        // Background
        Theme.bg.setFill()
        NSBezierPath(roundedRect: bounds, xRadius: 10, yRadius: 10).fill()

        // Header bar
        Theme.headerBg.setFill()
        let headerPath = NSBezierPath()
        headerPath.move(to: NSPoint(x: 0, y: bounds.height - 52))
        headerPath.line(to: NSPoint(x: 0, y: bounds.height - 10))
        headerPath.appendArc(from: NSPoint(x: 0, y: bounds.height), to: NSPoint(x: 10, y: bounds.height), radius: 10)
        headerPath.line(to: NSPoint(x: bounds.width - 10, y: bounds.height))
        headerPath.appendArc(from: NSPoint(x: bounds.width, y: bounds.height), to: NSPoint(x: bounds.width, y: bounds.height - 10), radius: 10)
        headerPath.line(to: NSPoint(x: bounds.width, y: bounds.height - 52))
        headerPath.close()
        headerPath.fill()

        // Username
        let nameAttrs: [NSAttributedString.Key: Any] = [
            .foregroundColor: Theme.textPrimary,
            .font: NSFont.systemFont(ofSize: 14, weight: .bold)
        ]
        let name = "m0rvayne"
        name.draw(at: NSPoint(x: 16, y: bounds.height - 36), withAttributes: nameAttrs)

        // Mic icon
        let micAttrs: [NSAttributedString.Key: Any] = [
            .foregroundColor: Theme.accent,
            .font: NSFont.systemFont(ofSize: 16)
        ]
        "🎙".draw(at: NSPoint(x: bounds.width - 36, y: bounds.height - 38), withAttributes: micAttrs)

        // Status section
        let statusY = bounds.height - 80
        let statusAttrs: [NSAttributedString.Key: Any] = [
            .foregroundColor: Theme.textSecondary,
            .font: NSFont.systemFont(ofSize: 11)
        ]

        if state == "RECORDING" {
            // Recording indicator
            let dotAttrs: [NSAttributedString.Key: Any] = [
                .foregroundColor: Theme.endRed,
                .font: NSFont.systemFont(ofSize: 10)
            ]
            "●".draw(at: NSPoint(x: 16, y: statusY), withAttributes: dotAttrs)
            let recAttrs: [NSAttributedString.Key: Any] = [
                .foregroundColor: Theme.textPrimary,
                .font: NSFont.systemFont(ofSize: 12, weight: .medium)
            ]
            "Recording".draw(at: NSPoint(x: 30, y: statusY), withAttributes: recAttrs)

            // Transcript name
            let truncated = transcriptName.count > 30 ? String(transcriptName.prefix(30)) + "..." : transcriptName
            truncated.draw(at: NSPoint(x: 16, y: statusY - 22), withAttributes: statusAttrs)

        } else if state == "PAUSED" {
            let pauseAttrs: [NSAttributedString.Key: Any] = [
                .foregroundColor: Theme.pauseYellow,
                .font: NSFont.systemFont(ofSize: 12, weight: .medium)
            ]
            "⏸ Paused".draw(at: NSPoint(x: 16, y: statusY), withAttributes: pauseAttrs)
            let truncated = transcriptName.count > 30 ? String(transcriptName.prefix(30)) + "..." : transcriptName
            truncated.draw(at: NSPoint(x: 16, y: statusY - 22), withAttributes: statusAttrs)

        } else {
            let idleAttrs: [NSAttributedString.Key: Any] = [
                .foregroundColor: Theme.textSecondary,
                .font: NSFont.systemFont(ofSize: 12)
            ]
            "No active recording".draw(at: NSPoint(x: 16, y: statusY), withAttributes: idleAttrs)
        }

        // Separator
        Theme.separator.setStroke()
        let sepY: CGFloat = state == "IDLE" ? 36 : 76
        let sep = NSBezierPath()
        sep.move(to: NSPoint(x: 12, y: sepY))
        sep.line(to: NSPoint(x: bounds.width - 12, y: sepY))
        sep.lineWidth = 0.5
        sep.stroke()
    }

    // Button hit testing
    override func mouseUp(with event: NSEvent) {
        let loc = convert(event.locationInWindow, from: nil)
        let bounds = self.bounds

        if state == "IDLE" {
            // Only quit button
            if loc.y < 36 {
                onQuit?()
            }
            return
        }

        // Button area: y = 40 to 72
        let buttonY: CGFloat = 40
        let buttonH: CGFloat = 32
        if loc.y >= buttonY && loc.y <= buttonY + buttonH {
            let thirdW = bounds.width / 3

            if state == "RECORDING" {
                if loc.x < thirdW * 2 {
                    onPause?()
                } else {
                    onEnd?()
                }
            } else if state == "PAUSED" {
                if loc.x < thirdW * 2 {
                    onResume?()
                } else {
                    onEnd?()
                }
            }
        }

        if loc.y < buttonY - 4 {
            onQuit?()
        }
    }

    func drawButtons() {
        let bounds = self.bounds
        guard state != "IDLE" else { return }

        let buttonY: CGFloat = 42
        let buttonH: CGFloat = 28
        let margin: CGFloat = 12
        let gap: CGFloat = 8
        let totalW = bounds.width - margin * 2 - gap

        // Left button (Pause or Resume) — 2/3 width
        let leftW = totalW * 0.65
        let leftRect = NSRect(x: margin, y: buttonY, width: leftW, height: buttonH)
        Theme.buttonBg.setFill()
        NSBezierPath(roundedRect: leftRect, xRadius: 6, yRadius: 6).fill()

        if state == "RECORDING" {
            let attrs: [NSAttributedString.Key: Any] = [
                .foregroundColor: Theme.pauseYellow,
                .font: NSFont.systemFont(ofSize: 12, weight: .semibold)
            ]
            let text = "⏸  PAUSE"
            let size = text.size(withAttributes: attrs)
            text.draw(at: NSPoint(x: leftRect.midX - size.width/2, y: buttonY + 6), withAttributes: attrs)
        } else {
            let attrs: [NSAttributedString.Key: Any] = [
                .foregroundColor: Theme.resumeGreen,
                .font: NSFont.systemFont(ofSize: 12, weight: .semibold)
            ]
            let text = "▶  RESUME"
            let size = text.size(withAttributes: attrs)
            text.draw(at: NSPoint(x: leftRect.midX - size.width/2, y: buttonY + 6), withAttributes: attrs)
        }

        // Right button (End) — 1/3 width
        let rightW = totalW * 0.35
        let rightRect = NSRect(x: margin + leftW + gap, y: buttonY, width: rightW, height: buttonH)
        Theme.buttonBg.setFill()
        NSBezierPath(roundedRect: rightRect, xRadius: 6, yRadius: 6).fill()

        let endAttrs: [NSAttributedString.Key: Any] = [
            .foregroundColor: Theme.endRed,
            .font: NSFont.systemFont(ofSize: 12, weight: .semibold)
        ]
        let endText = "■  END"
        let endSize = endText.size(withAttributes: endAttrs)
        endText.draw(at: NSPoint(x: rightRect.midX - endSize.width/2, y: buttonY + 6), withAttributes: endAttrs)

        // Quit at the bottom
        let quitAttrs: [NSAttributedString.Key: Any] = [
            .foregroundColor: Theme.textSecondary,
            .font: NSFont.systemFont(ofSize: 11)
        ]
        "Quit".draw(at: NSPoint(x: 16, y: 12), withAttributes: quitAttrs)
    }

    override func layout() {
        super.layout()
        setNeedsDisplay(bounds)
    }
}

// MARK: - Popover Controller

class PopoverController: NSObject {
    let popover = NSPopover()
    let contentView = MenuContentView()
    var statusItem: NSStatusItem!

    let homePath = NSHomeDirectory()
    var statusPath: String { homePath + "/.meeting-transcript-mcp/watcher-status.json" }
    var controlPath: String { homePath + "/.meeting-transcript-mcp/watcher-control.json" }

    func setup() {
        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.title = "🎙️"
        statusItem.button?.target = self
        statusItem.button?.action = #selector(togglePopover)

        let vc = NSViewController()
        vc.view = contentView
        popover.contentViewController = vc
        popover.behavior = .transient
        popover.animates = true

        contentView.onPause = { [weak self] in self?.writeControl("pause"); self?.popover.close() }
        contentView.onResume = { [weak self] in self?.writeControl("resume"); self?.popover.close() }
        contentView.onEnd = { [weak self] in self?.writeControl("end"); self?.popover.close() }
        contentView.onQuit = { NSApp.terminate(nil) }

        Timer.scheduledTimer(withTimeInterval: 2.0, repeats: true) { [weak self] _ in
            self?.pollStatus()
        }
        pollStatus()
    }

    @objc func togglePopover() {
        if popover.isShown {
            popover.close()
        } else if let button = statusItem.button {
            // Force redraw before showing
            contentView.invalidateIntrinsicContentSize()
            contentView.setNeedsDisplay(contentView.bounds)
            popover.show(relativeTo: button.bounds, of: button, preferredEdge: .minY)
            // Draw buttons after popover is visible
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.05) { [weak self] in
                self?.contentView.drawButtons()
            }
        }
    }

    func pollStatus() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: statusPath)),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let state = json["state"] as? String else {
            contentView.state = "IDLE"
            contentView.transcriptName = ""
            statusItem.button?.title = "🎙️"
            return
        }

        contentView.state = state
        contentView.transcriptName = json["transcript"] as? String ?? ""

        switch state {
        case "RECORDING":
            statusItem.button?.title = "🔴"
        case "PAUSED":
            statusItem.button?.title = "⏸️"
        default:
            statusItem.button?.title = "🎙️"
        }
    }

    func writeControl(_ action: String) {
        let json = "{\"action\":\"\(action)\",\"timestamp\":\(Date().timeIntervalSince1970)}"
        try? json.write(toFile: controlPath, atomically: true, encoding: .utf8)
    }
}

// MARK: - App Delegate

class AppDelegate: NSObject, NSApplicationDelegate {
    let controller = PopoverController()

    func applicationDidFinishLaunching(_ notification: Notification) {
        controller.setup()
    }
}

// MARK: - Main

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let delegate = AppDelegate()
app.delegate = delegate
app.run()
