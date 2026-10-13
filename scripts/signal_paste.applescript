use framework "AppKit"
use framework "Foundation"
use scripting additions

-- No clipboard access: only inspect focus, wait briefly, and emit Command-V.
on run argv
    set deadlineMs to (item 1 of argv) as real
    set waitSeconds to (item 2 of argv) as real
    try
        if my nowMs() ≥ deadlineMs then return "expired"
        tell application "System Events"
            set targetProcess to first application process whose frontmost is true
            set targetPid to unix id of targetProcess
            set targetBundle to bundle identifier of targetProcess
            -- The initial trial is deliberately limited to known text editors.
            -- IDEs/browsers can expose terminals as ordinary AXTextArea fields.
            if targetBundle is not in {"com.apple.TextEdit", "com.apple.Notes"} then return "unsafe_target"
            set initialField to value of attribute "AXFocusedUIElement" of targetProcess
            set initialWindow to value of attribute "AXFocusedWindow" of targetProcess
            set fieldRole to value of attribute "AXRole" of initialField
            set safetyResult to my fieldSafety(initialField)
            if safetyResult is not "ready" then return safetyResult
        end tell
        if my modifiersHeld() then return "modifier_held"
        delay waitSeconds
        if my nowMs() ≥ deadlineMs then return "expired"
        tell application "System Events"
            set currentProcess to first application process whose frontmost is true
            if unix id of currentProcess is not targetPid then return "focus_changed"
            if (value of attribute "AXFocusedWindow" of currentProcess) is not initialWindow then return "focus_changed"
            if (value of attribute "AXFocusedUIElement" of currentProcess) is not initialField then return "focus_changed"
            set safetyResult to my fieldSafety(initialField)
            if safetyResult is not "ready" then return safetyResult
            if my modifiersHeld() then return "modifier_held"
            if my nowMs() ≥ deadlineMs then return "expired"
            keystroke "v" using command down
        end tell
        return "executed"
    on error errText number errNumber
        if errNumber is -1743 or errNumber is -1719 or errNumber is -25211 then return "permission_missing"
        return "paste_failed"
    end try
end run

on nowMs()
    return (current application's NSDate's timeIntervalSinceReferenceDate() + 978307200) * 1000
end nowMs

on modifiersHeld()
    set flags to (current application's NSEvent's modifierFlags()) as integer
    -- Shift, Control, Option and Command; ignore Caps Lock and numeric keypad.
    repeat with mask in {131072, 262144, 524288, 1048576}
        if (flags div mask) mod 2 is 1 then return true
    end repeat
    return false
end modifiersHeld

-- Missing optional attributes are allowed; failed reads are not. In particular,
-- a transient AX/permission error must never turn a protected field into an
-- ordinary input. Check again just before dispatch in case the field changed.
on fieldSafety(theField)
    tell application "System Events"
        set fieldRole to value of attribute "AXRole" of theField
        set attributeNames to name of every attribute of theField
        if class of attributeNames is not list then error number -1700
        set fieldSubrole to ""
        set fieldEnabled to true
        set fieldProtected to false
        if "AXSubrole" is in attributeNames then set fieldSubrole to value of attribute "AXSubrole" of theField
        if "AXEnabled" is in attributeNames then set fieldEnabled to value of attribute "AXEnabled" of theField
        if "AXProtectedContent" is in attributeNames then set fieldProtected to value of attribute "AXProtectedContent" of theField
    end tell
    return my inputSafety(fieldRole, fieldSubrole, fieldEnabled, fieldProtected)
end fieldSafety

-- Pure policy, also exercised without accessibility or clipboard access.
on inputSafety(fieldRole, fieldSubrole, fieldEnabled, fieldProtected)
    if class of fieldRole is not text or class of fieldSubrole is not text then error number -1700
    if class of fieldEnabled is not boolean or class of fieldProtected is not boolean then error number -1700
    if fieldSubrole is "AXSecureTextField" or fieldProtected then return "unsafe_target"
    if fieldRole is not in {"AXTextArea", "AXTextField", "AXComboBox"} or not fieldEnabled then return "no_focused_input"
    return "ready"
end inputSafety
