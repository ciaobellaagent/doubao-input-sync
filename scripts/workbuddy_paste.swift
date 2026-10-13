import AppKit
import ApplicationServices
import CryptoKit
import Foundation
import Darwin

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
func note(_ stage: String, _ code: Int32) {
    let directory = URL(fileURLWithPath: NSHomeDirectory()).appendingPathComponent("Library/Logs/DoubaoInputSync")
    do {
        try FileManager.default.createDirectory(at: directory, withIntermediateDirectories: true,
                                                attributes: [.posixPermissions: 0o700])
        let path = directory.appendingPathComponent("native-errors.log").path
        let fd = open(path, O_WRONLY | O_APPEND | O_CREAT | O_NOFOLLOW, 0o600)
        guard fd >= 0 else { return }
        defer { close(fd) }
        let line = "\(Date().timeIntervalSince1970 * 1000)|\(stage)|\(code)\n"
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
func focus(_ system: AXUIElement, _ expectedPid: pid_t) throws -> Focus {
    guard NSWorkspace.shared.frontmostApplication?.processIdentifier == expectedPid else { throw FocusChanged() }
    let app = AXUIElementCreateApplication(expectedPid)
    AXUIElementSetMessagingTimeout(app, 0.25)
    let pid = expectedPid
    // Use the confirmed front app directly: querying focusedApplication on the
    // global AX object can fail even when its app-level interface works.
    let field: AXUIElement
    do { field = try elementAttribute(app, kAXFocusedUIElementAttribute, "ax_app_field") }
    catch { field = try elementAttribute(system, kAXFocusedUIElementAttribute, "ax_system_field") }
    var fieldPid: pid_t = 0
    guard AXUIElementGetPid(field, &fieldPid) == .success, fieldPid == pid else {
        throw AXFailure(stage: "ax_field_owner", code: .invalidUIElement)
    }
    let window = try elementAttribute(app, kAXFocusedWindowAttribute, "ax_window")
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
func roleCheck(_ field: AXUIElement) throws -> String? {
    guard let role = try attribute(field, kAXRoleAttribute, "ax_role") as? String else {
        throw AXFailure(stage: "ax_role_type", code: .illegalArgument)
    }
    let subrole = try optionalString(field, kAXSubroleAttribute, "ax_subrole") ?? ""
    let enabled = try optionalBool(field, kAXEnabledAttribute, "ax_enabled") ?? true
    let protected = try optionalBool(field, "AXProtectedContent", "ax_protected") ?? false
    if protected { return "unsafe_target" }
    return inputResult(role: role, subrole: subrole, enabled: enabled)
}
func modifiersHeld() -> Bool {
    !NSEvent.modifierFlags.intersection([.shift, .control, .option, .command]).isEmpty
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
                      pause: () -> Void = { Thread.sleep(forTimeInterval: 0.025) }) -> (verified: Bool, failure: AXFailure?) {
    var lastFailure: AXFailure?
    while now() < deadline && ownerAlive() {
        do {
            let actual = try read()
            lastFailure = nil
            if insertionConfirmed(before, expected, actual, emptyWorkBuddyPaste: emptyWorkBuddyPaste) {
                return (now() < deadline && ownerAlive(), nil)
            }
        } catch let error as AXFailure {
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
    let clipboard = NSPasteboard.general
    do {
        guard NSWorkspace.shared.frontmostApplication?.processIdentifier == expectedPid else { return "focus_changed" }
        let app = AXUIElementCreateApplication(expectedPid)
        AXUIElementSetMessagingTimeout(app, 0.25)
        if ["com.tencent.workbuddy.mac", "com.google.antigravity-ide"].contains(NSRunningApplication(processIdentifier: expectedPid)?.bundleIdentifier ?? "") {
            _ = AXUIElementSetAttributeValue(app, "AXManualAccessibility" as CFString, kCFBooleanTrue)
        }
        let initial = try focus(system, expectedPid)
        note("native_target_pid", initial.pid)
        guard initial.pid == expectedPid else {
            return "focus_changed"
        }
        if let result = try roleCheck(initial.field) { return result }
        if modifiersHeld() { return "modifier_held" }
        while !expired() && !ownerGone() {
            guard sameFocus(initial, try focus(system, expectedPid)) else { return "focus_changed" }
            let count = clipboard.changeCount
            if let text = clipboard.string(forType: .string), digest(text) == expectedHash {
                guard clipboard.changeCount == count, clipboard.string(forType: .string) == text else {
                    return "clipboard_changed"
                }
                guard sameFocus(initial, try focus(system, expectedPid)) else { return "focus_changed" }
                if let result = try roleCheck(initial.field) { return result }
                if modifiersHeld() { return "modifier_held" }
                guard !expired(), !ownerGone() else { return "expired" }
                guard clipboard.changeCount == count, clipboard.string(forType: .string) == text else {
                    return "clipboard_changed"
                }
                let before = try inputSnapshot(initial.field)
                guard let expected = expectedInsertion(before.text, before.range, text),
                      !before.text.utf16.elementsEqual(expected.utf16) else {
                    note("verify_unobservable", 0); return "paste_failed"
                }
                let menuItem = isWorkBuddy ? try pasteMenuItem(initial.app) : nil
                // Leave time to observe insertion, all within the original deadline.
                guard deadline - Date().timeIntervalSince1970 * 1000 >= 800 else { return "expired" }
                guard sameFocus(initial, try focus(system, expectedPid)) else { return "focus_changed" }
                guard sameSnapshot(before, try inputSnapshot(initial.field)) else { return "focus_changed" }
                guard !expired(), !ownerGone() else { return "expired" }
                if modifiersHeld() { return "modifier_held" }
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
                          let down = CGEvent(keyboardEventSource: source, virtualKey: 9, keyDown: true),
                          let up = CGEvent(keyboardEventSource: source, virtualKey: 9, keyDown: false) else { return "paste_failed" }
                    down.flags = .maskCommand; up.flags = .maskCommand
                    down.post(tap: .cghidEventTap); up.post(tap: .cghidEventTap)
                    note("key_paste_dispatch", 0)
                }
                // Dispatch success is not insertion success. Never retry/fallback
                // after a command may have been delivered. No field text is logged.
                // Read the exact captured field. Re-querying global/app focus
                // during menu dismissal can fail even when insertion succeeded.
                // Focus may change after dispatch; confirmed text in the original
                // field is still evidence of insertion. No other field is read.
                AXUIElementSetMessagingTimeout(initial.field, 0.1)
                let confirmation = waitForInsertion(before: before.text, expected: expected,
                    emptyWorkBuddyPaste: isWorkBuddy ? text : nil, deadline: deadline,
                    ownerAlive: { !ownerGone() }, read: {
                        if try roleCheck(initial.field) != nil {
                            throw AXFailure(stage: "verify_target_changed", code: .illegalArgument)
                        }
                        return try fieldText(initial.field)
                    })
                if confirmation.verified {
                    note("paste_verified", 0)
                    return "executed"
                }
                if let failure = confirmation.failure { note(failure.stage, failure.code.rawValue) }
                note("paste_unconfirmed", 0)
                return "paste_failed"
            }
            Thread.sleep(forTimeInterval: 0.025)
        }
        return ownerGone() ? "receiver_stopped" : "clipboard_timeout"
    } catch is FocusChanged {
        // Only pre-dispatch focus checks throw here. Verification catches its
        // own errors and always reports an unconfirmed result after dispatch.
        return "focus_changed"
    } catch let error as AXFailure {
        note(error.stage, error.code.rawValue)
        return "paste_failed"
    } catch {
        note("native_exception", 0)
        return "paste_failed"
    }
}

if CommandLine.arguments == [CommandLine.arguments[0], "--self-test"] {
    precondition(digest("hello") == "2cf24dba5fb0a30e26e83b2ac5b9e29e1b161e5c1fa7425e73043362938b9824")
    for role in allowedRoles { precondition(inputResult(role: role, subrole: "", enabled: true) == nil) }
    precondition(inputResult(role: "AXTextField", subrole: "AXSecureTextField", enabled: true) == "unsafe_target")
    precondition(inputResult(role: "AXWebArea", subrole: "", enabled: true) == "no_focused_input")
    precondition(inputResult(role: "AXTextArea", subrole: "", enabled: false) == "no_focused_input")
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
        now: { testClock }, pause: { testClock += 25 })
    precondition(!timedOut.verified && testClock == 100)
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
    print("pure guard/hash/insertion checks passed; no AX or clipboard access")
} else if CommandLine.arguments.count == 5, let deadline = Double(CommandLine.arguments[1]),
          deadline.isFinite, let owner = Int32(CommandLine.arguments[3]), owner > 1,
          let expectedPid = Int32(CommandLine.arguments[4]), expectedPid > 1,
          CommandLine.arguments[2].range(of: "^[0-9a-f]{64}$", options: .regularExpression) != nil {
    print(run(deadline: deadline, expectedHash: CommandLine.arguments[2], owner: owner, expectedPid: expectedPid))
} else { print("paste_failed") }
