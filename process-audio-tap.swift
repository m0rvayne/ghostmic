// process-audio-tap.swift
// Captures audio from a macOS app via CoreAudio Process Taps (macOS 14.4+)
// and outputs raw PCM (16-bit signed LE, 16000 Hz, mono) to stdout.
//
// Build:
//   swiftc -O -framework CoreAudio -framework AudioToolbox -framework AppKit \
//     process-audio-tap.swift -o process-audio-tap
//
// Usage:
//   ./process-audio-tap --bundle-id com.google.Chrome
//   ./process-audio-tap --pid 12345
//   ./process-audio-tap --bundle-id us.zoom.xos --wait
//
// The --wait flag will keep trying until the process appears (useful for
// processes that haven't launched yet).
//
// Output goes to stdout. Pipe to a file or another process:
//   ./process-audio-tap --bundle-id com.google.Chrome > audio.pcm
//   ./process-audio-tap --bundle-id com.google.Chrome | ffmpeg -f s16le -ar 16000 -ac 1 -i - out.wav

import Foundation
import CoreAudio
import AudioToolbox
import AppKit
import Darwin

// MARK: - Audio Object Helpers

extension AudioObjectID {
    static let system = AudioObjectID(kAudioObjectSystemObject)
    static let unknown = kAudioObjectUnknown
    var isValid: Bool { self != Self.unknown }
}

func readPropertyData<T>(_ objectID: AudioObjectID,
                         selector: AudioObjectPropertySelector,
                         scope: AudioObjectPropertyScope = kAudioObjectPropertyScopeGlobal,
                         element: AudioObjectPropertyElement = kAudioObjectPropertyElementMain,
                         defaultValue: T) throws -> T {
    var address = AudioObjectPropertyAddress(mSelector: selector, mScope: scope, mElement: element)
    var dataSize = UInt32(MemoryLayout<T>.size)
    var value = defaultValue
    let err = withUnsafeMutablePointer(to: &value) { ptr in
        AudioObjectGetPropertyData(objectID, &address, 0, nil, &dataSize, ptr)
    }
    guard err == noErr else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(err)) }
    return value
}

func readPropertyDataWithQualifier<T, Q>(_ objectID: AudioObjectID,
                                          selector: AudioObjectPropertySelector,
                                          qualifier: Q,
                                          defaultValue: T) throws -> T {
    var address = AudioObjectPropertyAddress(
        mSelector: selector,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize = UInt32(MemoryLayout<T>.size)
    var value = defaultValue
    var qual = qualifier
    let qualSize = UInt32(MemoryLayout<Q>.size)
    let err = withUnsafeMutablePointer(to: &value) { valPtr in
        withUnsafeMutablePointer(to: &qual) { qualPtr in
            AudioObjectGetPropertyData(objectID, &address, qualSize, qualPtr, &dataSize, valPtr)
        }
    }
    guard err == noErr else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(err)) }
    return value
}

func readPropertyString(_ objectID: AudioObjectID,
                        selector: AudioObjectPropertySelector) throws -> String {
    let cf: CFString = try readPropertyData(objectID, selector: selector, defaultValue: "" as CFString)
    return cf as String
}

func readProcessList() throws -> [AudioObjectID] {
    var address = AudioObjectPropertyAddress(
        mSelector: kAudioHardwarePropertyProcessObjectList,
        mScope: kAudioObjectPropertyScopeGlobal,
        mElement: kAudioObjectPropertyElementMain
    )
    var dataSize: UInt32 = 0
    var err = AudioObjectGetPropertyDataSize(AudioObjectID.system, &address, 0, nil, &dataSize)
    guard err == noErr else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(err)) }
    let count = Int(dataSize) / MemoryLayout<AudioObjectID>.size
    var result = [AudioObjectID](repeating: .unknown, count: count)
    err = AudioObjectGetPropertyData(AudioObjectID.system, &address, 0, nil, &dataSize, &result)
    guard err == noErr else { throw NSError(domain: NSOSStatusErrorDomain, code: Int(err)) }
    return result
}

func translatePIDToObjectID(_ pid: pid_t) throws -> AudioObjectID {
    let objectID: AudioObjectID = try readPropertyDataWithQualifier(
        AudioObjectID.system,
        selector: kAudioHardwarePropertyTranslatePIDToProcessObject,
        qualifier: pid,
        defaultValue: AudioObjectID.unknown
    )
    guard objectID.isValid else { throw "No audio object for PID \(pid)" }
    return objectID
}

func readDefaultOutputDeviceUID() throws -> String {
    let deviceID: AudioDeviceID = try readPropertyData(
        AudioObjectID.system,
        selector: kAudioHardwarePropertyDefaultSystemOutputDevice,
        defaultValue: AudioObjectID.unknown
    )
    guard deviceID.isValid else { throw "No default output device" }
    return try readPropertyString(deviceID, selector: kAudioDevicePropertyDeviceUID)
}

func readTapFormat(_ tapID: AudioObjectID) throws -> AudioStreamBasicDescription {
    return try readPropertyData(tapID, selector: kAudioTapPropertyFormat, defaultValue: AudioStreamBasicDescription())
}

// MARK: - Process Lookup

/// Find a running process by bundle identifier. Returns the PID.
func findProcessByBundleID(_ bundleID: String) -> pid_t? {
    let apps = NSRunningApplication.runningApplications(withBundleIdentifier: bundleID)
    return apps.first?.processIdentifier
}

/// Find the CoreAudio object ID for a process, trying by PID first, then
/// scanning the audio process list for a matching bundle ID.
func findAudioObjectID(pid: pid_t?, bundleID: String?) throws -> AudioObjectID {
    // Try direct PID translation first
    if let pid = pid {
        if let objID = try? translatePIDToObjectID(pid), objID.isValid {
            return objID
        }
    }

    // Scan audio process list for matching bundle ID or PID
    let processList = try readProcessList()
    for objectID in processList {
        if let bundleID = bundleID {
            if let procBundle = try? readPropertyString(objectID, selector: kAudioProcessPropertyBundleID),
               procBundle == bundleID {
                return objectID
            }
        }
        if let pid = pid {
            if let procPID: pid_t = try? readPropertyData(objectID, selector: kAudioProcessPropertyPID, defaultValue: -1),
               procPID == pid {
                return objectID
            }
        }
    }

    throw "Process not found in audio system"
}

// MARK: - Audio Converter (Resampler)

/// A simple wrapper around AudioConverter to convert from the tap's native
/// format (typically 44100/48000 Hz, Float32, stereo) to 16000 Hz, Int16, mono.
class PCMConverter {
    private var converter: AudioConverterRef?
    private let inputFormat: AudioStreamBasicDescription
    private let outputFormat: AudioStreamBasicDescription

    // Ring buffer for input data
    private var inputBuffer = Data()
    private let lock = NSLock()

    init(inputFormat: AudioStreamBasicDescription) throws {
        self.inputFormat = inputFormat

        var outFmt = AudioStreamBasicDescription(
            mSampleRate: 16000,
            mFormatID: kAudioFormatLinearPCM,
            mFormatFlags: kLinearPCMFormatFlagIsSignedInteger | kAudioFormatFlagIsPacked,
            mBytesPerPacket: 2,
            mFramesPerPacket: 1,
            mBytesPerFrame: 2,
            mChannelsPerFrame: 1,
            mBitsPerChannel: 16,
            mReserved: 0
        )
        self.outputFormat = outFmt

        var inFmt = inputFormat
        var conv: AudioConverterRef?
        let err = AudioConverterNew(&inFmt, &outFmt, &conv)
        guard err == noErr, let c = conv else {
            throw "AudioConverterNew failed: \(err)"
        }
        self.converter = c

        // Set conversion quality
        var quality = UInt32(kAudioConverterQuality_Medium)
        AudioConverterSetProperty(c,
                                  kAudioConverterSampleRateConverterQuality,
                                  UInt32(MemoryLayout<UInt32>.size),
                                  &quality)
    }

    deinit {
        if let converter = converter {
            AudioConverterDispose(converter)
        }
    }

    /// Feed input audio data and get back converted Int16 PCM at 16kHz mono.
    func convert(inputData: UnsafeRawPointer, inputByteCount: Int) -> Data? {
        guard let converter = converter else { return nil }

        lock.lock()
        inputBuffer.append(Data(bytes: inputData, count: inputByteCount))
        lock.unlock()

        // Calculate how many output frames we can produce
        let inputBytesPerFrame = Int(inputFormat.mBytesPerFrame)
        guard inputBytesPerFrame > 0 else { return nil }

        lock.lock()
        let availableInputFrames = inputBuffer.count / inputBytesPerFrame
        lock.unlock()

        guard availableInputFrames > 0 else { return nil }

        // Estimate output frames based on sample rate ratio
        let ratio = outputFormat.mSampleRate / inputFormat.mSampleRate
        let estimatedOutputFrames = UInt32(Double(availableInputFrames) * ratio) + 1
        let outputByteCount = Int(estimatedOutputFrames) * Int(outputFormat.mBytesPerFrame)

        var outputData = Data(count: outputByteCount)
        var outputBufferList = AudioBufferList(
            mNumberBuffers: 1,
            mBuffers: AudioBuffer(
                mNumberChannels: 1,
                mDataByteSize: UInt32(outputByteCount),
                mData: nil
            )
        )

        var outputFrameCount = estimatedOutputFrames

        // Use the complex input data proc pattern
        struct UserData {
            var inputData: Data
            var inputFormat: AudioStreamBasicDescription
            var consumed: Int
        }

        var userData = UserData(inputData: Data(), inputFormat: inputFormat, consumed: 0)
        lock.lock()
        userData.inputData = inputBuffer
        lock.unlock()

        let result: Data? = outputData.withUnsafeMutableBytes { outputRawPtr in
            guard let outputBaseAddress = outputRawPtr.baseAddress else { return nil }
            outputBufferList.mBuffers.mData = outputBaseAddress

            let err = withUnsafeMutablePointer(to: &userData) { userDataPtr in
                AudioConverterFillComplexBuffer(
                    converter,
                    { (
                        _: AudioConverterRef,
                        ioNumberDataPackets: UnsafeMutablePointer<UInt32>,
                        ioData: UnsafeMutablePointer<AudioBufferList>,
                        _: UnsafeMutablePointer<UnsafeMutablePointer<AudioStreamPacketDescription>?>?,
                        inUserData: UnsafeMutableRawPointer?
                    ) -> OSStatus in
                        guard let inUserData = inUserData else {
                            ioNumberDataPackets.pointee = 0
                            return -50 // paramErr
                        }
                        let userData = inUserData.assumingMemoryBound(to: UserData.self)
                        let bytesPerFrame = Int(userData.pointee.inputFormat.mBytesPerFrame)
                        let availableBytes = userData.pointee.inputData.count - userData.pointee.consumed
                        let availableFrames = availableBytes / bytesPerFrame
                        let framesToProvide = min(Int(ioNumberDataPackets.pointee), availableFrames)

                        if framesToProvide == 0 {
                            ioNumberDataPackets.pointee = 0
                            return 100 // not enough data, stop
                        }

                        let byteCount = framesToProvide * bytesPerFrame
                        let offset = userData.pointee.consumed

                        userData.pointee.inputData.withUnsafeBytes { rawBuf in
                            let src = rawBuf.baseAddress!.advanced(by: offset)
                            ioData.pointee.mNumberBuffers = 1
                            ioData.pointee.mBuffers.mNumberChannels = userData.pointee.inputFormat.mChannelsPerFrame
                            ioData.pointee.mBuffers.mDataByteSize = UInt32(byteCount)
                            ioData.pointee.mBuffers.mData = UnsafeMutableRawPointer(mutating: src)
                        }

                        userData.pointee.consumed += byteCount
                        ioNumberDataPackets.pointee = UInt32(framesToProvide)
                        return noErr
                    },
                    userDataPtr,
                    &outputFrameCount,
                    &outputBufferList,
                    nil
                )
            }

            if err != noErr && err != 100 {
                return nil
            }

            let producedBytes = Int(outputFrameCount) * Int(self.outputFormat.mBytesPerFrame)
            return Data(bytes: outputBaseAddress, count: producedBytes)
        }

        // Remove consumed input
        lock.lock()
        if userData.consumed > 0 {
            inputBuffer.removeFirst(min(userData.consumed, inputBuffer.count))
        }
        lock.unlock()

        return result
    }
}

// MARK: - Process Tap Manager

class ProcessAudioTap {
    let pid: pid_t?
    let bundleID: String?
    let waitForProcess: Bool

    private var processTapID: AudioObjectID = .unknown
    private var aggregateDeviceID: AudioObjectID = .unknown
    private var deviceProcID: AudioDeviceIOProcID?
    private var converter: PCMConverter?
    private let queue = DispatchQueue(label: "audio-tap-io", qos: .userInitiated)

    init(pid: pid_t? = nil, bundleID: String? = nil, waitForProcess: Bool = false) {
        self.pid = pid
        self.bundleID = bundleID
        self.waitForProcess = waitForProcess
    }

    func start() throws {
        var resolvedPID = pid

        // If we have a bundle ID but no PID, look it up
        if resolvedPID == nil, let bundleID = bundleID {
            if let foundPID = findProcessByBundleID(bundleID) {
                resolvedPID = foundPID
            } else if waitForProcess {
                fputs("Waiting for process with bundle ID \(bundleID)...\n", stderr)
                while resolvedPID == nil {
                    Thread.sleep(forTimeInterval: 1.0)
                    resolvedPID = findProcessByBundleID(bundleID)
                }
                fputs("Found process with PID \(resolvedPID!)\n", stderr)
                // Give the process a moment to register with CoreAudio
                Thread.sleep(forTimeInterval: 2.0)
            } else {
                throw "Process with bundle ID \(bundleID) not running. Use --wait to wait for it."
            }
        }

        guard resolvedPID != nil || bundleID != nil else {
            throw "Must specify --pid or --bundle-id"
        }

        // Find the CoreAudio object for this process
        var audioObjectID: AudioObjectID = .unknown
        var attempts = 0
        let maxAttempts = waitForProcess ? 30 : 3

        while !audioObjectID.isValid && attempts < maxAttempts {
            do {
                audioObjectID = try findAudioObjectID(pid: resolvedPID, bundleID: bundleID)
            } catch {
                attempts += 1
                if attempts >= maxAttempts {
                    throw "Could not find audio object for process after \(attempts) attempts: \(error)"
                }
                fputs("Waiting for process to appear in audio system (attempt \(attempts)/\(maxAttempts))...\n", stderr)
                Thread.sleep(forTimeInterval: 1.0)
            }
        }

        fputs("Audio object ID: \(audioObjectID)\n", stderr)

        // 1. Create the process tap
        let tapDescription = CATapDescription(stereoMixdownOfProcesses: [audioObjectID])
        tapDescription.uuid = UUID()
        tapDescription.muteBehavior = .unmuted

        var tapID: AudioObjectID = .unknown
        var err = AudioHardwareCreateProcessTap(tapDescription, &tapID)
        guard err == noErr else {
            throw "AudioHardwareCreateProcessTap failed: \(err)"
        }
        self.processTapID = tapID
        fputs("Created process tap: \(tapID)\n", stderr)

        // 2. Read the tap's native format
        let tapFormat = try readTapFormat(tapID)
        fputs("Tap format: \(tapFormat.mSampleRate) Hz, \(tapFormat.mChannelsPerFrame) ch, \(tapFormat.mBitsPerChannel) bits, flags=\(tapFormat.mFormatFlags)\n", stderr)

        // 3. Create the converter from tap format to 16kHz/mono/Int16
        self.converter = try PCMConverter(inputFormat: tapFormat)

        // 4. Get default output device UID for aggregate device
        let outputUID = try readDefaultOutputDeviceUID()

        // 5. Create aggregate device with the tap
        let aggregateUID = UUID().uuidString
        let aggregateDescription: [String: Any] = [
            kAudioAggregateDeviceNameKey: "ProcessAudioTap-\(resolvedPID ?? 0)",
            kAudioAggregateDeviceUIDKey: aggregateUID,
            kAudioAggregateDeviceMainSubDeviceKey: outputUID,
            kAudioAggregateDeviceIsPrivateKey: true,
            kAudioAggregateDeviceIsStackedKey: false,
            kAudioAggregateDeviceTapAutoStartKey: true,
            kAudioAggregateDeviceSubDeviceListKey: [
                [kAudioSubDeviceUIDKey: outputUID]
            ],
            kAudioAggregateDeviceTapListKey: [
                [
                    kAudioSubTapDriftCompensationKey: true,
                    kAudioSubTapUIDKey: tapDescription.uuid.uuidString
                ]
            ]
        ]

        var aggDeviceID: AudioObjectID = .unknown
        err = AudioHardwareCreateAggregateDevice(aggregateDescription as CFDictionary, &aggDeviceID)
        guard err == noErr else {
            throw "AudioHardwareCreateAggregateDevice failed: \(err)"
        }
        self.aggregateDeviceID = aggDeviceID
        fputs("Created aggregate device: \(aggDeviceID)\n", stderr)

        // 6. Set up I/O proc to receive audio buffers
        let converterRef = self.converter!
        let stdout = FileHandle.standardOutput

        err = AudioDeviceCreateIOProcIDWithBlock(&deviceProcID, aggregateDeviceID, queue) {
            inNow, inInputData, inInputTime, outOutputData, inOutputTime in

            let bufferList = UnsafeMutableAudioBufferListPointer(UnsafeMutablePointer(mutating: inInputData))

            var totalBytes = 0
            for buf in bufferList {
                let dataSize = Int(buf.mDataByteSize)
                guard let mData = buf.mData, dataSize > 0 else { continue }
                totalBytes += dataSize

                if let converted = converterRef.convert(inputData: mData, inputByteCount: dataSize) {
                    if !converted.isEmpty {
                        stdout.write(converted)
                    }
                }
            }
            // Debug: log first few callbacks
            struct CallbackCounter { static var count = 0 }
            CallbackCounter.count += 1
            if CallbackCounter.count <= 5 {
                fputs("Callback #\(CallbackCounter.count): \(totalBytes) bytes in \(bufferList.count) buffers\n", stderr)
            }
        }
        guard err == noErr else {
            throw "AudioDeviceCreateIOProcIDWithBlock failed: \(err)"
        }

        // 7. Start the device
        err = AudioDeviceStart(aggregateDeviceID, deviceProcID)
        guard err == noErr else {
            throw "AudioDeviceStart failed: \(err)"
        }

        fputs("Streaming audio to stdout (16-bit LE, 16000 Hz, mono). Press Ctrl+C to stop.\n", stderr)
    }

    func stop() {
        if aggregateDeviceID.isValid {
            AudioDeviceStop(aggregateDeviceID, deviceProcID)
            if let deviceProcID = deviceProcID {
                AudioDeviceDestroyIOProcID(aggregateDeviceID, deviceProcID)
                self.deviceProcID = nil
            }
            AudioHardwareDestroyAggregateDevice(aggregateDeviceID)
            aggregateDeviceID = .unknown
        }
        if processTapID.isValid {
            AudioHardwareDestroyProcessTap(processTapID)
            processTapID = .unknown
        }
        fputs("Stopped.\n", stderr)
    }
}

// MARK: - String error conformance

extension String: @retroactive LocalizedError {
    public var errorDescription: String? { self }
}

extension String: @retroactive Error {}

// MARK: - Signal Handling & Main

var globalTap: ProcessAudioTap?
var shouldStop = false

func setupSignalHandlers() {
    let handler: @convention(c) (Int32) -> Void = { _ in
        shouldStop = true
    }
    signal(SIGINT, handler)
    signal(SIGTERM, handler)
}

func printUsage() {
    fputs("""
    process-audio-tap: Capture audio from a macOS app via CoreAudio Process Taps

    Usage:
      process-audio-tap --bundle-id <bundle-identifier> [--wait]
      process-audio-tap --pid <process-id> [--wait]

    Options:
      --bundle-id <id>   Bundle identifier (e.g. com.google.Chrome, us.zoom.xos)
      --pid <pid>        Process ID
      --wait             Wait for the process if it's not running yet

    Output:
      Raw PCM to stdout: signed 16-bit little-endian, 16000 Hz, mono

    Examples:
      process-audio-tap --bundle-id com.google.Chrome > audio.pcm
      process-audio-tap --bundle-id us.zoom.xos --wait | whisper -

    The binary needs "Screen Recording" (audio capture) permission on macOS.
    On first run, macOS will prompt you to grant permission to Terminal
    (or whichever app runs this binary).

    """, stderr)
}

// Parse arguments
var bundleID: String?
var pid: pid_t?
var waitForProcess = false

let args = CommandLine.arguments
var i = 1
while i < args.count {
    switch args[i] {
    case "--bundle-id":
        i += 1
        guard i < args.count else {
            fputs("Error: --bundle-id requires an argument\n", stderr)
            exit(1)
        }
        bundleID = args[i]
    case "--pid":
        i += 1
        guard i < args.count, let p = Int32(args[i]) else {
            fputs("Error: --pid requires a numeric argument\n", stderr)
            exit(1)
        }
        pid = p
    case "--wait":
        waitForProcess = true
    case "--help", "-h":
        printUsage()
        exit(0)
    default:
        fputs("Unknown argument: \(args[i])\n", stderr)
        printUsage()
        exit(1)
    }
    i += 1
}

if bundleID == nil && pid == nil {
    printUsage()
    exit(1)
}

setupSignalHandlers()

let tap = ProcessAudioTap(pid: pid, bundleID: bundleID, waitForProcess: waitForProcess)
globalTap = tap

do {
    try tap.start()
} catch {
    fputs("Error: \(error)\n", stderr)
    exit(1)
}

// Poll for shutdown signal; audio flows on the I/O proc callback thread
while !shouldStop {
    Thread.sleep(forTimeInterval: 0.2)
}
tap.stop()
exit(0)
