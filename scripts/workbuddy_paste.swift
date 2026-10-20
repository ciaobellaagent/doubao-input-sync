import AppKit
import ApplicationServices
import CryptoKit
import Foundation
import Darwin
import ImageIO

// WorkBuddy's focused AX element can be visible to the native API but cannot
// be represented by System Events. Field text is read only in memory to verify
// insertion; it is never logged, transmitted, or set through accessibility.
let allowedRoles = ["AXTextArea", "AXTextField", "AXComboBox"]
func digest(_ text: String) -> String {
    SHA256.hash(data: Data(text.utf8)).map { String(format: "%02x", $0) }.joined()
}
func inputResult(role: String, subrole: String, enabled: Bool) -> String? {
    if subrole == "AXSecureTextField" { return "unsafe_target" }
    return allowedRoles.contains(role) && enabled ? nil : "no_focused_input"
}
let traceActionID: String? = {
    guard let raw = ProcessInfo.processInfo.environment["DOUBAO_PASTE_TRACE_ID"],
          let uuid = UUID(uuidString: raw), uuid.uuidString.lowercased() == raw else { return nil }
    return raw
}()
var diagnosticCount = 0
func note(_ stage: String, _ code: Int32) {
    guard diagnosticCount < 64 else { return }
    diagnosticCount += 1
    let directory = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Logs/DoubaoInputSync")
    do {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let path = directory.appendingPathComponent("native-errors.log").path
        // Serialize only diagnostic rotation/writes; it cannot authorize input.
        let lockFD = open(path + ".lock", O_WRONLY | O_CREAT | O_NOFOLLOW | O_NONBLOCK, 0o600)
        guard lockFD >= 0 else { return }
        defer { close(lockFD) }
        guard flock(lockFD, LOCK_EX | LOCK_NB) == 0 else { return }
        defer { flock(lockFD, LOCK_UN) }
        for candidate in [path, path + ".1", path + ".2"] {
            var metadata = stat()
            if lstat(candidate, &metadata) == 0 {
                guard (metadata.st_mode & S_IFMT) == S_IFREG else { return }
                if metadata.st_size > 2 * 1024 * 1024 {
                    let oversized = open(candidate, O_WRONLY | O_NOFOLLOW | O_NONBLOCK)
                    guard oversized >= 0 else { return }
                    let trimmed = ftruncate(oversized, 0)
                    close(oversized)
                    guard trimmed == 0 else { return }
                }
            }
        }
        var metadata = stat()
        let line = "\(Date().timeIntervalSince1970 * 1000)|\(stage)|\(code)|\(traceActionID ?? "-")\n"
        if lstat(path, &metadata) == 0 && metadata.st_size + Int64(line.utf8.count) > 2 * 1024 * 1024 {
            guard unlink(path + ".2") == 0 || errno == ENOENT else { return }
            guard rename(path + ".1", path + ".2") == 0 || errno == ENOENT else { return }
            guard rename(path, path + ".1") == 0 else { return }
        }
        let fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW | O_NONBLOCK, 0o600)
        guard fd >= 0 else { return }
        defer { close(fd) }
        _ = line.withCString { write(fd, $0, strlen($0)) }
    } catch { /* Diagnostics cannot authorize or retry an action. */ }
}

struct AXFailure: Error {
    let stage: String
    let code: AXError
}
func attribute(_ element: AXUIElement, _ name: String, _ stage: String) throws -> CFTypeRef {
    var value: CFTypeRef?
    let error = AXUIElementCopyAttributeValue(element, name as CFString, &value)
    guard error == .success, let value = value else {
        throw AXFailure(stage: stage, code: error == .success ? .noValue : error)
    }
    return value
}
func elementAttribute(_ element: AXUIElement, _ name: String, _ stage: String) throws -> AXUIElement {
    let value = try attribute(element, name, stage)
    guard CFGetTypeID(value) == AXUIElementGetTypeID() else {
        throw AXFailure(stage: stage, code: .illegalArgument)
    }
    return unsafeBitCast(value, to: AXUIElement.self)
}
struct Focus {
    let app: AXUIElement
    let window: AXUIElement
    let field: AXUIElement
    let pid: pid_t
}
struct FocusChanged: Error {}
struct ActionStopped: Error {
    let result: String
}
struct TargetRejected: Error {
    let result: String
}
enum InputEligibility {
    case editable
    case unsupported(Int32)
    case blocked(String)
}
func roleCode(_ role: String) -> Int32 {
    switch role {
    case "AXWebArea": return 4
    case "AXGroup": return 5
    case "AXUnknown": return 6
    case "AXWindow": return 7
    case "AXScrollArea": return 8
    default: return 0
    }
}
func eligibility(role: String, subrole: String, enabled: Bool, protected: Bool) -> InputEligibility {
    if protected || subrole == "AXSecureTextField" { return .blocked("unsafe_target") }
    if !enabled { return .blocked("no_focused_input") }
    return allowedRoles.contains(role) ? .editable : .unsupported(roleCode(role))
}
// Read both representations when the app returns a wrapper instead of an input.
// Never search children, accept a different process, or promote wrapper roles.
func resolveFocusedField<Element>(readApp: () throws -> Element, readSystem: () throws -> Element,
                                  ownerCheck: (Element) throws -> Void,
                                  assess: (Element) throws -> InputEligibility,
                                  report: (String, Int32) -> Void = { _, _ in }) throws -> Element {
    let appCandidate: Element?
    let appReadFailure: AXFailure?
    do {
        appCandidate = try readApp()
        appReadFailure = nil
    } catch let error as AXFailure {
        guard [.cannotComplete, .noValue, .attributeUnsupported].contains(error.code) else { throw error }
        appCandidate = nil
        appReadFailure = error
    }
    let appFailure: AXFailure
    if let candidate = appCandidate {
        try ownerCheck(candidate)
        switch try assess(candidate) {
        case .editable: return candidate
        case .blocked(let result): throw TargetRejected(result: result)
        case .unsupported(let code):
            report("ax_app_nontext_role", code)
            appFailure = AXFailure(stage: "ax_app_nontext", code: .noValue)
        }
    } else {
        appFailure = appReadFailure!
    }
    let candidate: Element
    do {
        candidate = try readSystem()
    } catch let error as AXFailure {
        throw FocusReadFailure(app: appFailure, system: error)
    }
    try ownerCheck(candidate)
    switch try assess(candidate) {
    case .editable: return candidate
    case .blocked(let result): throw TargetRejected(result: result)
    case .unsupported(let code):
        report("ax_system_nontext_role", code)
        throw TargetRejected(result: "no_focused_input")
    }
}
struct FocusReadFailure: Error {
    let app: AXFailure
    let system: AXFailure
}
func recordFocusFailure(_ error: Error) {
    if let failure = error as? FocusReadFailure {
        note(failure.app.stage, failure.app.code.rawValue)
        note(failure.system.stage, failure.system.code.rawValue)
    } else if let failure = error as? AXFailure {
        note(failure.stage, failure.code.rawValue)
    }
}
func initialFocusCanRetry(_ error: Error) -> Bool {
    if let failure = error as? FocusReadFailure {
        let codes = [failure.app.code, failure.system.code]
        return codes.contains(.cannotComplete) && codes.allSatisfy {
            $0 == .cannotComplete || $0 == .noValue || $0 == .attributeUnsupported
        }
    }
    guard let failure = error as? AXFailure else { return false }
    return failure.code == .cannotComplete &&
        ["ax_window", "ax_window_recheck", "ax_app_field", "ax_system_field"].contains(failure.stage)
}
// Only initial discovery may re-read AX. There are no AX attribute writes or
// input dispatch; standard metadata reads may initialize a provider's interface.
// Keep callbacks injectable so preparation, retry and cancellation can be tested
// without reading any application, clipboard or accessibility permissions.
func captureInitialFocus<T>(check: () throws -> Void,
                            prepare: () throws -> Void = {}, read: () throws -> T,
                            onRetry: (Error) throws -> Void,
                            pause: () -> Void = { Thread.sleep(forTimeInterval: 0.025) }) throws -> T {
    try check()
    // A standard application-role query lets on-demand accessibility providers
    // prepare their native interface. Perform it once, before focus discovery;
    // permission/type failures cannot enter the focus retry path.
    try prepare()
    try check()
    do {
        let result = try read()
        try check()
        return result
    } catch {
        guard initialFocusCanRetry(error) else { throw error }
        try check()
        try onRetry(error)
        try check()
        pause()
        try check()
        let result = try read()
        try check()
        return result
    }
}
func focus(_ system: AXUIElement, _ expectedPid: pid_t,
           observeWindow: ((AXUIElement) throws -> Void)? = nil) throws -> Focus {
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == expectedPid else { throw FocusChanged() }
    let app = AXUIElementCreateApplication(expectedPid)
    AXUIElementSetMessagingTimeout(app, 0.25)
    let pid = expectedPid
    // Anchor any window available before initial recovery; reject a different
    // window on the retry even if it belongs to the same foreground process.
    let window = try elementAttribute(app, kAXFocusedWindowAttribute, "ax_window")
    try observeWindow?(window)
    // Use the confirmed front app directly: querying focusedApplication on the
    // global AX object can fail even when its app-level interface works.
    let field = try resolveFocusedField(readApp: {
        try elementAttribute(app, kAXFocusedUIElementAttribute, "ax_app_field")
    }, readSystem: {
        try elementAttribute(system, kAXFocusedUIElementAttribute, "ax_system_field")
    }, ownerCheck: { candidate in
        var fieldPid: pid_t = 0
        guard AXUIElementGetPid(candidate, &fieldPid) == .success, fieldPid == pid else {
            throw AXFailure(stage: "ax_field_owner", code: .invalidUIElement)
        }
    }, assess: fieldEligibility, report: note)
    guard CFEqual(window, try elementAttribute(app, kAXFocusedWindowAttribute, "ax_window_recheck")) else {
        throw FocusChanged()
    }
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == expectedPid else { throw FocusChanged() }
    return Focus(app: app, window: window, field: field, pid: pid)
}
func sameFocus(_ a: Focus, _ b: Focus) -> Bool {
    a.pid == b.pid && CFEqual(a.app, b.app) && CFEqual(a.window, b.window) && CFEqual(a.field, b.field)
}
func optionalAttribute(_ element: AXUIElement, _ name: String, _ stage: String) throws -> CFTypeRef? {
    do { return try attribute(element, name, stage) }
    catch let error as AXFailure where error.code == .attributeUnsupported || error.code == .noValue { return nil }
}
func optionalString(_ field: AXUIElement, _ name: String, _ stage: String) throws -> String? {
    guard let value = try optionalAttribute(field, name, stage) else { return nil }
    guard let result = value as? String else { throw AXFailure(stage: stage, code: .illegalArgument) }
    return result
}
func optionalBool(_ field: AXUIElement, _ name: String, _ stage: String) throws -> Bool? {
    guard let value = try optionalAttribute(field, name, stage) else { return nil }
    guard CFGetTypeID(value) == CFBooleanGetTypeID(), let result = value as? Bool else {
        throw AXFailure(stage: stage, code: .illegalArgument)
    }
    return result
}
func fieldEligibility(_ field: AXUIElement) throws -> InputEligibility {
    guard let role = try attribute(field, kAXRoleAttribute, "ax_role") as? String else {
        throw AXFailure(stage: "ax_role_type", code: .illegalArgument)
    }
    let subrole = try optionalString(field, kAXSubroleAttribute, "ax_subrole") ?? ""
    let enabled = try optionalBool(field, kAXEnabledAttribute, "ax_enabled") ?? true
    let protected = try optionalBool(field, "AXProtectedContent", "ax_protected") ?? false
    return eligibility(role: role, subrole: subrole, enabled: enabled, protected: protected)
}
func roleCheck(_ field: AXUIElement) throws -> String? {
    switch try fieldEligibility(field) {
    case .editable: return nil
    case .unsupported: return "no_focused_input"
    case .blocked(let result): return result
    }
}
let blockingModifiers: CGEventFlags = [.maskShift, .maskControl, .maskAlternate, .maskCommand]
struct ModifierSnapshot {
    let appKit: UInt64
    let session: UInt64
    let hardware: UInt64
    var blocked: Bool { (appKit | session | hardware) & blockingModifiers.rawValue != 0 }
}
enum ModifierCheck: String { case initial, clipboardReady = "clipboard_ready", preDispatch = "pre_dispatch" }
func modifierBits(_ raw: UInt64) -> Int32 {
    let flags: [CGEventFlags] = [.maskShift, .maskControl, .maskAlternate, .maskCommand]
    return flags.enumerated().reduce(0) { result, item in
        result | (raw & item.element.rawValue == 0 ? 0 : Int32(1 << item.offset))
    }
}
func modifiersHeld(_ stage: ModifierCheck,
                   sample: () -> ModifierSnapshot = {
                       ModifierSnapshot(appKit: UInt64(NSEvent.modifierFlags.rawValue),
                           session: CGEventSource.flagsState(.combinedSessionState).rawValue,
                           hardware: CGEventSource.flagsState(.hidSystemState).rawValue)
                   }, report: (String, Int32) -> Void = note) -> Bool {
    let state = sample()
    if state.blocked {
        // A single snapshot drives both refusal and bounded diagnostics. Keep
        // the hardware check even if synthesized flags clear the session state.
        report("modifier_" + stage.rawValue + "_appkit", modifierBits(state.appKit))
        report("modifier_" + stage.rawValue + "_session", modifierBits(state.session))
        report("modifier_" + stage.rawValue + "_hardware", modifierBits(state.hardware))
    }
    return state.blocked
}
struct PasteKeyStep {
    let key: CGKeyCode
    let down: Bool
    let flags: CGEventFlags
}
let pasteKeySteps = [PasteKeyStep(key: 9, down: true, flags: .maskCommand),
                     PasteKeyStep(key: 9, down: false, flags: [])]
func pasteKeyEvents(_ source: CGEventSource) -> [CGEvent]? {
    var events: [CGEvent] = []
    for step in pasteKeySteps {
        guard let event = CGEvent(keyboardEventSource: source, virtualKey: step.key, keyDown: step.down) else { return nil }
        event.flags = step.flags
        events.append(event)
    }
    return events
}
struct InputSnapshot {
    let text: String
    let range: CFRange
}
func fieldText(_ field: AXUIElement) throws -> String {
    guard let text = try attribute(field, kAXValueAttribute, "verify_value") as? String else {
        throw AXFailure(stage: "verify_value_type", code: .illegalArgument)
    }
    return text
}
func inputSnapshot(_ field: AXUIElement) throws -> InputSnapshot {
    let text = try fieldText(field)
    let value = try attribute(field, kAXSelectedTextRangeAttribute, "verify_selection")
    guard CFGetTypeID(value) == AXValueGetTypeID() else {
        throw AXFailure(stage: "verify_selection_type", code: .illegalArgument)
    }
    let axValue = unsafeBitCast(value, to: AXValue.self)
    var range = CFRange()
    guard AXValueGetType(axValue) == .cfRange, AXValueGetValue(axValue, .cfRange, &range) else {
        throw AXFailure(stage: "verify_selection_type", code: .illegalArgument)
    }
    return InputSnapshot(text: text, range: range)
}
func expectedInsertion(_ before: String, _ range: CFRange, _ pasted: String) -> String? {
    let count = (before as NSString).length
    guard range.location >= 0, range.length >= 0, range.location <= count,
          range.length <= count - range.location else { return nil }
    return (before as NSString).replacingCharacters(in: NSRange(location: range.location, length: range.length), with: pasted)
}
func sameSnapshot(_ a: InputSnapshot, _ b: InputSnapshot) -> Bool {
    a.text.utf16.elementsEqual(b.text.utf16) && a.range.location == b.range.location && a.range.length == b.range.length
}
func insertionConfirmed(_ before: String, _ expected: String, _ actual: String, emptyWorkBuddyPaste: String? = nil) -> Bool {
    guard !before.utf16.elementsEqual(actual.utf16) else { return false }
    if actual.utf16.elementsEqual(expected.utf16) { return true }
    // WorkBuddy's empty rich editor exposes a lone BOM sentinel in AX and
    // removes it upon insertion. Accept only the entire exact pasted body.
    return before == "\u{FEFF}" && emptyWorkBuddyPaste.map { actual.utf16.elementsEqual($0.utf16) } == true
}
func waitForInsertion(before: String, expected: String, emptyWorkBuddyPaste: String?,
                      deadline: Double, ownerAlive: () -> Bool, read: () throws -> String,
                      now: () -> Double = { Date().timeIntervalSince1970 * 1000 },
                      pause: () -> Void = { Thread.sleep(forTimeInterval: 0.025) },
                      observe: (Int32) -> Void = { _ in }) -> (verified: Bool, failure: AXFailure?) {
    var lastFailure: AXFailure?
    // Fixed categories only: no read, unchanged, changed unexpectedly, read
    // error, exact match. Timeliness is checked separately. No text or lengths.
    var observation: Int32 = 0
    defer { observe(observation) }
    while now() < deadline && ownerAlive() {
        do {
            let actual = try read()
            lastFailure = nil
            observation = before.utf16.elementsEqual(actual.utf16) ? 1 : 2
            if insertionConfirmed(before, expected, actual, emptyWorkBuddyPaste: emptyWorkBuddyPaste) {
                observation = 4
                return (now() < deadline && ownerAlive(), nil)
            }
        } catch let error as AXFailure {
            observation = 3
            lastFailure = error
            // Electron can temporarily stop responding during the menu action.
            // Retry this read only, without changing target or posting again.
            if error.code != .cannotComplete && error.code != .noValue { return (false, error) }
        } catch {
            return (false, AXFailure(stage: "verify_exception", code: .failure))
        }
        pause()
    }
    return (false, lastFailure)
}
func children(_ element: AXUIElement) throws -> [AXUIElement] {
    guard let value = try optionalAttribute(element, kAXChildrenAttribute, "menu_children") else { return [] }
    guard let array = value as? [AXUIElement] else { throw AXFailure(stage: "menu_children_type", code: .illegalArgument) }
    return array
}
func pasteMenuItem(_ app: AXUIElement) throws -> AXUIElement {
    let menuBar = try elementAttribute(app, kAXMenuBarAttribute, "menu_bar")
    var matches: [AXUIElement] = []
    // Only the native top-level menus; never search web content or submenus.
    for top in try children(menuBar) {
        for menu in try children(top) {
            for item in try children(menu) {
                let identifier = try optionalString(item, kAXIdentifierAttribute, "menu_identifier")
                if identifier == "paste:" { matches.append(item) }
            }
        }
    }
    guard matches.count == 1, let item = matches.first,
          try optionalBool(item, kAXEnabledAttribute, "menu_enabled") == true else {
        throw AXFailure(stage: "menu_paste_unavailable", code: .noValue)
    }
    var actions: CFArray?
    let error = AXUIElementCopyActionNames(item, &actions)
    guard error == .success, let names = actions as? [String], names.contains(kAXPressAction) else {
        throw AXFailure(stage: "menu_press_unavailable", code: error == .success ? .actionUnsupported : error)
    }
    return item
}
func run(deadline: Double, expectedHash: String, owner: pid_t, expectedPid: pid_t) -> String {
    func expired() -> Bool { Date().timeIntervalSince1970 * 1000 >= deadline }
    func ownerGone() -> Bool { kill(owner, 0) != 0 }
    guard !expired(), !ownerGone() else { return "expired" }
    guard AXIsProcessTrusted() else { note("ax_trust", -25211); return "permission_missing" }
    let isWorkBuddy = NSRunningApplication(processIdentifier: expectedPid)?.bundleIdentifier == "com.tencent.workbuddy.mac"
    if !isWorkBuddy && !CGPreflightPostEventAccess() { note("key_trust", -25211); return "permission_missing" }
    let system = AXUIElementCreateSystemWide()
    AXUIElementSetMessagingTimeout(system, 0.25)
    let clipboard = NSPasteboard.general
    do {
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == expectedPid else { return "focus_changed" }
        let app = AXUIElementCreateApplication(expectedPid)
        AXUIElementSetMessagingTimeout(app, 0.25)
        if ["com.tencent.workbuddy.mac", "com.google.antigravity-ide"].contains(NSRunningApplication(processIdentifier: expectedPid)?.bundleIdentifier ?? "") {
            _ = AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        }
        var initialWindow: AXUIElement?
        let initial = try captureInitialFocus(check: {
            guard !expired() else { throw ActionStopped(result: "expired") }
            guard !ownerGone() else { throw ActionStopped(result: "receiver_stopped") }
            guard NSWorkspace.shared.frontmostApplication?.processIdentifier == expectedPid else { throw FocusChanged() }
        }, prepare: {
            let role = try attribute(app, kAXRoleAttribute, "ax_app_role")
            guard let role = role as? String, role == kAXApplicationRole else {
                throw AXFailure(stage: "ax_app_role_type", code: .illegalArgument)
            }
            note("ax_app_role_checked", 0)
        }, read: {
            try focus(system, expectedPid, observeWindow: { window in
                if let captured = initialWindow, !CFEqual(captured, window) { throw FocusChanged() }
                initialWindow = window
            })
        }, onRetry: { failure in
            recordFocusFailure(failure)
            note("ax_focus_read_retry", 0)
        })
        note("native_target_pid", initial.pid)
        note("native_focus_ready", 0)
        guard initial.pid == expectedPid else {
            return "focus_changed"
        }
        if let result = try roleCheck(initial.field) { return result }
        if modifiersHeld(.initial) { return "modifier_held" }
        note("native_clipboard_wait", 0)
        while !expired() && !ownerGone() {
            guard sameFocus(initial, try focus(system, expectedPid)) else { return "focus_changed" }
            let count = clipboard.changeCount
            if let text = clipboard.string(forType: .string), digest(text) == expectedHash {
                guard clipboard.changeCount == count, clipboard.string(forType: .string) == text else {
                    return "clipboard_changed"
                }
                guard sameFocus(initial, try focus(system, expectedPid)) else { return "focus_changed" }
                if let result = try roleCheck(initial.field) { return result }
                if modifiersHeld(.clipboardReady) { return "modifier_held" }
                guard !expired(), !ownerGone() else { return "expired" }
                guard clipboard.changeCount == count, clipboard.string(forType: .string) == text else {
                    return "clipboard_changed"
                }
                note("native_clipboard_matched", 0)
                let before = try inputSnapshot(initial.field)
                guard let expected = expectedInsertion(before.text, before.range, text),
                      !before.text.utf16.elementsEqual(expected.utf16) else {
                    note("verify_unobservable", 0); return "paste_failed"
                }
                let menuItem = isWorkBuddy ? try pasteMenuItem(initial.app) : nil
                // Intent alone is never proof of dispatch. All final field,
                // snapshot, modifier, clipboard and deadline checks follow it.
                note("native_dispatch_intent", 0)
                // Leave time to observe insertion, all within the original deadline.
                guard deadline - Date().timeIntervalSince1970 * 1000 >= 800 else { return "expired" }
                guard sameFocus(initial, try focus(system, expectedPid)) else { return "focus_changed" }
                guard sameSnapshot(before, try inputSnapshot(initial.field)) else { return "focus_changed" }
                guard !expired(), !ownerGone() else { return "expired" }
                if modifiersHeld(.preDispatch) { return "modifier_held" }
                guard clipboard.changeCount == count, clipboard.string(forType: .string) == text else {
                    return "clipboard_changed"
                }
                guard NSWorkspace.shared.frontmostApplication?.processIdentifier == expectedPid else { return "focus_changed" }
                guard !expired(), !ownerGone() else { return "expired" }
                if let item = menuItem {
                    // WorkBuddy exposes the standard native paste: menu command.
                    // Invoke it once, without synthesizing a keyboard shortcut.
                    let error = AXUIElementPerformAction(item, kAXPressAction as CFString)
                    note("menu_paste_dispatch", error.rawValue)
                    guard error == .success else { return "paste_failed" }
                } else {
                    guard let source = CGEventSource(stateID: .combinedSessionState),
                          let events = pasteKeyEvents(source) else { return "paste_failed" }
                    // End the V key pair without declaring Command still held.
                    // No extra modifier release or uncertain paste retry is sent.
                    for event in events { event.post(tap: .cghidEventTap) }
                    note("key_paste_dispatch", 0)
                }
                // Dispatch success is not insertion success. Never retry/fallback
                // after a command may have been delivered. No field text is logged.
                // Read the exact captured field. Re-querying global/app focus
                // during menu dismissal can fail even when insertion succeeded.
                // Focus may change after dispatch; confirmed text in the original
                // field is still evidence of insertion. No other field is read.
                note("native_verify_begin", 0)
                AXUIElementSetMessagingTimeout(initial.field, 0.1)
                let confirmation = waitForInsertion(before: before.text, expected: expected,
                    emptyWorkBuddyPaste: isWorkBuddy ? text : nil, deadline: deadline - 200,
                    ownerAlive: { !ownerGone() }, read: {
                        if try roleCheck(initial.field) != nil {
                            throw AXFailure(stage: "verify_target_changed", code: .illegalArgument)
                        }
                        return try fieldText(initial.field)
                    }, observe: { note("verify_observation", $0) })
                if confirmation.verified {
                    note("paste_verified", 0)
                    return "executed"
                }
                if let failure = confirmation.failure { note(failure.stage, failure.code.rawValue) }
                note("paste_unconfirmed", 0)
                return "paste_unconfirmed"
            }
            Thread.sleep(forTimeInterval: 0.025)
        }
        return ownerGone() ? "receiver_stopped" : "clipboard_timeout"
    } catch let error as ActionStopped {
        return error.result
    } catch let error as TargetRejected {
        return error.result
    } catch is FocusChanged {
        // Only pre-dispatch focus checks throw here. Verification catches its
        // own errors and always reports an unconfirmed result after dispatch.
        return "focus_changed"
    } catch let error as FocusReadFailure {
        recordFocusFailure(error)
        return "paste_failed"
    } catch let error as AXFailure {
        recordFocusFailure(error)
        return "paste_failed"
    } catch {
        note("native_exception", 0)
        return "paste_failed"
    }
}

// Attachment files have one private, bounded cache location. Validation and
// image preparation below never query accessibility or touch the pasteboard.
let attachmentOriginalLimit = 20 * 1024 * 1024
let attachmentPNGLimit = 50 * 1024 * 1024
let attachmentPixelLimit = 25_000_000
let attachmentDimensionLimit = 16_384
let attachmentCacheRoot = URL(fileURLWithPath: NSHomeDirectory())
    .appendingPathComponent("Library/Application Support/DoubaoInputSync/attachments").path
struct AttachmentFailure: Error { let result: String }
final class AttachmentDirectory {
    let fd: Int32
    let path: String
    init(fd: Int32, path: String) { self.fd = fd; self.path = path }
    deinit { close(fd) }
}
struct AttachmentFile {
    let directory: AttachmentDirectory
    let basename: String
    let metadata: stat
    let data: Data
    var path: String { directory.path + "/" + basename }
}
func dataDigest(_ data: Data) -> String {
    SHA256.hash(data: data).map { String(format: "%02x", $0) }.joined()
}
func safeAttachmentBasename(_ name: String) -> Bool {
    !name.isEmpty && !name.hasPrefix(".") && name.utf8.count <= 240 &&
        !name.contains("/") && !name.contains("\\") &&
        name.trimmingCharacters(in: .whitespacesAndNewlines) == name &&
        !name.unicodeScalars.contains { CharacterSet.controlCharacters.contains($0) }
}
func canonicalAttachmentUUID(_ name: String) -> Bool {
    UUID(uuidString: name)?.uuidString.lowercased() == name
}
// Walk every component with O_NOFOLLOW. Resolving a symlink and then testing
// its destination would allow an out-of-cache file to enter the clipboard.
func openAttachmentDirectory(_ path: String, privateDirectory: Bool) throws -> Int32 {
    guard path.hasPrefix("/"), URL(fileURLWithPath: path).standardizedFileURL.path == path else {
        throw AttachmentFailure(result: "attachment_cache_unsafe")
    }
    var current = open("/", O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard current >= 0 else { throw AttachmentFailure(result: "attachment_cache_unsafe") }
    do {
        for part in path.split(separator: "/") {
            let next = openat(current, String(part), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
            guard next >= 0 else { throw AttachmentFailure(result: "attachment_cache_unsafe") }
            close(current)
            current = next
        }
        var metadata = stat()
        guard fstat(current, &metadata) == 0,
              (metadata.st_mode & S_IFMT) == S_IFDIR,
              !privateDirectory || (metadata.st_uid == getuid() && metadata.st_mode & 0o7077 == 0) else {
            throw AttachmentFailure(result: "attachment_cache_unsafe")
        }
        return current
    } catch { close(current); throw error }
}
func attachmentDirectory(originalPath: String, cacheRoot: String) throws -> (AttachmentDirectory, String) {
    guard cacheRoot.hasPrefix("/"), !cacheRoot.hasSuffix("/"),
          URL(fileURLWithPath: cacheRoot).standardizedFileURL.path == cacheRoot,
          originalPath.hasPrefix(cacheRoot + "/"),
          URL(fileURLWithPath: originalPath).standardizedFileURL.path == originalPath else {
        throw AttachmentFailure(result: "attachment_cache_unsafe")
    }
    let parts = String(originalPath.dropFirst(cacheRoot.count + 1)).split(separator: "/", omittingEmptySubsequences: false)
    guard parts.count == 2, canonicalAttachmentUUID(String(parts[0])), safeAttachmentBasename(String(parts[1])) else {
        throw AttachmentFailure(result: "attachment_cache_unsafe")
    }
    let rootFD = try openAttachmentDirectory(cacheRoot, privateDirectory: true)
    defer { close(rootFD) }
    let directoryPath = cacheRoot + "/" + parts[0]
    let fd = openat(rootFD, String(parts[0]), O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC)
    guard fd >= 0 else { throw AttachmentFailure(result: "attachment_cache_unsafe") }
    var metadata = stat()
    guard fstat(fd, &metadata) == 0, (metadata.st_mode & S_IFMT) == S_IFDIR,
          metadata.st_uid == getuid(), metadata.st_mode & 0o7077 == 0 else {
        close(fd); throw AttachmentFailure(result: "attachment_cache_unsafe")
    }
    return (AttachmentDirectory(fd: fd, path: directoryPath), String(parts[1]))
}
func readAttachmentFile(directory: AttachmentDirectory, basename: String, limit: Int) throws -> AttachmentFile {
    let fd = openat(directory.fd, basename, O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC)
    guard fd >= 0 else { throw AttachmentFailure(result: "attachment_cache_unsafe") }
    defer { close(fd) }
    var metadata = stat()
    guard fstat(fd, &metadata) == 0, (metadata.st_mode & S_IFMT) == S_IFREG,
          metadata.st_uid == getuid(), metadata.st_nlink == 1,
          metadata.st_mode & 0o7177 == 0, metadata.st_size >= 0 else {
        throw AttachmentFailure(result: "attachment_cache_unsafe")
    }
    guard metadata.st_size <= limit else { throw AttachmentFailure(result: "attachment_too_large") }
    var data = Data()
    var buffer = [UInt8](repeating: 0, count: 64 * 1024)
    while true {
        let count = read(fd, &buffer, min(buffer.count, limit - data.count + 1))
        if count == 0 { break }
        if count < 0 {
            if errno == EINTR { continue }
            throw AttachmentFailure(result: "attachment_read_failed")
        }
        guard data.count + count <= limit else { throw AttachmentFailure(result: "attachment_too_large") }
        data.append(contentsOf: buffer.prefix(count))
    }
    var after = stat()
    guard fstat(fd, &after) == 0, after.st_dev == metadata.st_dev, after.st_ino == metadata.st_ino,
          after.st_size == metadata.st_size, data.count == metadata.st_size,
          after.st_mtimespec.tv_sec == metadata.st_mtimespec.tv_sec,
          after.st_mtimespec.tv_nsec == metadata.st_mtimespec.tv_nsec,
          after.st_ctimespec.tv_sec == metadata.st_ctimespec.tv_sec,
          after.st_ctimespec.tv_nsec == metadata.st_ctimespec.tv_nsec else {
        throw AttachmentFailure(result: "attachment_changed")
    }
    return AttachmentFile(directory: directory, basename: basename, metadata: metadata, data: data)
}
func attachmentStillCurrent(_ file: AttachmentFile) -> Bool {
    var metadata = stat()
    // The URL will later be consumed by the target app. Reject replaced cache
    // directories too, not just a changed file under an already-open dirfd.
    guard let liveFD = try? openAttachmentDirectory(file.directory.path, privateDirectory: true) else { return false }
    defer { close(liveFD) }
    var openedDirectory = stat(), liveDirectory = stat()
    guard fstat(file.directory.fd, &openedDirectory) == 0, fstat(liveFD, &liveDirectory) == 0,
          openedDirectory.st_dev == liveDirectory.st_dev, openedDirectory.st_ino == liveDirectory.st_ino else { return false }
    return fstatat(liveFD, file.basename, &metadata, AT_SYMLINK_NOFOLLOW) == 0 &&
        (metadata.st_mode & S_IFMT) == S_IFREG && metadata.st_nlink == 1 &&
        metadata.st_uid == getuid() && metadata.st_mode & 0o7177 == 0 &&
        metadata.st_dev == file.metadata.st_dev && metadata.st_ino == file.metadata.st_ino &&
        metadata.st_size == file.metadata.st_size &&
        metadata.st_mtimespec.tv_sec == file.metadata.st_mtimespec.tv_sec &&
        metadata.st_mtimespec.tv_nsec == file.metadata.st_mtimespec.tv_nsec &&
        metadata.st_ctimespec.tv_sec == file.metadata.st_ctimespec.tv_sec &&
        metadata.st_ctimespec.tv_nsec == file.metadata.st_ctimespec.tv_nsec
}
func attachmentDimensionsAllowed(width: Int64, height: Int64) -> Bool {
    width > 0 && height > 0 && width <= attachmentDimensionLimit && height <= attachmentDimensionLimit &&
        width <= Int64(attachmentPixelLimit) / height
}
func boundedImageSource(_ data: Data, pngOnly: Bool = false) -> (source: CGImageSource, width: Int64, height: Int64)? {
    guard let source = CGImageSourceCreateWithData(data as CFData,
            [kCGImageSourceShouldCache: false] as CFDictionary), CGImageSourceGetCount(source) > 0,
          !pngOnly || CGImageSourceGetType(source) as String? == "public.png",
          let properties = CGImageSourceCopyPropertiesAtIndex(source, 0, nil) as? [CFString: Any],
          let width = properties[kCGImagePropertyPixelWidth] as? NSNumber,
          let height = properties[kCGImagePropertyPixelHeight] as? NSNumber else { return nil }
    let w = width.int64Value, h = height.int64Value
    // Metadata must pass before any pixel decoding. Unknown/oversized/broken
    // images remain ordinary files and are never decoded as a fallback.
    guard attachmentDimensionsAllowed(width: w, height: h) else { return nil }
    return (source, w, h)
}
func boundedPNG(_ data: Data) -> Data? {
    guard let input = boundedImageSource(data),
          let image = CGImageSourceCreateThumbnailAtIndex(input.source, 0,
            [kCGImageSourceCreateThumbnailFromImageAlways: true,
             kCGImageSourceCreateThumbnailWithTransform: true,
             kCGImageSourceThumbnailMaxPixelSize: max(input.width, input.height),
             kCGImageSourceShouldCacheImmediately: true] as CFDictionary),
          attachmentDimensionsAllowed(width: Int64(image.width), height: Int64(image.height)) else { return nil }
    let output = NSMutableData()
    guard let destination = CGImageDestinationCreateWithData(output, "public.png" as CFString, 1, nil) else { return nil }
    CGImageDestinationAddImage(destination, image, nil)
    guard CGImageDestinationFinalize(destination), output.length <= attachmentPNGLimit else { return nil }
    return output as Data
}
func prepareAttachment(originalPath: String, pngPath: String, cacheRoot: String = attachmentCacheRoot) -> String {
    do {
        let (directory, basename) = try attachmentDirectory(originalPath: originalPath, cacheRoot: cacheRoot)
        guard pngPath == directory.path + "/.clipboard.png" else { return "attachment_cache_unsafe" }
        let original = try readAttachmentFile(directory: directory, basename: basename, limit: attachmentOriginalLimit)
        guard let png = boundedPNG(original.data) else { return "file_ready" }
        guard attachmentStillCurrent(original) else { return "attachment_changed" }
        let fd = openat(directory.fd, ".clipboard.png", O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0o600)
        if fd < 0 {
            guard errno == EEXIST else { return "attachment_cache_unsafe" }
            // A new preparation may reuse this attachment's cache directory.
            // Reuse only an intact private output derived from this exact
            // original, never an unchecked filename or an in-place overwrite.
            let existing = try readAttachmentFile(directory: directory, basename: ".clipboard.png", limit: attachmentPNGLimit)
            guard existing.data == png else { return "attachment_cache_unsafe" }
            guard attachmentStillCurrent(original), attachmentStillCurrent(existing) else { return "attachment_changed" }
            return "image_ready"
        }
        var completed = false
        defer {
            close(fd)
            if !completed { _ = unlinkat(directory.fd, ".clipboard.png", 0) }
        }
        try png.withUnsafeBytes { raw in
            var written = 0
            while written < png.count {
                let count = write(fd, raw.baseAddress!.advanced(by: written), png.count - written)
                if count < 0 && errno == EINTR { continue }
                guard count > 0 else { throw AttachmentFailure(result: "attachment_write_failed") }
                written += count
            }
        }
        guard attachmentStillCurrent(original) else { return "attachment_changed" }
        completed = true
        return "image_ready"
    } catch let error as AttachmentFailure { return error.result }
    catch { return "attachment_prepare_failed" }
}
func attachmentClipboardMatches(_ clipboard: NSPasteboard, count: Int, fileURL: String, png: Data?) -> Bool {
    guard clipboard.changeCount == count, let items = clipboard.pasteboardItems, items.count == 1,
          items[0].string(forType: .fileURL) == fileURL else { return false }
    if let png = png {
        guard items[0].data(forType: .png) == png else { return false }
    } else if items[0].types.contains(.png) { return false }
    return clipboard.changeCount == count
}
// No callback is retried. A stopped guard cannot progress to a clipboard write
// or dispatch; a successful dispatch is never converted into insertion proof.
func guardedAttachmentDispatch(beforeWrite: () throws -> Void, writeClipboard: () throws -> Void,
                               beforeDispatch: () throws -> Void, dispatch: () throws -> Bool) throws -> String {
    try beforeWrite()
    try writeClipboard()
    try beforeDispatch()
    return try dispatch() ? "paste_dispatched" : "paste_failed"
}
func runAttachment(deadline: Double, originalPath: String, expectedHash: String, pngPath: String?,
                   owner: pid_t, expectedPid: pid_t) -> String {
    func expired() -> Bool { Date().timeIntervalSince1970 * 1000 >= deadline }
    func ownerGone() -> Bool { kill(owner, 0) != 0 }
    guard !expired(), !ownerGone() else { return "expired" }
    guard expectedPid > 1 else { return "no_focused_input" }
    guard AXIsProcessTrusted() else { note("ax_trust", -25211); return "permission_missing" }
    let isWorkBuddy = NSRunningApplication(processIdentifier: expectedPid)?.bundleIdentifier == "com.tencent.workbuddy.mac"
    if !isWorkBuddy && !CGPreflightPostEventAccess() { note("key_trust", -25211); return "permission_missing" }
    let clipboard = NSPasteboard.general
    let initialClipboardCount = clipboard.changeCount
    let system = AXUIElementCreateSystemWide()
    AXUIElementSetMessagingTimeout(system, 0.25)
    do {
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == expectedPid else { return "focus_changed" }
        let app = AXUIElementCreateApplication(expectedPid)
        AXUIElementSetMessagingTimeout(app, 0.25)
        if ["com.tencent.workbuddy.mac", "com.google.antigravity-ide"].contains(NSRunningApplication(processIdentifier: expectedPid)?.bundleIdentifier ?? "") {
            _ = AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        }
        var initialWindow: AXUIElement?
        let initial = try captureInitialFocus(check: {
            guard !expired() else { throw ActionStopped(result: "expired") }
            guard !ownerGone() else { throw ActionStopped(result: "receiver_stopped") }
            guard NSWorkspace.shared.frontmostApplication?.processIdentifier == expectedPid else { throw FocusChanged() }
        }, prepare: {
            let role = try attribute(app, kAXRoleAttribute, "ax_app_role")
            guard let role = role as? String, role == kAXApplicationRole else {
                throw AXFailure(stage: "ax_app_role_type", code: .illegalArgument)
            }
        }, read: {
            try focus(system, expectedPid, observeWindow: { window in
                if let captured = initialWindow, !CFEqual(captured, window) { throw FocusChanged() }
                initialWindow = window
            })
        }, onRetry: { failure in recordFocusFailure(failure); note("ax_focus_read_retry", 0) })
        if let result = try roleCheck(initial.field) { return result }
        if modifiersHeld(.initial) { return "modifier_held" }
        let before = try inputSnapshot(initial.field)
        // Anchor the field and selection before bounded file I/O. A same-app
        // input/window change during hashing must fail the final focus guard.
        let (directory, basename) = try attachmentDirectory(originalPath: originalPath, cacheRoot: attachmentCacheRoot)
        let original = try readAttachmentFile(directory: directory, basename: basename, limit: attachmentOriginalLimit)
        guard dataDigest(original.data) == expectedHash else { return "attachment_hash_mismatch" }
        var pngFile: AttachmentFile?
        if let pngPath = pngPath {
            guard pngPath == directory.path + "/.clipboard.png" else { return "attachment_cache_unsafe" }
            pngFile = try readAttachmentFile(directory: directory, basename: ".clipboard.png", limit: attachmentPNGLimit)
            guard let data = pngFile?.data, boundedImageSource(data, pngOnly: true) != nil else {
                return "attachment_prepare_failed"
            }
        }
        let menuItem = isWorkBuddy ? try pasteMenuItem(initial.app) : nil
        let source = isWorkBuddy ? nil : CGEventSource(stateID: .combinedSessionState)
        let keyEvents = source.flatMap(pasteKeyEvents)
        guard isWorkBuddy || keyEvents != nil else { return "paste_failed" }
        let fileURL = URL(fileURLWithPath: original.path).absoluteString
        let item = NSPasteboardItem()
        guard item.setString(fileURL, forType: .fileURL) else { return "paste_failed" }
        if let png = pngFile?.data, !item.setData(png, forType: .png) { return "paste_failed" }
        var count = initialClipboardCount
        return try guardedAttachmentDispatch(beforeWrite: {
            guard sameFocus(initial, try focus(system, expectedPid)), sameSnapshot(before, try inputSnapshot(initial.field)) else {
                throw ActionStopped(result: "focus_changed")
            }
            if let result = try roleCheck(initial.field) { throw ActionStopped(result: result) }
            if modifiersHeld(.clipboardReady) { throw ActionStopped(result: "modifier_held") }
            guard !expired(), !ownerGone() else { throw ActionStopped(result: "expired") }
            guard attachmentStillCurrent(original), pngFile.map(attachmentStillCurrent) ?? true else {
                throw AttachmentFailure(result: "attachment_changed")
            }
            guard clipboard.changeCount == initialClipboardCount else { throw ActionStopped(result: "clipboard_changed") }
        }, writeClipboard: {
            // One item advertises the original file URL and optional PNG pixels.
            // Never restore over a user's later clipboard contents on failure.
            let clearedCount = clipboard.clearContents()
            guard clipboard.changeCount == clearedCount else { throw ActionStopped(result: "clipboard_changed") }
            guard clipboard.writeObjects([item]) else { throw ActionStopped(result: "paste_failed") }
            count = clipboard.changeCount
        }, beforeDispatch: {
            guard attachmentClipboardMatches(clipboard, count: count, fileURL: fileURL, png: pngFile?.data) else {
                throw ActionStopped(result: "clipboard_changed")
            }
            guard sameFocus(initial, try focus(system, expectedPid)), sameSnapshot(before, try inputSnapshot(initial.field)) else {
                throw ActionStopped(result: "focus_changed")
            }
            if let result = try roleCheck(initial.field) { throw ActionStopped(result: result) }
            if modifiersHeld(.preDispatch) { throw ActionStopped(result: "modifier_held") }
            guard !expired(), !ownerGone() else { throw ActionStopped(result: "expired") }
            guard attachmentStillCurrent(original), pngFile.map(attachmentStillCurrent) ?? true else {
                throw AttachmentFailure(result: "attachment_changed")
            }
            guard attachmentClipboardMatches(clipboard, count: count, fileURL: fileURL, png: pngFile?.data) else {
                throw ActionStopped(result: "clipboard_changed")
            }
            guard NSWorkspace.shared.frontmostApplication?.processIdentifier == expectedPid else {
                throw ActionStopped(result: "focus_changed")
            }
            guard !expired(), !ownerGone() else { throw ActionStopped(result: "expired") }
        }, dispatch: {
            note("attachment_dispatch_intent", 0)
            if let item = menuItem {
                let error = AXUIElementPerformAction(item, kAXPressAction as CFString)
                note("menu_paste_dispatch", error.rawValue)
                return error == .success
            }
            guard let events = keyEvents else { return false }
            for event in events { event.post(tap: .cghidEventTap) }
            note("key_paste_dispatch", 0)
            // AX field text cannot confirm attachment acceptance. No retry,
            // fallback, extra key or Enter is sent after this single command.
            return true
        })
    } catch let error as AttachmentFailure { return error.result }
    catch let error as ActionStopped { return error.result }
    catch let error as TargetRejected { return error.result }
    catch is FocusChanged { return "focus_changed" }
    catch { recordFocusFailure(error); return "paste_failed" }
}
func attachmentPureChecks() {
    for stop in 0...2 {
        var steps: [Int] = []
        func step(_ current: Int) throws {
            steps.append(current)
            if current == stop { throw ActionStopped(result: "clipboard_changed") }
        }
        do {
            _ = try guardedAttachmentDispatch(beforeWrite: { try step(0) }, writeClipboard: { try step(1) },
                beforeDispatch: { try step(2) }, dispatch: { preconditionFailure("a stopped attachment guard must never dispatch") })
            preconditionFailure("a stopped attachment guard must fail")
        } catch let error as ActionStopped {
            precondition(error.result == "clipboard_changed" && steps == Array(0...stop))
        } catch { preconditionFailure("attachment guard reason must be preserved") }
    }
    for didDispatch in [false, true] {
        var steps: [Int] = []
        let result = try! guardedAttachmentDispatch(beforeWrite: { steps.append(0) }, writeClipboard: { steps.append(1) },
            beforeDispatch: { steps.append(2) }, dispatch: { steps.append(3); return didDispatch })
        precondition(steps == [0, 1, 2, 3])
        precondition(result == (didDispatch ? "paste_dispatched" : "paste_failed"))
        precondition(result != "executed")
    }
    precondition(safeAttachmentBasename("说明 图片.png"))
    for name in ["", ".", "..", ".clipboard.png", "../a", "a/b", "a\\b", "x\n", " x", "x ", String(repeating: "x", count: 241)] {
        precondition(!safeAttachmentBasename(name))
    }
    precondition(canonicalAttachmentUUID("e40d4fb3-a855-44ba-98d8-b9919947969f"))
    precondition(!canonicalAttachmentUUID("E40D4FB3-A855-44BA-98D8-B9919947969F"))
    precondition(attachmentDimensionsAllowed(width: 5000, height: 5000))
    for dimensions: (Int64, Int64) in [(0, 1), (1, 0), (-1, 1), (5001, 5000), (1, 16385), (Int64.max, Int64.max)] {
        precondition(!attachmentDimensionsAllowed(width: dimensions.0, height: dimensions.1))
    }
    precondition(boundedPNG(Data("ordinary file".utf8)) == nil)
    // macOS's default /var temp path itself traverses a system symlink. Use an
    // isolated, short-lived home-cache fixture to exercise the same strict
    // no-symlink policy used for the actual attachment cache.
    let directory = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Caches")
        .appendingPathComponent("doubao-attachment-pure-" + UUID().uuidString.lowercased())
    let cache = directory.appendingPathComponent("attachments")
    do {
        try FileManager.default.createDirectory(at: cache, withIntermediateDirectories: true, attributes: [.posixPermissions: 0o700])
        defer { try? FileManager.default.removeItem(at: directory) }
        func fixtureDirectory() throws -> URL {
            let folder = cache.appendingPathComponent(UUID().uuidString.lowercased())
            try FileManager.default.createDirectory(at: folder, withIntermediateDirectories: false, attributes: [.posixPermissions: 0o700])
            return folder
        }
        func write(_ data: Data, _ path: URL) throws {
            try data.write(to: path)
            try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: path.path)
        }
        let ordinary = try fixtureDirectory()
        let ordinaryPath = ordinary.appendingPathComponent("plain.txt")
        try write(Data("plain".utf8), ordinaryPath)
        let ordinaryResult = prepareAttachment(originalPath: ordinaryPath.path,
            pngPath: ordinary.appendingPathComponent(".clipboard.png").path, cacheRoot: cache.path)
        precondition(ordinaryResult == "file_ready", ordinaryResult)
        precondition(!FileManager.default.fileExists(atPath: ordinary.appendingPathComponent(".clipboard.png").path))
        precondition(prepareAttachment(originalPath: ordinaryPath.path,
            pngPath: directory.appendingPathComponent("wrong.png").path, cacheRoot: cache.path) == "attachment_cache_unsafe")
        let (opened, basename) = try attachmentDirectory(originalPath: ordinaryPath.path, cacheRoot: cache.path)
        let unchanged = try readAttachmentFile(directory: opened, basename: basename, limit: attachmentOriginalLimit)
        precondition(attachmentStillCurrent(unchanged))
        precondition(dataDigest(unchanged.data) == digest("plain"))
        try write(Data("changed".utf8), ordinaryPath)
        precondition(!attachmentStillCurrent(unchanged))
        // Tiny real PNG data, synthesized entirely in memory; no screen/image
        // files, app state, clipboard or AX permissions are consulted.
        let rgba = Data([255, 0, 0, 255])
        let provider = CGDataProvider(data: rgba as CFData)!
        let image = CGImage(width: 1, height: 1, bitsPerComponent: 8, bitsPerPixel: 32,
            bytesPerRow: 4, space: CGColorSpaceCreateDeviceRGB(),
            bitmapInfo: CGBitmapInfo(rawValue: CGImageAlphaInfo.last.rawValue),
            provider: provider, decode: nil, shouldInterpolate: false, intent: .defaultIntent)!
        let png = NSMutableData()
        let destination = CGImageDestinationCreateWithData(png, "public.png" as CFString, 1, nil)!
        CGImageDestinationAddImage(destination, image, nil)
        precondition(CGImageDestinationFinalize(destination))
        precondition(boundedPNG(png as Data) != nil)
        precondition(boundedImageSource(png as Data, pngOnly: true) != nil)
        precondition(boundedImageSource(Data([137, 80, 78, 71, 13, 10, 26, 10]), pngOnly: true) == nil)
        let picture = try fixtureDirectory()
        let picturePath = picture.appendingPathComponent("no-extension")
        try write(png as Data, picturePath)
        let pngPath = picture.appendingPathComponent(".clipboard.png")
        precondition(prepareAttachment(originalPath: picturePath.path, pngPath: pngPath.path, cacheRoot: cache.path) == "image_ready")
        let (pictureDir, _) = try attachmentDirectory(originalPath: picturePath.path, cacheRoot: cache.path)
        let prepared = try readAttachmentFile(directory: pictureDir, basename: ".clipboard.png", limit: attachmentPNGLimit)
        precondition(prepared.data.starts(with: [137, 80, 78, 71, 13, 10, 26, 10]))
        precondition(attachmentStillCurrent(prepared))
        // Re-preparation is idempotent only for the exact validated output.
        // A changed output is never trusted, deleted or overwritten.
        precondition(prepareAttachment(originalPath: picturePath.path, pngPath: pngPath.path, cacheRoot: cache.path) == "image_ready")
        try write(Data("changed output".utf8), pngPath)
        precondition(prepareAttachment(originalPath: picturePath.path, pngPath: pngPath.path, cacheRoot: cache.path) == "attachment_cache_unsafe")
        let unchangedBadOutput = try Data(contentsOf: pngPath)
        precondition(unchangedBadOutput == Data("changed output".utf8))
        let oversized = try fixtureDirectory()
        let large = oversized.appendingPathComponent("large.bin")
        let fd = open(large.path, O_WRONLY | O_CREAT | O_EXCL, 0o600)
        precondition(fd >= 0 && ftruncate(fd, off_t(attachmentOriginalLimit + 1)) == 0)
        close(fd)
        precondition(prepareAttachment(originalPath: large.path,
            pngPath: oversized.appendingPathComponent(".clipboard.png").path, cacheRoot: cache.path) == "attachment_too_large")
        let unsafe = try fixtureDirectory()
        let link = unsafe.appendingPathComponent("link.txt")
        try FileManager.default.createSymbolicLink(at: link, withDestinationURL: ordinaryPath)
        precondition(prepareAttachment(originalPath: link.path,
            pngPath: unsafe.appendingPathComponent(".clipboard.png").path, cacheRoot: cache.path) == "attachment_cache_unsafe")
        let hardlink = unsafe.appendingPathComponent("hard.txt")
        precondition(Darwin.link(ordinaryPath.path, hardlink.path) == 0)
        precondition(prepareAttachment(originalPath: hardlink.path,
            pngPath: unsafe.appendingPathComponent(".clipboard.png").path, cacheRoot: cache.path) == "attachment_cache_unsafe")
        let linkedDirectory = cache.appendingPathComponent(UUID().uuidString.lowercased())
        try FileManager.default.createSymbolicLink(at: linkedDirectory, withDestinationURL: ordinary)
        precondition(prepareAttachment(originalPath: linkedDirectory.appendingPathComponent("plain.txt").path,
            pngPath: linkedDirectory.appendingPathComponent(".clipboard.png").path, cacheRoot: cache.path) == "attachment_cache_unsafe")
        for invalid in [ordinary.path + "/../plain.txt", cache.path + "-other/plain.txt", directory.appendingPathComponent("outside.txt").path] {
            precondition(prepareAttachment(originalPath: invalid,
                pngPath: ordinary.appendingPathComponent(".clipboard.png").path, cacheRoot: cache.path) == "attachment_cache_unsafe")
        }
        let permissions = try fixtureDirectory()
        let protectedFile = permissions.appendingPathComponent("private.txt")
        try write(Data("private".utf8), protectedFile)
        for mode in [0o644, 0o660, 0o700] {
            try FileManager.default.setAttributes([.posixPermissions: mode], ofItemAtPath: protectedFile.path)
            precondition(prepareAttachment(originalPath: protectedFile.path,
                pngPath: permissions.appendingPathComponent(".clipboard.png").path, cacheRoot: cache.path) == "attachment_cache_unsafe")
        }
        try FileManager.default.setAttributes([.posixPermissions: 0o600], ofItemAtPath: protectedFile.path)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: permissions.path)
        precondition(prepareAttachment(originalPath: protectedFile.path,
            pngPath: permissions.appendingPathComponent(".clipboard.png").path, cacheRoot: cache.path) == "attachment_cache_unsafe")
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: permissions.path)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: cache.path)
        precondition(prepareAttachment(originalPath: protectedFile.path,
            pngPath: permissions.appendingPathComponent(".clipboard.png").path, cacheRoot: cache.path) == "attachment_cache_unsafe")
        try FileManager.default.setAttributes([.posixPermissions: 0o700], ofItemAtPath: cache.path)
        try FileManager.default.setAttributes([.posixPermissions: 0o755], ofItemAtPath: unsafe.path)
        precondition(prepareAttachment(originalPath: unsafe.appendingPathComponent("missing.txt").path,
            pngPath: unsafe.appendingPathComponent(".clipboard.png").path, cacheRoot: cache.path) == "attachment_cache_unsafe")
    } catch { preconditionFailure("private attachment fixture checks failed") }
}

if CommandLine.arguments == [CommandLine.arguments[0], "--self-test"] {
    // Pure event-plan checks: never create/post an OS event or query live keys.
    precondition(pasteKeySteps.count == 2)
    precondition(pasteKeySteps.allSatisfy { $0.key == 9 })
    precondition(pasteKeySteps.filter { $0.down }.count == 1)
    precondition(pasteKeySteps[0].down && pasteKeySteps[0].flags == .maskCommand)
    precondition(!pasteKeySteps[1].down && pasteKeySteps[1].flags.intersection(blockingModifiers).isEmpty)
    let clearModifiers = ModifierSnapshot(appKit: 0, session: 0, hardware: 0)
    let nonblocking: CGEventFlags = [.maskAlphaShift, .maskSecondaryFn, .maskNumericPad]
    precondition(!ModifierSnapshot(appKit: nonblocking.rawValue, session: nonblocking.rawValue,
                                  hardware: nonblocking.rawValue).blocked)
    for stage in [ModifierCheck.initial, .clipboardReady, .preDispatch] {
        for flag in [CGEventFlags.maskShift, .maskControl, .maskAlternate, .maskCommand] {
            // Any source remains sufficient to stop; hardware cannot be hidden
            // by a synthetic zero-flag event, nor may a synthetic held flag pass.
            for state in [ModifierSnapshot(appKit: flag.rawValue, session: 0, hardware: 0),
                          ModifierSnapshot(appKit: 0, session: flag.rawValue, hardware: 0),
                          ModifierSnapshot(appKit: 0, session: 0, hardware: flag.rawValue)] {
                var samples = 0
                var diagnostics: [(String, Int32)] = []
                let blocked = modifiersHeld(stage, sample: { samples += 1; return state },
                    report: { diagnostics.append(($0, $1)) })
                precondition(blocked && samples == 1 && diagnostics.count == 3)
                precondition(diagnostics.allSatisfy { $0.0.hasPrefix("modifier_" + stage.rawValue + "_") && (0...15).contains($0.1) })
            }
        }
        var clearSamples = 0
        precondition(!modifiersHeld(stage, sample: { clearSamples += 1; return clearModifiers },
            report: { _, _ in preconditionFailure("clear modifiers must not be reported as held") }))
        precondition(clearSamples == 1)
    }
    precondition(modifierBits(blockingModifiers.rawValue) == 15)
    precondition(digest("hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
    for role in allowedRoles { precondition(inputResult(role: role, subrole: "", enabled: true) == nil) }
    precondition(inputResult(role: "AXTextField", subrole: "AXSecureTextField", enabled: true) == "unsafe_target")
    precondition(inputResult(role: "AXWebArea", subrole: "", enabled: true) == "no_focused_input")
    precondition(inputResult(role: "AXTextArea", subrole: "", enabled: false) == "no_focused_input")
    // Representations can disagree about wrappers. Resolve the actual focused
    // input without allowing a wrapper, a foreign PID or a protected element.
    var systemReads = 0
    func unexpectedFocusRead() -> String { preconditionFailure("this focus path must not read system focus") }
    func unexpectedFocusAssessment(_: String) -> InputEligibility { preconditionFailure("foreign field must not be inspected") }
    func testEligibility(_ element: String) -> InputEligibility {
        switch element {
        case "input": return .editable
        case "secure": return .blocked("unsafe_target")
        case "disabled": return .blocked("no_focused_input")
        default: return .unsupported(5)
        }
    }
    do {
        let appInput = try resolveFocusedField(readApp: { "input" },
            readSystem: unexpectedFocusRead,
            ownerCheck: { _ in }, assess: testEligibility)
        precondition(appInput == "input")
        let wrappedInput = try resolveFocusedField(readApp: { "wrapper" },
            readSystem: { systemReads += 1; return "input" }, ownerCheck: { _ in }, assess: testEligibility)
        precondition(wrappedInput == "input" && systemReads == 1)
        let missingAppFocus = try resolveFocusedField(readApp: {
            throw AXFailure(stage: "ax_app_field", code: .noValue)
        }, readSystem: { "input" }, ownerCheck: { _ in }, assess: testEligibility)
        precondition(missingAppFocus == "input")
    } catch { preconditionFailure("a verified focused input should resolve") }
    for blocked in ["secure", "disabled"] {
        do {
            let _: String = try resolveFocusedField(readApp: { blocked },
                readSystem: unexpectedFocusRead,
                ownerCheck: { _ in }, assess: testEligibility)
            preconditionFailure("blocked focus must stop")
        } catch let error as TargetRejected {
            precondition(error.result == (blocked == "secure" ? "unsafe_target" : "no_focused_input"))
        } catch { preconditionFailure("blocked target reason must be retained") }
        do {
            let _: String = try resolveFocusedField(readApp: { "wrapper" }, readSystem: { blocked },
                ownerCheck: { _ in }, assess: testEligibility)
            preconditionFailure("blocked system focus must stop")
        } catch is TargetRejected {} catch { preconditionFailure("blocked global focus must be rejected") }
    }
    do {
        let _: String = try resolveFocusedField(readApp: { "wrapper" }, readSystem: { "wrapper" },
            ownerCheck: { _ in }, assess: testEligibility)
        preconditionFailure("wrappers are not editable inputs")
    } catch let error as TargetRejected { precondition(error.result == "no_focused_input") }
    catch { preconditionFailure("wrapper rejection must be retained") }
    do {
        let _: String = try resolveFocusedField(readApp: { "foreign-input" },
            readSystem: unexpectedFocusRead,
            ownerCheck: { _ in throw AXFailure(stage: "ax_field_owner", code: .invalidUIElement) },
            assess: unexpectedFocusAssessment)
        preconditionFailure("foreign app focus must stop")
    } catch let error as AXFailure { precondition(error.code == .invalidUIElement) }
    catch { preconditionFailure("owner error must be retained") }
    do {
        let _: String = try resolveFocusedField(readApp: {
            throw AXFailure(stage: "ax_app_field", code: .apiDisabled)
        }, readSystem: unexpectedFocusRead,
            ownerCheck: { _ in }, assess: testEligibility)
        preconditionFailure("permission failure must stop")
    } catch let error as AXFailure { precondition(error.code == .apiDisabled) }
    catch { preconditionFailure("permission error must be retained") }
    do {
        let _: String = try resolveFocusedField(readApp: { "wrapper" }, readSystem: { "foreign-input" },
            ownerCheck: { candidate in
                if candidate == "foreign-input" { throw AXFailure(stage: "ax_field_owner", code: .invalidUIElement) }
            }, assess: testEligibility)
        preconditionFailure("foreign system focus must stop")
    } catch let error as AXFailure { precondition(error.code == .invalidUIElement) }
    catch { preconditionFailure("global owner error must be retained") }
    do {
        let _: String = try resolveFocusedField(readApp: { "input" }, readSystem: unexpectedFocusRead,
            ownerCheck: { _ in }, assess: { _ in throw AXFailure(stage: "ax_protected", code: .cannotComplete) })
        preconditionFailure("unreadable safety attributes must stop")
    } catch let error as AXFailure {
        precondition(error.stage == "ax_protected" && !initialFocusCanRetry(error))
    } catch { preconditionFailure("safety attribute error must be retained") }
    do {
        let _: String = try resolveFocusedField(readApp: { "wrapper" }, readSystem: { "input" },
            ownerCheck: { _ in }, assess: { candidate in
                if candidate == "input" { throw AXFailure(stage: "ax_enabled", code: .cannotComplete) }
                return .unsupported(5)
            })
        preconditionFailure("unreadable system safety attributes must stop")
    } catch let error as AXFailure {
        precondition(error.stage == "ax_enabled" && !initialFocusCanRetry(error))
    } catch { preconditionFailure("system safety attribute error must be retained") }
    precondition(expectedInsertion("A😀B", CFRange(location: 1, length: 2), "中文\n🌱") == "A中文\n🌱B")
    precondition(expectedInsertion("abc", CFRange(location: 3, length: 0), "尾") == "abc尾")
    precondition(expectedInsertion("abc", CFRange(location: -1, length: 0), "x") == nil)
    precondition(expectedInsertion("abc", CFRange(location: 2, length: Int.max), "x") == nil)
    precondition(!insertionConfirmed("abc", "abc", "abc"))
    precondition(!insertionConfirmed("abc", "abcd", "abc"))
    precondition(insertionConfirmed("abc", "abcd", "abcd"))
    precondition(!insertionConfirmed("", "é", "e\u{301}"))
    precondition(insertionConfirmed("\u{FEFF}", "\u{FEFF}中文", "中文", emptyWorkBuddyPaste: "中文"))
    precondition(!insertionConfirmed("已有文字", "已有文字中文", "中文", emptyWorkBuddyPaste: "中文"))
    // Pure initial-focus recovery checks: no AX calls and no paste dispatch.
    let transientFocus = FocusReadFailure(app: AXFailure(stage: "ax_app_field", code: .noValue),
                                         system: AXFailure(stage: "ax_system_field", code: .cannotComplete))
    // Discovery preparation happens once even when the focused element needs
    // a transient retry; it never dispatches a key or mutates AX attributes.
    var preparationCount = 0
    var preparedReads = 0
    do {
        let value = try captureInitialFocus(check: {}, prepare: { preparationCount += 1 }, read: {
            precondition(preparationCount == 1)
            preparedReads += 1
            if preparedReads == 1 { throw transientFocus }
            return "prepared-input"
        }, onRetry: { _ in }, pause: {})
        precondition(value == "prepared-input" && preparationCount == 1 && preparedReads == 2)
    } catch { preconditionFailure("one preparation must precede initial and retried focus reads") }
    for code in [AXError.apiDisabled, .cannotComplete, .illegalArgument] {
        do {
            let _: String = try captureInitialFocus(check: {}, prepare: {
                throw AXFailure(stage: "ax_app_role", code: code)
            }, read: { preconditionFailure("failed application-role read must stop before focus") },
                onRetry: { _ in preconditionFailure("preparation must never be retried") },
                pause: { preconditionFailure("preparation failure must not pause") })
            preconditionFailure("application-role failure must stop")
        } catch let error as AXFailure { precondition(error.stage == "ax_app_role" && error.code == code) }
        catch { preconditionFailure("application-role error must be retained") }
    }
    var preparationStable = true
    do {
        let _: String = try captureInitialFocus(check: {
            if !preparationStable { throw FocusChanged() }
        }, prepare: { preparationStable = false },
            read: { preconditionFailure("focus change during preparation must stop before discovery") },
            onRetry: { _ in preconditionFailure("focus change must not retry preparation") })
        preconditionFailure("changed focus after preparation must stop")
    } catch is FocusChanged {} catch { preconditionFailure("focus change must be retained") }
    for reason in ["expired", "receiver_stopped"] {
        var preparationEnded = false
        do {
            let _: String = try captureInitialFocus(check: {
                if preparationEnded { throw ActionStopped(result: reason) }
            }, prepare: { preparationEnded = true },
                read: { preconditionFailure("action ended during preparation must not discover focus") },
                onRetry: { _ in preconditionFailure("ended preparation must not retry") })
            preconditionFailure("ended preparation must stop")
        } catch let error as ActionStopped { precondition(error.result == reason) }
        catch { preconditionFailure("preparation cancellation must be retained") }
        do {
            let _: String = try captureInitialFocus(check: { throw ActionStopped(result: reason) },
                prepare: { preconditionFailure("cancelled action must not prepare accessibility") },
                read: { preconditionFailure("cancelled action must not discover focus") },
                onRetry: { _ in preconditionFailure("cancelled preparation must not retry") })
            preconditionFailure("cancelled action must stop before preparation")
        } catch let error as ActionStopped { precondition(error.result == reason) }
        catch { preconditionFailure("initial cancellation must be retained") }
    }
    precondition(initialFocusCanRetry(transientFocus))
    precondition(!initialFocusCanRetry(FocusReadFailure(
        app: AXFailure(stage: "ax_app_field", code: .apiDisabled), system: transientFocus.system)))
    precondition(!initialFocusCanRetry(FocusReadFailure(
        app: AXFailure(stage: "ax_app_field", code: .invalidUIElement), system: transientFocus.system)))
    precondition(!initialFocusCanRetry(FocusReadFailure(
        app: AXFailure(stage: "ax_app_field", code: .cannotComplete),
        system: AXFailure(stage: "ax_system_field", code: .apiDisabled))))
    precondition(!initialFocusCanRetry(AXFailure(stage: "ax_window", code: .noValue)))
    var focusReads = 0
    var recoveries = 0
    do {
        let value = try captureInitialFocus(check: {}, read: { focusReads += 1; return "ready-input" },
            onRetry: { _ in preconditionFailure("ready input must not retry focus") },
            pause: { preconditionFailure("ready input must not pause") })
        precondition(value == "ready-input" && focusReads == 1)
    } catch { preconditionFailure("ready input must succeed immediately") }
    focusReads = 0
    do {
        let value = try captureInitialFocus(check: {}, read: {
            focusReads += 1
            if focusReads == 1 { throw transientFocus }
            return "same-window-input"
        }, onRetry: { _ in recoveries += 1 }, pause: {})
        precondition(value == "same-window-input" && focusReads == 2 && recoveries == 1)
    } catch { preconditionFailure("initial transient focus should recover") }
    focusReads = 0; recoveries = 0
    do {
        let _: String = try captureInitialFocus(check: {}, read: {
            focusReads += 1; throw transientFocus
        }, onRetry: { _ in recoveries += 1 }, pause: {})
        preconditionFailure("persistent focus failure must stop")
    } catch is FocusReadFailure { precondition(focusReads == 2 && recoveries == 1) }
    catch { preconditionFailure("original focus errors must be retained") }
    for hardError in [AXError.apiDisabled, .invalidUIElement, .attributeUnsupported, .noValue] {
        do {
            let _: String = try captureInitialFocus(check: {}, read: {
                throw AXFailure(stage: "ax_window", code: hardError)
            }, onRetry: { _ in preconditionFailure("hard failures must not retry focus") },
                pause: { preconditionFailure("hard failures must not pause") })
            preconditionFailure("hard focus failure must stop")
        } catch let error as AXFailure { precondition(error.code == hardError) }
        catch { preconditionFailure("hard error must be retained") }
    }
    for reason in ["expired", "receiver_stopped"] {
        do {
            let _: String = try captureInitialFocus(check: { throw ActionStopped(result: reason) },
                read: { preconditionFailure("cancelled action must not read") },
                onRetry: { _ in preconditionFailure("cancelled action must not retry focus") })
            preconditionFailure("cancelled action must stop")
        } catch let error as ActionStopped { precondition(error.result == reason) }
        catch { preconditionFailure("cancellation must be retained") }
        var alive = true
        focusReads = 0
        do {
            let _: String = try captureInitialFocus(check: {
                if !alive { throw ActionStopped(result: reason) }
            }, read: { focusReads += 1; throw transientFocus }, onRetry: { _ in alive = false }, pause: { preconditionFailure("ended action must not pause") })
            preconditionFailure("action ended before retry must stop")
        } catch let error as ActionStopped { precondition(error.result == reason && focusReads == 1) }
        catch { preconditionFailure("retry cancellation must be retained") }
    }
    var targetStable = true
    focusReads = 0
    do {
        let _: String = try captureInitialFocus(check: { if !targetStable { throw FocusChanged() } },
            read: { focusReads += 1; throw transientFocus }, onRetry: { _ in },
            pause: { targetStable = false })
        preconditionFailure("switched foreground must stop recovery")
    } catch is FocusChanged { precondition(focusReads == 1) }
    catch { preconditionFailure("focus change must be retained") }
    focusReads = 0; recoveries = 0
    do {
        let _: String = try captureInitialFocus(check: {}, read: {
            focusReads += 1
            if focusReads == 1 { throw transientFocus }
            throw FocusChanged() // Anchored window changed on the retry.
        }, onRetry: { _ in recoveries += 1 }, pause: {})
        preconditionFailure("switched window must stop recovery")
    } catch is FocusChanged { precondition(focusReads == 2 && recoveries == 1) }
    catch { preconditionFailure("window change must be retained") }
    var focusClock = 0
    do {
        let _: String = try captureInitialFocus(check: {
            if focusClock >= 100 { throw ActionStopped(result: "expired") }
        }, read: { focusClock = 101; return "late-input" },
            onRetry: { _ in preconditionFailure("late result must not retry focus") })
        preconditionFailure("late focus result must be rejected")
    } catch let error as ActionStopped { precondition(error.result == "expired") }
    catch { preconditionFailure("late focus cancellation must be retained") }
    focusClock = 0
    focusReads = 0
    do {
        let _: String = try captureInitialFocus(check: {
            if focusClock >= 100 { throw ActionStopped(result: "expired") }
        }, read: {
            focusReads += 1
            if focusReads == 1 { throw transientFocus }
            focusClock = 101; return "late-recovered-input"
        }, onRetry: { _ in }, pause: {})
        preconditionFailure("late recovered focus result must be rejected")
    } catch let error as ActionStopped { precondition(error.result == "expired" && focusReads == 2) }
    catch { preconditionFailure("late recovery cancellation must be retained") }
    var testClock = 0.0
    var testReads = 0
    let recovered = waitForInsertion(before: "", expected: "pasted", emptyWorkBuddyPaste: nil,
        deadline: 100, ownerAlive: { true }, read: {
            testReads += 1
            if testReads == 1 { throw AXFailure(stage: "verify_value", code: .cannotComplete) }
            return "pasted"
        }, now: { testClock }, pause: { testClock += 25 })
    precondition(recovered.verified && testReads == 2)
    let timedOut = waitForInsertion(before: "", expected: "pasted", emptyWorkBuddyPaste: nil,
        deadline: 100, ownerAlive: { true }, read: { "" },
        now: { testClock }, pause: { testClock += 25 }, observe: { precondition($0 == 1) })
    precondition(!timedOut.verified && testClock == 100)
    testClock = 0
    let transformed = waitForInsertion(before: "", expected: "pasted", emptyWorkBuddyPaste: nil,
        deadline: 100, ownerAlive: { true }, read: { "different" },
        now: { testClock }, pause: { testClock += 25 }, observe: { precondition($0 == 2) })
    precondition(!transformed.verified)
    let ownerStopped = waitForInsertion(before: "", expected: "pasted", emptyWorkBuddyPaste: nil,
        deadline: 200, ownerAlive: { false }, read: { preconditionFailure("must not read after owner exits") }, now: { 100 })
    precondition(!ownerStopped.verified)
    let invalidField = waitForInsertion(before: "", expected: "pasted", emptyWorkBuddyPaste: nil,
        deadline: 200, ownerAlive: { true }, read: { throw AXFailure(stage: "verify_value", code: .invalidUIElement) },
        now: { 100 }, pause: { preconditionFailure("invalid field must not be retried") })
    precondition(!invalidField.verified && invalidField.failure?.code == .invalidUIElement)
    let lateRead = waitForInsertion(before: "", expected: "pasted", emptyWorkBuddyPaste: nil,
        deadline: 200, ownerAlive: { true }, read: { testClock = 201; return "pasted" },
        now: { testClock }, pause: { preconditionFailure("late read must stop") })
    precondition(!lateRead.verified)
    attachmentPureChecks()
    print("pure guard/hash/focus-retry/insertion/attachment checks passed; no AX or clipboard access")
} else if CommandLine.arguments.count == 4, CommandLine.arguments[1] == "--prepare-attachment" {
    print(prepareAttachment(originalPath: CommandLine.arguments[2], pngPath: CommandLine.arguments[3]))
} else if CommandLine.arguments.count == 8, CommandLine.arguments[1] == "--paste-attachment",
          let deadline = Double(CommandLine.arguments[2]), deadline.isFinite,
          CommandLine.arguments[4].range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil,
          let owner = Int32(CommandLine.arguments[6]), owner > 1,
          let requestedPid = Int32(CommandLine.arguments[7]), requestedPid == 0 || requestedPid > 1 {
    // PID 0 means capture the foreground process once at this explicit command
    // entry. All subsequent focus checks remain anchored to that exact PID.
    let target = requestedPid == 0 ? NSWorkspace.shared.frontmostApplication?.processIdentifier ?? 0 : requestedPid
    print(runAttachment(deadline: deadline, originalPath: CommandLine.arguments[3], expectedHash: CommandLine.arguments[4],
        pngPath: CommandLine.arguments[5] == "-" ? nil : CommandLine.arguments[5], owner: owner, expectedPid: target))
} else if CommandLine.arguments.count == 5, let deadline = Double(CommandLine.arguments[1]),
          deadline.isFinite, let owner = Int32(CommandLine.arguments[3]), owner > 1,
          let expectedPid = Int32(CommandLine.arguments[4]), expectedPid > 1,
          CommandLine.arguments[2].range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil {
    print(run(deadline: deadline, expectedHash: CommandLine.arguments[2], owner: owner, expectedPid: expectedPid))
} else { print("paste_failed") }
