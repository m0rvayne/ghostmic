// ghostmic — Premium Menu Bar
// Real NSButton/NSTextField/NSStackView with settings panel
// Compile: swiftc -O -framework AppKit statusbar.swift -o statusbar

import AppKit
import Foundation


// MARK: - Ghost Icon Generator

func ghostIcon(filled: Bool, recording: Bool, size: CGFloat = 18) -> NSImage {
    let img = NSImage(size: NSSize(width: size, height: size))
    img.lockFocus()

    let scale = size / 16.0

    // Ghost body path (from SVG)
    let body = NSBezierPath()
    body.move(to: NSPoint(x: 8 * scale, y: (16 - 1) * scale))
    body.curve(to: NSPoint(x: 2 * scale, y: (16 - 6.5) * scale),
               controlPoint1: NSPoint(x: 4.5 * scale, y: (16 - 1) * scale),
               controlPoint2: NSPoint(x: 2 * scale, y: (16 - 3.5) * scale))
    body.line(to: NSPoint(x: 2 * scale, y: (16 - 13) * scale))
    body.line(to: NSPoint(x: 4 * scale, y: (16 - 11.5) * scale))
    body.line(to: NSPoint(x: 6 * scale, y: (16 - 13) * scale))
    body.line(to: NSPoint(x: 8 * scale, y: (16 - 11.5) * scale))
    body.line(to: NSPoint(x: 10 * scale, y: (16 - 13) * scale))
    body.line(to: NSPoint(x: 12 * scale, y: (16 - 11.5) * scale))
    body.line(to: NSPoint(x: 14 * scale, y: (16 - 13) * scale))
    body.line(to: NSPoint(x: 14 * scale, y: (16 - 6.5) * scale))
    body.curve(to: NSPoint(x: 8 * scale, y: (16 - 1) * scale),
               controlPoint1: NSPoint(x: 14 * scale, y: (16 - 3.5) * scale),
               controlPoint2: NSPoint(x: 11.5 * scale, y: (16 - 1) * scale))
    body.close()

    if filled {
        NSColor.black.setFill()
        body.fill()
        // White eyes
        NSColor.white.setFill()
        let leftEye = NSBezierPath(ovalIn: NSRect(x: (6 - 1.3) * scale, y: (16 - 6 - 1.3) * scale, width: 2.6 * scale, height: 2.6 * scale))
        let rightEye = NSBezierPath(ovalIn: NSRect(x: (10 - 1.3) * scale, y: (16 - 6 - 1.3) * scale, width: 2.6 * scale, height: 2.6 * scale))
        leftEye.fill()
        rightEye.fill()
    } else {
        // Outline only (idle)
        NSColor.black.setStroke()
        body.lineWidth = 1.2 * scale
        body.stroke()
        // Black dot eyes
        NSColor.black.setFill()
        let leftEye = NSBezierPath(ovalIn: NSRect(x: (6 - 1.3) * scale, y: (16 - 6 - 1.3) * scale, width: 2.6 * scale, height: 2.6 * scale))
        let rightEye = NSBezierPath(ovalIn: NSRect(x: (10 - 1.3) * scale, y: (16 - 6 - 1.3) * scale, width: 2.6 * scale, height: 2.6 * scale))
        leftEye.fill()
        rightEye.fill()
    }

    // Red recording dot (top-right)
    if recording {
        NSColor(red: 1.0, green: 0.23, blue: 0.19, alpha: 1.0).setFill()
        let dot = NSBezierPath(ovalIn: NSRect(x: (13.5 - 2) * scale, y: (16 - 2.5 - 2) * scale, width: 4 * scale, height: 4 * scale))
        dot.fill()
    }

    img.unlockFocus()
    img.isTemplate = !recording  // template = macOS auto-colors for light/dark. Recording has red dot so not template.
    return img
}

// MARK: - Theme

let kBg = NSColor(red: 0.11, green: 0.11, blue: 0.13, alpha: 1.0)
let kStopRed = NSColor(red: 0.92, green: 0.25, blue: 0.25, alpha: 1.0)
let kPauseYellow = NSColor(red: 0.95, green: 0.72, blue: 0.12, alpha: 1.0)
let kGreen = NSColor(red: 0.18, green: 0.8, blue: 0.38, alpha: 1.0)
let kBlue = NSColor(red: 0.35, green: 0.58, blue: 1.0, alpha: 1.0)
let kDimText = NSColor(white: 0.45, alpha: 1.0)
let kBtnBg = NSColor(white: 1.0, alpha: 0.07)
let kBtnHover = NSColor(white: 1.0, alpha: 0.14)
let kWidth: CGFloat = 280

// MARK: - Config

struct AppConfig {
    var whisperModel: String = "small"
    var diarization: Bool = true
    var transcriptsPath: String

    static let models = ["tiny", "base", "small", "medium", "large-v3"]

    static func load(home: String) -> AppConfig {
        let dir = home + "/.ghostmic"
        // Read current env config from watcher-status or defaults
        var cfg = AppConfig(transcriptsPath: dir + "/transcripts")
        if let data = try? Data(contentsOf: URL(fileURLWithPath: dir + "/config.json")),
           let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            cfg.whisperModel = json["whisper_model"] as? String ?? "small"
            cfg.diarization = (json["diarization"] as? String ?? "1") == "1"
            if let p = json["transcripts_path"] as? String, !p.isEmpty { cfg.transcriptsPath = p }
        }
        return cfg
    }

    func save(home: String) {
        let dir = home + "/.ghostmic"
        let json: [String: Any] = [
            "whisper_model": whisperModel,
            "diarization": diarization ? "1" : "0",
            "transcripts_path": transcriptsPath,
        ]
        if let data = try? JSONSerialization.data(withJSONObject: json),
           let str = String(data: data, encoding: .utf8) {
            try? str.write(toFile: dir + "/config.json", atomically: true, encoding: .utf8)
        }
    }
}

// MARK: - Hover Button

class HoverButton: NSButton {
    var hoverColor: NSColor = kBtnHover
    var normalColor: NSColor = kBtnBg
    var area: NSTrackingArea?

    override func updateTrackingAreas() {
        if let a = area { removeTrackingArea(a) }
        area = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self)
        addTrackingArea(area!)
    }

    override func mouseEntered(with event: NSEvent) {
        animator().layer?.backgroundColor = hoverColor.cgColor
    }

    override func mouseExited(with event: NSEvent) {
        animator().layer?.backgroundColor = normalColor.cgColor
    }

    static func make(title: String, color: NSColor, target: AnyObject?, action: Selector) -> HoverButton {
        let b = HoverButton()
        b.title = title
        b.isBordered = false
        b.wantsLayer = true
        b.layer?.cornerRadius = 7
        b.layer?.backgroundColor = kBtnBg.cgColor
        b.layer?.borderWidth = 1
        b.layer?.borderColor = color.withAlphaComponent(0.35).cgColor
        b.contentTintColor = color
        b.font = .systemFont(ofSize: 11, weight: .semibold)
        b.target = target
        b.action = action
        b.translatesAutoresizingMaskIntoConstraints = false
        b.heightAnchor.constraint(equalToConstant: 32).isActive = true
        b.normalColor = kBtnBg
        b.hoverColor = color.withAlphaComponent(0.18)
        return b
    }
}

// MARK: - Hover Row

class HoverRow: NSView {
    var area: NSTrackingArea?
    var onClick: (() -> Void)?

    override func updateTrackingAreas() {
        if let a = area { removeTrackingArea(a) }
        area = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect], owner: self)
        addTrackingArea(area!)
    }

    override func mouseEntered(with event: NSEvent) {
        wantsLayer = true
        animator().layer?.backgroundColor = NSColor.white.withAlphaComponent(0.06).cgColor
    }

    override func mouseExited(with event: NSEvent) {
        animator().layer?.backgroundColor = NSColor.clear.cgColor
    }

    override func mouseUp(with event: NSEvent) {
        onClick?()
    }

    override func resetCursorRects() {
        addCursorRect(bounds, cursor: .pointingHand)
    }
}

// MARK: - Link Button

class LinkButton: NSButton {
    var area: NSTrackingArea?
    override func resetCursorRects() { addCursorRect(bounds, cursor: .pointingHand) }
    override func updateTrackingAreas() {
        if let a = area { removeTrackingArea(a) }
        area = NSTrackingArea(rect: bounds, options: [.mouseEnteredAndExited, .activeAlways, .inVisibleRect, .cursorUpdate], owner: self)
        addTrackingArea(area!)
    }
    override func mouseEntered(with event: NSEvent) {
        (cell as? NSButtonCell)?.attributedTitle = NSAttributedString(string: title,
            attributes: [.foregroundColor: kBlue, .font: font ?? .systemFont(ofSize: 11), .underlineStyle: NSUnderlineStyle.single.rawValue])
    }
    override func mouseExited(with event: NSEvent) {
        (cell as? NSButtonCell)?.attributedTitle = NSAttributedString(string: title,
            attributes: [.foregroundColor: kBlue.withAlphaComponent(0.7), .font: font ?? .systemFont(ofSize: 11)])
    }
}

// MARK: - Helpers

func lbl(_ text: String, size: CGFloat = 12, weight: NSFont.Weight = .regular, color: NSColor = .white) -> NSTextField {
    let t = NSTextField(labelWithString: text)
    t.font = .systemFont(ofSize: size, weight: weight)
    t.textColor = color
    t.translatesAutoresizingMaskIntoConstraints = false
    return t
}

func sep() -> NSView {
    let v = NSView(); v.wantsLayer = true
    v.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.08).cgColor
    v.translatesAutoresizingMaskIntoConstraints = false
    v.heightAnchor.constraint(equalToConstant: 1).isActive = true
    return v
}

func dot(color: NSColor) -> NSView {
    let v = NSView(); v.wantsLayer = true
    v.layer?.backgroundColor = color.cgColor; v.layer?.cornerRadius = 4
    v.translatesAutoresizingMaskIntoConstraints = false
    v.widthAnchor.constraint(equalToConstant: 8).isActive = true
    v.heightAnchor.constraint(equalToConstant: 8).isActive = true
    return v
}

func padded(_ view: NSView, h: CGFloat = 14, v: CGFloat = 4) -> NSView {
    let c = NSView(); c.translatesAutoresizingMaskIntoConstraints = false
    view.translatesAutoresizingMaskIntoConstraints = false; c.addSubview(view)
    NSLayoutConstraint.activate([
        c.widthAnchor.constraint(equalToConstant: kWidth),
        view.leadingAnchor.constraint(equalTo: c.leadingAnchor, constant: h),
        view.trailingAnchor.constraint(equalTo: c.trailingAnchor, constant: -h),
        view.topAnchor.constraint(equalTo: c.topAnchor, constant: v),
        view.bottomAnchor.constraint(equalTo: c.bottomAnchor, constant: -v),
    ])
    return c
}

// MARK: - App Delegate

class AppDelegate: NSObject, NSApplicationDelegate {
    var statusItem: NSStatusItem!
    var menu: NSMenu!

    var statusDot = NSView()
    var statusLabel = NSTextField()
    var timerLabel = NSTextField()
    var stopBtn = HoverButton()
    var pauseBtn = HoverButton()
    var restartBtn = HoverButton()
    var buttonsRow = NSStackView()
    var statusRow = NSStackView()
    var timerRow = NSStackView()
    var gotoBtn = LinkButton()

    // Settings items (hidden by default)
    var settingsItems: [NSMenuItem] = []
    var mainItems: [NSMenuItem] = []
    var settingsVisible = false

    var modelButtons: [HoverButton] = []
    var diarizationToggle: NSButton!
    var pathDisplay: NSTextField!

    var currentState = "IDLE"
    var recordingStart: TimeInterval = 0
    var transcriptName = ""
    var config: AppConfig!

    let homePath = NSHomeDirectory()
    var statusPath: String { homePath + "/.ghostmic/watcher-status.json" }
    var controlPath: String { homePath + "/.ghostmic/watcher-control.json" }
    var transcriptsDir: String { config?.transcriptsPath ?? homePath + "/.ghostmic/transcripts" }

    func applicationDidFinishLaunching(_ n: Notification) {
        config = AppConfig.load(home: homePath)

        statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        statusItem.button?.image = ghostIcon(filled: false, recording: false)
        statusItem.button?.title = ""

        menu = NSMenu()
        menu.appearance = NSAppearance(named: .darkAqua)
        menu.minimumWidth = kWidth

        buildMenu()

        self.statusItem.menu = menu
        // Use .common mode so timers fire even when menu is open (tracking mode)
        let pollT = Timer(timeInterval: 2.0, repeats: true) { [weak self] _ in self?.poll() }
        let tickT = Timer(timeInterval: 1.0, repeats: true) { [weak self] _ in self?.updateTimer() }
        RunLoop.main.add(pollT, forMode: .common)
        RunLoop.main.add(tickT, forMode: .common)
        poll()
    }

    func buildMenu() {
        menu.removeAllItems()

        // Header
        let headerItem = NSMenuItem()
        headerItem.view = makeHeader()
        menu.addItem(headerItem)

        let s1 = NSMenuItem(); s1.view = padded(sep(), h: 0, v: 4); menu.addItem(s1)

        // Buttons
        let btnItem = NSMenuItem()
        stopBtn = HoverButton.make(title: "STOP", color: kStopRed, target: self, action: #selector(doStop))
        pauseBtn = HoverButton.make(title: "PAUSE", color: kPauseYellow, target: self, action: #selector(doPause))
        restartBtn = HoverButton.make(title: "RESTART", color: kGreen, target: self, action: #selector(doRestart))
        buttonsRow = NSStackView(views: [stopBtn, pauseBtn, restartBtn])
        buttonsRow.distribution = .fillEqually; buttonsRow.spacing = 6
        btnItem.view = padded(buttonsRow, h: 14, v: 6)
        menu.addItem(btnItem)

        let s2 = NSMenuItem(); s2.view = padded(sep(), h: 14, v: 4); menu.addItem(s2)

        // Status + Timer on one line
        let si = NSMenuItem()
        statusDot = dot(color: kGreen)
        statusLabel = lbl("No active recording", size: 12, weight: .medium, color: kDimText)
        timerLabel = lbl("", size: 12, weight: .regular, color: kDimText)
        timerLabel.font = .monospacedDigitSystemFont(ofSize: 12, weight: .regular)
        timerLabel.alignment = .right
        timerLabel.setContentHuggingPriority(.required, for: .horizontal)
        let spacer = NSView(); spacer.translatesAutoresizingMaskIntoConstraints = false
        statusRow = NSStackView(views: [statusDot, statusLabel, spacer, timerLabel])
        statusRow.spacing = 8; statusRow.alignment = .centerY
        si.view = padded(statusRow, h: 14, v: 4)
        menu.addItem(si)

        // Go To File — styled button
        let gotoItem = NSMenuItem()
        let gotoButton = HoverButton.make(title: "Open Transcript", color: kBlue, target: self, action: #selector(doGoToFile))
        gotoButton.font = .systemFont(ofSize: 11, weight: .medium)
        gotoItem.view = padded(gotoButton, h: 14, v: 4)
        menu.addItem(gotoItem)

        let s3 = NSMenuItem(); s3.view = padded(sep(), h: 0, v: 6); menu.addItem(s3)

        // Settings row with hover
        let gearItem = NSMenuItem()
        gearItem.view = makeHoverRow("⚙  Settings", action: { [weak self] in self?.toggleSettings() })
        menu.addItem(gearItem)

        // Settings panel (initially hidden)
        addSettingsPanel()

        poll()
    }

    // MARK: - Settings Panel

    func addSettingsPanel() {
        // --- Speaker Labels (first) ---
        let diaItem = NSMenuItem()
        let diaRow = NSView(); diaRow.translatesAutoresizingMaskIntoConstraints = false
        let diaLabel = lbl("Speaker Labels", size: 11, weight: .medium, color: .white.withAlphaComponent(0.7))
        diarizationToggle = NSButton(checkboxWithTitle: "", target: self, action: nil)
        diarizationToggle.state = config.diarization ? .on : .off
        diarizationToggle.controlSize = .small
        diarizationToggle.translatesAutoresizingMaskIntoConstraints = false
        diaRow.addSubview(diaLabel); diaRow.addSubview(diarizationToggle)
        NSLayoutConstraint.activate([
            diaRow.widthAnchor.constraint(equalToConstant: kWidth),
            diaRow.heightAnchor.constraint(equalToConstant: 28),
            diaLabel.leadingAnchor.constraint(equalTo: diaRow.leadingAnchor, constant: 14),
            diaLabel.centerYAnchor.constraint(equalTo: diaRow.centerYAnchor),
            diarizationToggle.trailingAnchor.constraint(equalTo: diaRow.trailingAnchor, constant: -14),
            diarizationToggle.centerYAnchor.constraint(equalTo: diaRow.centerYAnchor),
        ])
        diaItem.view = diaRow; diaItem.isHidden = true
        menu.addItem(diaItem); settingsItems.append(diaItem)

        // --- Model label ---
        let modelLabelItem = NSMenuItem()
        let mlbl = lbl("WHISPER MODEL", size: 10, weight: .semibold, color: kDimText)
        modelLabelItem.view = padded(mlbl, h: 14, v: 4)
        modelLabelItem.isHidden = true
        menu.addItem(modelLabelItem); settingsItems.append(modelLabelItem)

        // --- Model selector: row of HoverButtons ---
        let modelItem = NSMenuItem()
        var modelBtns: [HoverButton] = []
        for m in AppConfig.models {
            let isActive = (m == config.whisperModel)
            let btn = HoverButton()
            btn.title = m
            btn.isBordered = false; btn.wantsLayer = true
            btn.layer?.cornerRadius = 6
            btn.font = .systemFont(ofSize: 10, weight: isActive ? .bold : .medium)
            btn.contentTintColor = isActive ? .white : kDimText
            btn.layer?.backgroundColor = isActive ? kBlue.withAlphaComponent(0.3).cgColor : kBtnBg.cgColor
            btn.layer?.borderWidth = isActive ? 1 : 0
            btn.layer?.borderColor = kBlue.withAlphaComponent(0.5).cgColor
            btn.normalColor = isActive ? kBlue.withAlphaComponent(0.3) : kBtnBg
            btn.hoverColor = kBlue.withAlphaComponent(0.2)
            btn.target = self; btn.action = #selector(modelSelected(_:))
            btn.translatesAutoresizingMaskIntoConstraints = false
            btn.heightAnchor.constraint(equalToConstant: 26).isActive = true
            modelBtns.append(btn)
        }
        modelButtons = modelBtns
        let modelStack = NSStackView(views: modelBtns)
        modelStack.distribution = .fillEqually; modelStack.spacing = 4
        modelItem.view = padded(modelStack, h: 14, v: 2)
        modelItem.isHidden = true
        menu.addItem(modelItem); settingsItems.append(modelItem)

        // --- Save Path ---
        let pathLabelItem = NSMenuItem()
        let plbl = lbl("SAVE PATH", size: 10, weight: .semibold, color: kDimText)
        pathLabelItem.view = padded(plbl, h: 14, v: 4)
        pathLabelItem.isHidden = true
        menu.addItem(pathLabelItem); settingsItems.append(pathLabelItem)

        let pathItem = NSMenuItem()
        let pathRow = NSView(); pathRow.translatesAutoresizingMaskIntoConstraints = false
        pathDisplay = lbl(shortenPath(config.transcriptsPath), size: 10, weight: .regular, color: .white.withAlphaComponent(0.6))
        pathDisplay.lineBreakMode = .byTruncatingMiddle

        // Rounded border around path
        let pathBox = NSView(); pathBox.wantsLayer = true
        pathBox.layer?.cornerRadius = 6
        pathBox.layer?.borderWidth = 1
        pathBox.layer?.borderColor = NSColor.white.withAlphaComponent(0.12).cgColor
        pathBox.layer?.backgroundColor = NSColor.white.withAlphaComponent(0.04).cgColor
        pathBox.translatesAutoresizingMaskIntoConstraints = false
        pathBox.addSubview(pathDisplay)
        NSLayoutConstraint.activate([
            pathDisplay.leadingAnchor.constraint(equalTo: pathBox.leadingAnchor, constant: 8),
            pathDisplay.trailingAnchor.constraint(equalTo: pathBox.trailingAnchor, constant: -8),
            pathDisplay.centerYAnchor.constraint(equalTo: pathBox.centerYAnchor),
        ])

        let browseBtn = HoverButton.make(title: "Browse", color: kBlue, target: self, action: #selector(doBrowsePath))
        browseBtn.font = .systemFont(ofSize: 10, weight: .medium)
        browseBtn.heightAnchor.constraint(equalToConstant: 24).isActive = true
        browseBtn.widthAnchor.constraint(equalToConstant: 64).isActive = true
        pathRow.addSubview(pathBox); pathRow.addSubview(browseBtn)
        NSLayoutConstraint.activate([
            pathRow.widthAnchor.constraint(equalToConstant: kWidth),
            pathRow.heightAnchor.constraint(equalToConstant: 30),
            pathBox.leadingAnchor.constraint(equalTo: pathRow.leadingAnchor, constant: 14),
            pathBox.trailingAnchor.constraint(equalTo: browseBtn.leadingAnchor, constant: -6),
            pathBox.centerYAnchor.constraint(equalTo: pathRow.centerYAnchor),
            pathBox.heightAnchor.constraint(equalToConstant: 24),
            browseBtn.trailingAnchor.constraint(equalTo: pathRow.trailingAnchor, constant: -14),
            browseBtn.centerYAnchor.constraint(equalTo: pathRow.centerYAnchor),
        ])
        pathItem.view = pathRow; pathItem.isHidden = true
        menu.addItem(pathItem); settingsItems.append(pathItem)

        // --- Separator + Save ---
        let ss = NSMenuItem(); ss.view = padded(sep(), h: 14, v: 6); ss.isHidden = true
        menu.addItem(ss); settingsItems.append(ss)

        let saveItem = NSMenuItem()
        let saveBtn = HoverButton.make(title: "Save & Apply", color: kBlue, target: self, action: #selector(doSave))
        saveBtn.font = .systemFont(ofSize: 11, weight: .semibold)
        saveItem.view = padded(saveBtn, h: 14, v: 4)
        saveItem.isHidden = true
        menu.addItem(saveItem); settingsItems.append(saveItem)
    }

    func toggleSettings() {
        settingsVisible.toggle()
        for item in settingsItems { item.isHidden = !settingsVisible }
        // Force menu resize
        menu.update()
    }

    // MARK: - View Builders

    func makeHeader() -> NSView {
        let container = NSView(); container.translatesAutoresizingMaskIntoConstraints = false
        let logoText = NSAttributedString(string: "M 0 R V A Y N E", attributes: [
            .foregroundColor: NSColor.white.withAlphaComponent(0.75),
            .font: NSFont.systemFont(ofSize: 11, weight: .bold),
            .kern: 3.0,
        ])
        let nameLabel = NSTextField(labelWithAttributedString: logoText)
        nameLabel.alignment = .center; nameLabel.translatesAutoresizingMaskIntoConstraints = false
        let accent = NSView(); accent.wantsLayer = true
        accent.layer?.backgroundColor = kBlue.withAlphaComponent(0.4).cgColor
        accent.layer?.cornerRadius = 0.5; accent.translatesAutoresizingMaskIntoConstraints = false
        container.addSubview(nameLabel); container.addSubview(accent)
        NSLayoutConstraint.activate([
            container.heightAnchor.constraint(equalToConstant: 42),
            container.widthAnchor.constraint(equalToConstant: kWidth),
            nameLabel.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            nameLabel.centerYAnchor.constraint(equalTo: container.centerYAnchor, constant: -3),
            accent.topAnchor.constraint(equalTo: nameLabel.bottomAnchor, constant: 4),
            accent.centerXAnchor.constraint(equalTo: container.centerXAnchor),
            accent.widthAnchor.constraint(equalToConstant: 40),
            accent.heightAnchor.constraint(equalToConstant: 1.5),
        ])
        return container
    }

    func makeHoverRow(_ text: String, action: @escaping () -> Void) -> NSView {
        let row = HoverRow(); row.translatesAutoresizingMaskIntoConstraints = false
        row.wantsLayer = true; row.onClick = action
        let l = lbl(text, size: 12, weight: .regular, color: kDimText)
        row.addSubview(l)
        NSLayoutConstraint.activate([
            row.widthAnchor.constraint(equalToConstant: kWidth),
            row.heightAnchor.constraint(equalToConstant: 30),
            l.leadingAnchor.constraint(equalTo: row.leadingAnchor, constant: 14),
            l.centerYAnchor.constraint(equalTo: row.centerYAnchor),
        ])
        return row
    }

    // MARK: - State

    func poll() {
        guard let data = try? Data(contentsOf: URL(fileURLWithPath: statusPath)),
              let json = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
              let state = json["state"] as? String else {
            setState("IDLE", transcript: "", start: 0); return
        }
        setState(state, transcript: json["transcript"] as? String ?? "", start: json["timestamp"] as? TimeInterval ?? 0)
    }

    func setState(_ state: String, transcript: String, start: TimeInterval) {
        currentState = state; transcriptName = transcript; recordingStart = start
        let active = (state == "RECORDING" || state == "PAUSED")
        statusItem.button?.title = ""
        switch state {
        case "RECORDING":
            statusItem.button?.image = ghostIcon(filled: true, recording: true)
        case "PAUSED":
            statusItem.button?.image = ghostIcon(filled: true, recording: false)
        default:
            statusItem.button?.image = ghostIcon(filled: false, recording: false)
        }
        buttonsRow.isHidden = !active
        if state == "RECORDING" {
            statusDot.layer?.backgroundColor = kGreen.cgColor
            statusLabel.stringValue = "Transcript active"; statusLabel.textColor = .white.withAlphaComponent(0.9)
            pauseBtn.title = "PAUSE"; pauseBtn.contentTintColor = kPauseYellow
            pauseBtn.layer?.borderColor = kPauseYellow.withAlphaComponent(0.35).cgColor
            startPulse()
        } else if state == "PAUSED" {
            statusDot.layer?.backgroundColor = kPauseYellow.cgColor
            statusLabel.stringValue = "Paused"; statusLabel.textColor = kPauseYellow
            pauseBtn.title = "RESUME"; pauseBtn.contentTintColor = kGreen
            pauseBtn.layer?.borderColor = kGreen.withAlphaComponent(0.35).cgColor
            stopPulse()
        } else {
            statusDot.layer?.backgroundColor = kDimText.cgColor
            statusLabel.stringValue = "No active recording"; statusLabel.textColor = kDimText
            stopPulse()
        }
        updateTimer()
    }

    func startPulse() {
        guard statusDot.layer?.animation(forKey: "pulse") == nil else { return }
        let anim = CABasicAnimation(keyPath: "opacity")
        anim.fromValue = 1.0
        anim.toValue = 0.25
        anim.duration = 0.8
        anim.autoreverses = true
        anim.repeatCount = .infinity
        anim.timingFunction = CAMediaTimingFunction(name: .easeInEaseOut)
        statusDot.layer?.add(anim, forKey: "pulse")
    }

    func stopPulse() {
        statusDot.layer?.removeAnimation(forKey: "pulse")
        statusDot.layer?.opacity = 1.0
    }

    func updateTimer() {
        guard currentState == "RECORDING" || currentState == "PAUSED" else { timerLabel.stringValue = ""; return }
        let e = recordingStart > 0 ? Date().timeIntervalSince1970 - recordingStart : 0
        timerLabel.stringValue = String(format: "%02d:%02d", Int(e) / 60, Int(e) % 60)
    }

    // MARK: - Actions

    func writeControl(_ action: String) {
        let json = "{\"action\":\"\(action)\",\"timestamp\":\(Date().timeIntervalSince1970)}"
        try? json.write(toFile: controlPath, atomically: true, encoding: .utf8)
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.5) { [weak self] in self?.poll() }
    }

    @objc func doStop() { writeControl("end"); menu.cancelTracking() }
    @objc func doPause() { writeControl(currentState == "PAUSED" ? "resume" : "pause") }
    @objc func doRestart() { writeControl("end"); menu.cancelTracking() }

    @objc func doGoToFile() {
        guard !transcriptName.isEmpty else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: transcriptsDir + "/" + transcriptName))
        menu.cancelTracking()
    }

    func shortenPath(_ path: String) -> String {
        path.replacingOccurrences(of: homePath, with: "~")
    }

    @objc func modelSelected(_ sender: NSButton) {
        config.whisperModel = sender.title
        for btn in modelButtons {
            let isActive = (btn.title == sender.title)
            btn.font = .systemFont(ofSize: 10, weight: isActive ? .bold : .medium)
            btn.contentTintColor = isActive ? .white : kDimText
            btn.layer?.backgroundColor = isActive ? kBlue.withAlphaComponent(0.3).cgColor : kBtnBg.cgColor
            btn.layer?.borderWidth = isActive ? 1 : 0
            btn.normalColor = isActive ? kBlue.withAlphaComponent(0.3) : kBtnBg
        }
    }

    @objc func doBrowsePath() {
        menu.cancelTracking()
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
            guard let self = self else { return }
            let panel = NSOpenPanel()
            panel.canChooseDirectories = true
            panel.canChooseFiles = false
            panel.canCreateDirectories = true
            panel.prompt = "Select"
            panel.message = "Choose where to save meeting transcripts"
            panel.directoryURL = URL(fileURLWithPath: self.config.transcriptsPath)
            panel.appearance = NSAppearance(named: .darkAqua)
            if panel.runModal() == .OK, let url = panel.url {
                self.config.transcriptsPath = url.path
                self.pathDisplay.stringValue = self.shortenPath(url.path)
            }
        }
    }

    @objc func doSave() {
        // whisperModel already set by modelSelected, diarization read below
        config.diarization = diarizationToggle.state == .on
        config.save(home: homePath)

        menu.cancelTracking()

        // If recording, ask to restart
        if currentState == "RECORDING" || currentState == "PAUSED" {
            DispatchQueue.main.asyncAfter(deadline: .now() + 0.3) { [weak self] in
                let alert = NSAlert()
                alert.messageText = "Settings saved"
                alert.informativeText = "A recording is in progress. Restart now to apply changes, or apply after the recording ends?"
                alert.addButton(withTitle: "Restart Now")
                alert.addButton(withTitle: "Apply Later")
                alert.alertStyle = .informational
                alert.window.appearance = NSAppearance(named: .darkAqua)
                if alert.runModal() == .alertFirstButtonReturn {
                    self?.writeControl("end")
                }
            }
        }
    }
}

// MARK: - Main

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
let d = AppDelegate()
app.delegate = d
app.run()
