// zoom-participants.swift
// Reads Zoom meeting participant names via macOS Accessibility API.
//
// Build:
//   swiftc -O -framework AppKit zoom-participants.swift -o zoom-participants
//
// Usage:
//   ./zoom-participants              # one-shot: print JSON array of names
//   ./zoom-participants --poll 10    # poll every 10s, print JSON on each cycle
//   ./zoom-participants --open       # open Participants panel first (Cmd+U)
//
// Output: JSON array of participant names to stdout, one per line.
//   ["Alice", "Bob", "Charlie"]
//
// Requires: Accessibility permissions for the terminal/app running this.

import AppKit
import Foundation

// MARK: - Accessibility Helpers

func getZoomApp() -> AXUIElement? {
    let apps = NSWorkspace.shared.runningApplications
    guard let zoom = apps.first(where: { $0.bundleIdentifier == "us.zoom.xos" }) else {
        return nil
    }
    return AXUIElementCreateApplication(zoom.processIdentifier)
}

func getWindows(_ app: AXUIElement) -> [AXUIElement] {
    var value: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute as CFString, &value)
    guard err == .success, let windows = value as? [AXUIElement] else {
        return []
    }
    return windows
}

func getAttribute(_ element: AXUIElement, _ attr: String) -> CFTypeRef? {
    var value: CFTypeRef?
    let err = AXUIElementCopyAttributeValue(element, attr as CFString, &value)
    guard err == .success else { return nil }
    return value
}

func getStringAttribute(_ element: AXUIElement, _ attr: String) -> String? {
    guard let value = getAttribute(element, attr) else { return nil }
    return value as? String
}

func getChildren(_ element: AXUIElement) -> [AXUIElement] {
    guard let value = getAttribute(element, kAXChildrenAttribute) else { return [] }
    return value as? [AXUIElement] ?? []
}

func getRole(_ element: AXUIElement) -> String? {
    return getStringAttribute(element, kAXRoleAttribute)
}

// MARK: - Participant Extraction

/// Recursively search for participant names in the Accessibility tree.
/// Zoom's Participants panel uses AXOutline (NSOutlineView) with AXRow children,
/// each containing AXStaticText with the participant name.
func findParticipantNames(_ element: AXUIElement, depth: Int = 0) -> [String] {
    let maxDepth = 15
    if depth > maxDepth { return [] }

    let role = getRole(element)

    // AXOutline is the participant list container
    if role == "AXOutline" {
        return extractNamesFromOutline(element)
    }

    // Also check AXTable (Zoom sometimes uses this)
    if role == "AXTable" {
        return extractNamesFromTable(element)
    }

    // Recurse into children
    var names: [String] = []
    for child in getChildren(element) {
        names.append(contentsOf: findParticipantNames(child, depth: depth + 1))
    }
    return names
}

func extractNamesFromOutline(_ outline: AXUIElement) -> [String] {
    var names: [String] = []
    let rows = getChildren(outline)

    for row in rows {
        let rowRole = getRole(row)
        if rowRole == "AXRow" || rowRole == "AXCell" {
            if let name = extractNameFromRow(row) {
                names.append(name)
            }
        }
    }
    return names
}

func extractNamesFromTable(_ table: AXUIElement) -> [String] {
    var names: [String] = []
    let rows = getChildren(table)

    for row in rows {
        if let name = extractNameFromRow(row) {
            names.append(name)
        }
    }
    return names
}

func extractNameFromRow(_ row: AXUIElement) -> String? {
    // Try direct title/value first
    if let title = getStringAttribute(row, kAXTitleAttribute), !title.isEmpty {
        return cleanParticipantName(title)
    }
    if let value = getStringAttribute(row, kAXValueAttribute), !value.isEmpty {
        return cleanParticipantName(value)
    }

    // Search children for AXStaticText
    for child in getChildren(row) {
        let childRole = getRole(child)
        if childRole == "AXStaticText" || childRole == "AXTextField" {
            if let text = getStringAttribute(child, kAXValueAttribute), !text.isEmpty {
                return cleanParticipantName(text)
            }
            if let title = getStringAttribute(child, kAXTitleAttribute), !title.isEmpty {
                return cleanParticipantName(title)
            }
        }
        // One more level deep (AXCell -> AXStaticText)
        if childRole == "AXCell" || childRole == "AXGroup" {
            for grandchild in getChildren(child) {
                let gcRole = getRole(grandchild)
                if gcRole == "AXStaticText" || gcRole == "AXTextField" {
                    if let text = getStringAttribute(grandchild, kAXValueAttribute), !text.isEmpty {
                        return cleanParticipantName(text)
                    }
                    if let title = getStringAttribute(grandchild, kAXTitleAttribute), !title.isEmpty {
                        return cleanParticipantName(title)
                    }
                }
            }
        }
    }
    return nil
}

func cleanParticipantName(_ name: String) -> String {
    var clean = name
        .trimmingCharacters(in: .whitespacesAndNewlines)

    // Remove common suffixes Zoom adds
    let suffixes = [
        " (Host)", " (Хост)",
        " (Co-host)", " (Соведущий)",
        " (Me)", " (Я)",
        " (Guest)", " (Гость)",
    ]
    for suffix in suffixes {
        if clean.hasSuffix(suffix) {
            clean = String(clean.dropLast(suffix.count))
            break
        }
    }
    return clean
}

// MARK: - Participants Panel Detection

/// Find the Participants panel window/pane
func findParticipantsPanel(_ app: AXUIElement) -> AXUIElement? {
    let windows = getWindows(app)

    for window in windows {
        guard let title = getStringAttribute(window, kAXTitleAttribute) else { continue }
        let lower = title.lowercased()
        // English: "Participants", Russian: "Участники"
        if lower.contains("participant") || lower.contains("участник") {
            return window
        }
    }

    // Zoom 6.x: Participants may be embedded in the main meeting window as a panel
    // Search all windows for the outline/table
    for window in windows {
        let names = findParticipantNames(window)
        if !names.isEmpty {
            return window
        }
    }

    return nil
}

/// Open Participants panel via keyboard shortcut (Cmd+U)
func openParticipantsPanel() {
    let zoom = NSWorkspace.shared.runningApplications.first {
        $0.bundleIdentifier == "us.zoom.xos"
    }
    guard let zoom = zoom else { return }

    // Activate Zoom first
    zoom.activate()
    usleep(300_000) // 300ms for window focus

    // Send Cmd+U
    let src = CGEventSource(stateID: .combinedSessionState)
    let keyDown = CGEvent(keyboardEventSource: src, virtualKey: 0x20, keyDown: true) // U key
    let keyUp = CGEvent(keyboardEventSource: src, virtualKey: 0x20, keyDown: false)
    keyDown?.flags = .maskCommand
    keyUp?.flags = .maskCommand
    keyDown?.post(tap: .cghidEventTap)
    keyUp?.post(tap: .cghidEventTap)

    usleep(500_000) // 500ms for panel to open
}

// MARK: - Main

func readParticipants() -> [String] {
    guard let app = getZoomApp() else {
        return []
    }

    // Try to find participants panel
    guard let panel = findParticipantsPanel(app) else {
        return []
    }

    let names = findParticipantNames(panel)
    // Deduplicate while preserving order
    var seen = Set<String>()
    return names.filter { name in
        guard !name.isEmpty, !seen.contains(name) else { return false }
        seen.insert(name)
        return true
    }
}

func outputJSON(_ names: [String]) {
    let data = try! JSONSerialization.data(withJSONObject: names, options: [])
    let json = String(data: data, encoding: .utf8)!
    print(json)
    fflush(stdout)
}

// Parse args
var pollInterval: Double? = nil
var shouldOpen = false

var i = 1
while i < CommandLine.arguments.count {
    let arg = CommandLine.arguments[i]
    switch arg {
    case "--poll":
        i += 1
        if i < CommandLine.arguments.count {
            pollInterval = Double(CommandLine.arguments[i])
        }
    case "--open":
        shouldOpen = true
    case "--help", "-h":
        fputs("""
        Usage: zoom-participants [--poll SECONDS] [--open]
          --poll N    Poll every N seconds (default: one-shot)
          --open      Open Participants panel first (Cmd+U)
          -h, --help  Show this help

        Output: JSON array of participant names to stdout.
        Requires: Accessibility permissions.

        """, stderr)
        exit(0)
    default:
        fputs("Unknown argument: \(arg)\n", stderr)
        exit(1)
    }
    i += 1
}

// Open panel if requested
if shouldOpen {
    openParticipantsPanel()
}

// One-shot or poll mode
if let interval = pollInterval {
    // Poll mode: keep running
    signal(SIGINT) { _ in exit(0) }
    signal(SIGTERM) { _ in exit(0) }

    while true {
        let names = readParticipants()
        outputJSON(names)
        Thread.sleep(forTimeInterval: interval)
    }
} else {
    // One-shot
    let names = readParticipants()
    outputJSON(names)
}
