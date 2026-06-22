// Minimal CoreAudio CLI for creating Multi-Output Devices.
// Replaces the Node.js dependency (npx macos-audio-devices).
// Compile: swiftc -framework CoreAudio -framework CoreFoundation create-multi-output.swift -o create-multi-output

import CoreAudio
import Foundation

// MARK: - Device listing

struct AudioDevice {
    let id: AudioDeviceID
    let uid: String
    let name: String
    let isOutput: Bool
    let transportType: UInt32
}

func getStringProperty(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> String? {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &address, 0, nil, &size) == noErr, size > 0 else { return nil }
    var data = [UInt8](repeating: 0, count: Int(size))
    let status = data.withUnsafeMutableBufferPointer { buf in
        AudioObjectGetPropertyData(id, &address, 0, nil, &size, buf.baseAddress!)
    }
    guard status == noErr else { return nil }
    return data.withUnsafeBufferPointer { buf in
        let ptr = buf.baseAddress!.withMemoryRebound(to: CFString.self, capacity: 1) { $0 }
        return ptr.pointee as String
    }
}

func getUInt32Property(_ id: AudioObjectID, _ selector: AudioObjectPropertySelector) -> UInt32? {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var value: UInt32 = 0
    var size = UInt32(MemoryLayout<UInt32>.size)
    let status = AudioObjectGetPropertyData(id, &address, 0, nil, &size, &value)
    return status == noErr ? value : nil
}

func getChannelCount(_ id: AudioDeviceID, scope: AudioObjectPropertyScope) -> Int {
    var address = AudioObjectPropertyAddress(mSelector: kAudioDevicePropertyStreamConfiguration, mScope: scope, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(id, &address, 0, nil, &size) == noErr, size > 0 else { return 0 }
    let bufferList = UnsafeMutablePointer<AudioBufferList>.allocate(capacity: Int(size))
    defer { bufferList.deallocate() }
    guard AudioObjectGetPropertyData(id, &address, 0, nil, &size, bufferList) == noErr else { return 0 }
    var channels = 0
    let count = Int(bufferList.pointee.mNumberBuffers)
    let ptr = UnsafeMutableAudioBufferListPointer(bufferList)
    for i in 0..<count { channels += Int(ptr[i].mNumberChannels) }
    return channels
}

func listDevices() -> [AudioDevice] {
    var address = AudioObjectPropertyAddress(mSelector: kAudioHardwarePropertyDevices, mScope: kAudioObjectPropertyScopeGlobal, mElement: kAudioObjectPropertyElementMain)
    var size: UInt32 = 0
    guard AudioObjectGetPropertyDataSize(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size) == noErr else { return [] }
    let count = Int(size) / MemoryLayout<AudioDeviceID>.size
    var deviceIDs = [AudioDeviceID](repeating: 0, count: count)
    guard AudioObjectGetPropertyData(AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &deviceIDs) == noErr else { return [] }
    return deviceIDs.compactMap { id in
        guard let uid = getStringProperty(id, kAudioDevicePropertyDeviceUID),
              let name = getStringProperty(id, kAudioObjectPropertyName) else { return nil }
        let outputChannels = getChannelCount(id, scope: kAudioDevicePropertyScopeOutput)
        let transport = getUInt32Property(id, kAudioDevicePropertyTransportType) ?? 0
        return AudioDevice(id: id, uid: uid, name: name, isOutput: outputChannels > 0, transportType: transport)
    }
}

// MARK: - Multi-Output Device creation

func createMultiOutputDevice(name: String, subDeviceUIDs: [String]) -> Bool {
    let subDevices: [[String: Any]] = subDeviceUIDs.map { uid in
        [kAudioSubDeviceUIDKey: uid, kAudioSubDeviceDriftCompensationKey: 0] as [String: Any]
    }
    let description: [String: Any] = [
        kAudioAggregateDeviceNameKey: name,
        kAudioAggregateDeviceUIDKey: UUID().uuidString,
        kAudioAggregateDeviceSubDeviceListKey: subDevices,
        kAudioAggregateDeviceMasterSubDeviceKey: subDeviceUIDs[0],
        kAudioAggregateDeviceIsStackedKey: 1  // 1 = Multi-Output, 0 = Aggregate
    ]
    var deviceID: AudioDeviceID = 0
    let status = AudioHardwareCreateAggregateDevice(description as CFDictionary, &deviceID)
    if status == noErr {
        print("Created '\(name)' (ID: \(deviceID))")
        return true
    } else {
        fputs("Error creating device: OSStatus \(status)\n", stderr)
        return false
    }
}

// MARK: - Default output device

func getDefaultOutputDevice() -> AudioDevice? {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyDefaultOutputDevice,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var deviceID: AudioDeviceID = 0
    var size = UInt32(MemoryLayout<AudioDeviceID>.size)
    let status = AudioObjectGetPropertyData(
        AudioObjectID(kAudioObjectSystemObject), &address, 0, nil, &size, &deviceID
    )
    guard status == noErr, deviceID != 0 else { return nil }

    guard let uid = getStringProperty(deviceID, kAudioDevicePropertyDeviceUID),
          let name = getStringProperty(deviceID, kAudioObjectPropertyName) else { return nil }
    let outputChannels = getChannelCount(deviceID, scope: kAudioDevicePropertyScopeOutput)
    let transport = getUInt32Property(deviceID, kAudioDevicePropertyTransportType) ?? 0
    return AudioDevice(id: deviceID, uid: uid, name: name, isOutput: outputChannels > 0, transportType: transport)
}

// MARK: - JSON output

func devicesJSON(_ devices: [AudioDevice]) -> String {
    let entries = devices.map { d in
        """
        {"id":\(d.id),"uid":"\(d.uid)","name":"\(d.name.replacingOccurrences(of: "\"", with: "\\\""))","isOutput":\(d.isOutput),"transportType":\(d.transportType)}
        """
    }
    return "[\(entries.joined(separator: ","))]"
}

// MARK: - Main

let args = CommandLine.arguments
guard args.count >= 2 else {
    fputs("Usage:\n  \(args[0]) list [--json]\n  \(args[0]) create <name> <device-uid-1> <device-uid-2>\n", stderr)
    exit(1)
}

switch args[1] {
case "list":
    let devices = listDevices()
    if args.count >= 3 && args[2] == "--json" {
        print(devicesJSON(devices))
    } else {
        for d in devices where d.isOutput {
            print("[\(d.id)] \(d.name) (uid: \(d.uid), transport: \(d.transportType))")
        }
    }

case "default-output":
    if let dev = getDefaultOutputDevice() {
        print("\(dev.uid) \(dev.name)")
    } else {
        fputs("No default output device found\n", stderr)
        exit(1)
    }

case "create":
    guard args.count >= 5 else {
        fputs("Usage: \(args[0]) create <name> <device-uid-1> <device-uid-2>\n", stderr)
        exit(1)
    }
    let name = args[2]
    let uids = Array(args[3...])
    if !createMultiOutputDevice(name: name, subDeviceUIDs: uids) {
        exit(1)
    }

default:
    fputs("Unknown command: \(args[1])\n", stderr)
    exit(1)
}
