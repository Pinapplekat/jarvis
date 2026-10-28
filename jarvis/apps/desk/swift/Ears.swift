// Falak Ears: the desk's microphone, speakers and push-to-talk key, in one small app that holds the Microphone
// permission. Nothing is recorded: the microphone goes out as frames, and playback comes in as frames, over a Unix
// socket the node listens on (`--socket <path>`; stdin and stdout when there's none, for the self-test).
//
// Frames, both ways: one type byte (1 audio, 2 JSON line), a 4-byte big-endian length, the payload.
//   out: audio = 16 kHz mono Int16 from the microphone, 480 samples (30 ms) at a time, after macOS's echo
//        cancellation; JSON events: ready, error, hotkey, played, idle
//   in:  audio = 24 kHz mono Int16 for the speakers; JSON commands: stop, chime, hotkey, quit
import AppKit
import AVFoundation
import Carbon.HIToolbox

let inRate = 16000.0, outRate = 24000.0
let frameSamples = 480

// ---- framing -----------------------------------------------------------------------------------------------

// The node's socket. `open --stdin/--stdout` with named pipes hung LaunchServices (opening a pipe waits for the
// other end), which stopped every other app from launching, so the app connects to the node instead.
func connectSocket(_ path: String) -> FileHandle? {
    let fd = socket(AF_UNIX, SOCK_STREAM, 0)
    guard fd >= 0 else { return nil }
    var addr = sockaddr_un()
    addr.sun_family = sa_family_t(AF_UNIX)
    let bytes = Array(path.utf8)
    guard bytes.count < MemoryLayout.size(ofValue: addr.sun_path) else { close(fd); return nil }
    withUnsafeMutableBytes(of: &addr.sun_path) { raw in
        for (i, b) in bytes.enumerated() { raw[i] = b }
        raw[bytes.count] = 0
    }
    let ok = withUnsafePointer(to: &addr) {
        $0.withMemoryRebound(to: sockaddr.self, capacity: 1) { connect(fd, $0, socklen_t(MemoryLayout<sockaddr_un>.size)) }
    }
    guard ok == 0 else { close(fd); return nil }
    return FileHandle(fileDescriptor: fd, closeOnDealloc: true)
}

var link: (input: FileHandle, output: FileHandle) = (FileHandle.standardInput, FileHandle.standardOutput)
if let i = CommandLine.arguments.firstIndex(of: "--socket"), i + 1 < CommandLine.arguments.count {
    guard let handle = connectSocket(CommandLine.arguments[i + 1]) else { exit(1) }  // the node isn't listening
    link = (handle, handle)
}

let outLock = NSLock()
func emit(_ kind: UInt8, _ payload: Data) {
    var head = Data([kind])
    var n = UInt32(payload.count).bigEndian
    head.append(Data(bytes: &n, count: 4))
    outLock.lock(); defer { outLock.unlock() }
    link.output.write(head + payload)
}
func event(_ fields: [String: Any]) {
    if let data = try? JSONSerialization.data(withJSONObject: fields) { emit(2, data + Data("\n".utf8)) }
}
func fail(_ what: String) { event(["event": "error", "what": what]) }

// ---- speakers ---------------------------------------------------------------------------------------------

var engine = AVAudioEngine()  // the speakers; with echo cancellation, the microphone too (it needs both ends together)
var micEngine: AVAudioEngine?  // the microphone alone, when there's no echo cancellation: started only when needed
var echoCancelling = false
var micWanted = !CommandLine.arguments.contains("--mic-off")  // the node says when; off = no microphone light at all
var micRunning = false
let player = AVAudioPlayerNode()
let playFormat = AVAudioFormat(commonFormat: .pcmFormatFloat32, sampleRate: outRate, channels: 1, interleaved: false)!
var scheduled = 0, finished = 0
let countLock = NSLock()

func playedMs() -> Int {
    guard let last = player.lastRenderTime, let t = player.playerTime(forNodeTime: last) else { return 0 }
    return max(0, Int(Double(t.sampleTime) / outRate * 1000))
}

func play(_ pcm: Data) {
    let count = pcm.count / 2
    guard count > 0, let buffer = AVAudioPCMBuffer(pcmFormat: playFormat, frameCapacity: AVAudioFrameCount(count)) else { return }
    buffer.frameLength = AVAudioFrameCount(count)
    pcm.withUnsafeBytes { raw in
        let samples = raw.bindMemory(to: Int16.self)
        for i in 0..<count { buffer.floatChannelData![0][i] = Float(Int16(littleEndian: samples[i])) / 32768.0 }
    }
    countLock.lock(); scheduled += 1; countLock.unlock()
    player.scheduleBuffer(buffer, completionCallbackType: .dataPlayedBack) { _ in
        countLock.lock(); finished += 1; let drained = finished == scheduled; countLock.unlock()
        if drained { event(["event": "idle", "ms": playedMs()]) }
    }
    if echoCancelling && !engine.isRunning {  // speaking needs the shared engine, so the microphone comes on with it
        try? engine.start(); micRunning = true
    }
    if !player.isPlaying { player.play() }
}

func stopPlayback() {
    player.stop()  // drops everything queued; the completion handlers of dropped buffers still run
    countLock.lock(); scheduled = 0; finished = 0; countLock.unlock()
    event(["event": "idle", "ms": playedMs()])
}

// ---- microphone ---------------------------------------------------------------------------------------------

var pending = [Int16]()

func attachPlayer(_ e: AVAudioEngine) {
    if let old = player.engine { old.detach(player) }
    e.attach(player)
    e.connect(player, to: e.mainMixerNode, format: playFormat)
}

func tap(_ input: AVAudioInputNode) throws {
    input.removeTap(onBus: 0)
    let format = input.outputFormat(forBus: 0)
    guard let target = AVAudioFormat(commonFormat: .pcmFormatInt16, sampleRate: inRate, channels: 1, interleaved: true),
          let converter = AVAudioConverter(from: format, to: target) else {
        throw NSError(domain: "FalakEars", code: 1, userInfo: [NSLocalizedDescriptionKey: "no usable microphone format"])
    }
    input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
        let capacity = AVAudioFrameCount(Double(buffer.frameLength) * inRate / format.sampleRate) + 16
        guard let out = AVAudioPCMBuffer(pcmFormat: target, frameCapacity: capacity) else { return }
        var fed = false
        converter.convert(to: out, error: nil) { _, status in
            if fed { status.pointee = .noDataNow; return nil }
            fed = true; status.pointee = .haveData; return buffer
        }
        let samples = UnsafeBufferPointer(start: out.int16ChannelData![0], count: Int(out.frameLength))
        pending.append(contentsOf: samples)
        while pending.count >= frameSamples {
            let frame = pending.prefix(frameSamples).map { $0.littleEndian }
            pending.removeFirst(frameSamples)
            emit(1, frame.withUnsafeBufferPointer { Data(buffer: $0) })
        }
    }
}

func startWithEchoCancellation() throws {
    let input = engine.inputNode
    _ = engine.outputNode  // both ends exist before voice processing is switched, so they're set up together
    try input.setVoiceProcessingEnabled(true)
    attachPlayer(engine)
    try tap(input)
    engine.prepare()
    try engine.start()
    (echoCancelling, micRunning) = (true, true)
    if !micWanted { micOff() }
}

func startPlain() throws {
    engine.stop()
    engine = AVAudioEngine()  // a fresh one: a refused echo-cancellation attempt leaves the old one half set up
    attachPlayer(engine)  // the speakers only: the input node is never touched here, so macOS shows no microphone
    engine.prepare()
    try engine.start()
    echoCancelling = false
    if micWanted { try micOn() }
}

func micOn() throws {
    if micRunning { return }
    if echoCancelling {
        try engine.start()
    } else {
        let e = AVAudioEngine()
        try tap(e.inputNode)
        e.prepare()
        try e.start()
        micEngine = e
    }
    micRunning = true
}

func micOff() {
    guard micRunning else { return }
    if echoCancelling {
        if player.isPlaying { return }  // one engine: stopping it would cut Falak off; the next idle turns it off
        engine.stop()
    } else {
        micEngine?.inputNode.removeTap(onBus: 0)
        micEngine?.stop()
        micEngine = nil
    }
    pending.removeAll()
    micRunning = false
}

func setMic(_ on: Bool) {
    micWanted = on
    if on {
        do { try micOn() } catch { fail("couldn't start the microphone: \(error)") }
    } else {
        micOff()
    }
    event(["event": "mic", "on": micRunning])
}

func startMicrophone() {
    // Echo cancellation first. Some setups refuse it (-10875 while setting up the output); the desk still works
    // without it, and the node then stops treating speech during Falak's own voice as an interruption.
    do {
        try startWithEchoCancellation()
        event(["event": "ready", "echo_cancellation": true, "mic": micRunning])
        return
    } catch {
        event(["event": "notice", "what": "echo cancellation unavailable: \(error)"])
    }
    do {
        try startPlain()
        event(["event": "ready", "echo_cancellation": false, "mic": micRunning])
    } catch { fail("couldn't start audio: \(error)") }
}

func requestMicrophone() {
    switch AVCaptureDevice.authorizationStatus(for: .audio) {
    case .authorized: startMicrophone()
    case .notDetermined:
        AVCaptureDevice.requestAccess(for: .audio) { ok in
            DispatchQueue.main.async { ok ? startMicrophone() : fail("microphone not allowed") }
        }
    default: fail("microphone not allowed")
    }
}

// ---- push-to-talk key (Carbon: no Accessibility permission needed) ---------------------------------------

var hotKeyRef: EventHotKeyRef?
let keyCodes: [String: Int] = ["space": kVK_Space, "return": kVK_Return, "tab": kVK_Tab, "f13": kVK_F13, "f14": kVK_F14,
                               "f15": kVK_F15, "f16": kVK_F16, "f17": kVK_F17, "f18": kVK_F18, "f19": kVK_F19]
let modifierBits: [String: Int] = ["option": optionKey, "alt": optionKey, "command": cmdKey, "cmd": cmdKey,
                                   "control": controlKey, "ctrl": controlKey, "shift": shiftKey]

func installHotKeyHandler() {
    var spec = [EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyPressed)),
                EventTypeSpec(eventClass: OSType(kEventClassKeyboard), eventKind: UInt32(kEventHotKeyReleased))]
    InstallEventHandler(GetApplicationEventTarget(), { _, ev, _ in
        let down = GetEventKind(ev) == UInt32(kEventHotKeyPressed)
        event(["event": "hotkey", "action": down ? "down" : "up"])
        return noErr
    }, 2, &spec, nil, nil)
}

func registerHotKey(_ combo: String) {
    if let ref = hotKeyRef { UnregisterEventHotKey(ref); hotKeyRef = nil }
    var modifiers = 0, code: Int?
    for part in combo.lowercased().split(separator: "+").map(String.init) {
        if let bit = modifierBits[part] { modifiers |= bit } else { code = keyCodes[part] }
    }
    guard let code = code else { fail("unknown push-to-talk key: \(combo)"); return }
    let id = EventHotKeyID(signature: OSType(0x46414c4b), id: 1)  // 'FALK'
    let status = RegisterEventHotKey(UInt32(code), UInt32(modifiers), id, GetApplicationEventTarget(), 0, &hotKeyRef)
    if status != noErr { fail("couldn't register the push-to-talk key \(combo) (\(status))") }
}

// ---- commands from the node ---------------------------------------------------------------------------------

func chime(_ name: String) {
    let sound = NSSound(contentsOfFile: "/System/Library/Sounds/\(name == "start" ? "Tink" : "Pop").aiff", byReference: true)
    sound?.volume = 0.25
    sound?.play()
}

func handle(_ command: [String: Any]) {
    switch command["cmd"] as? String {
    case "stop": DispatchQueue.main.async { stopPlayback() }
    case "chime": DispatchQueue.main.async { chime(command["name"] as? String ?? "start") }
    case "hotkey": DispatchQueue.main.async { registerHotKey(command["key"] as? String ?? "option+space") }
    case "mic": DispatchQueue.main.async { setMic(command["on"] as? Bool ?? true) }
    case "quit": exit(0)
    default: break
    }
}

func readInput() {
    let input = link.input
    func exactly(_ n: Int) -> Data? {
        var data = Data()
        while data.count < n {
            let part = input.readData(ofLength: n - data.count)
            if part.isEmpty { return nil }
            data.append(part)
        }
        return data
    }
    while let head = exactly(5) {
        let kind = head[0]
        let length = Int(UInt32(bigEndian: head.subdata(in: 1..<5).withUnsafeBytes { $0.loadUnaligned(as: UInt32.self) }))
        guard length < (1 << 20), let payload = length == 0 ? Data() : exactly(length) else { break }
        if kind == 1 { play(payload) }
        else if let object = try? JSONSerialization.jsonObject(with: payload) as? [String: Any] { handle(object) }
    }
    exit(0)  // the node closed its end: it is gone
}

// ---- go --------------------------------------------------------------------------------------------------------

let app = NSApplication.shared
app.setActivationPolicy(.accessory)
if CommandLine.arguments.contains("--selftest") {
    // Everything except the microphone and the speakers: framing and the key table.
    event(["event": "ready", "selftest": true, "keys": keyCodes.count, "modifiers": modifierBits.count])
    exit(0)
}
installHotKeyHandler()
registerHotKey("option+space")
Thread.detachNewThread(readInput)
Timer.scheduledTimer(withTimeInterval: 0.1, repeats: true) { _ in
    if player.isPlaying { event(["event": "played", "ms": playedMs()]) }
}
requestMicrophone()
app.run()
