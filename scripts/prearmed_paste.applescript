use framework "AppKit"
use framework "Foundation"
use scripting additions

-- Only the explicitly authorized action starts this process. The body never
-- travels in argv or stdout. This path never writes the pasteboard.
on run argv
    set deadlineMs to (item 1 of argv) as real
    set expectedHash to item 2 of argv
    set diagnosticStage to "start"
    try
        if my nowMs() ≥ deadlineMs then return "expired"
        tell application "System Events"
            set diagnosticStage to "target_process"
            set targetPid to my frontmostPid()
            my recordFailure("workspace_target_pid", targetPid)
            try
                set legacyPid to unix id of first application process whose frontmost is true
                if legacyPid is not targetPid then my recordFailure("system_events_disagrees_pid", legacyPid)
            end try
            set targetProcess to first application process whose unix id is targetPid
            if (bundle identifier of targetProcess) is "com.tencent.workbuddy.mac" then
                set diagnosticStage to "native_workbuddy"
                return my pasteInWorkBuddy(deadlineMs, expectedHash, targetPid)
            end if
            try
                set diagnosticStage to "focused_input"
                set initialField to value of attribute "AXFocusedUIElement" of targetProcess
                set diagnosticStage to "focused_window"
                set initialWindow to value of attribute "AXFocusedWindow" of targetProcess
                set diagnosticStage to "input_role"
                set fieldRole to value of attribute "AXRole" of initialField
            on error lookupText number lookupCode
                if lookupCode is -1728 then
                    -- System Events may not represent the focused AX object.
                    -- Native AX must independently confirm the same front PID
                    -- and a safe editable field before it may send a key.
                    my recordFailure("system_events_target_pid", targetPid)
                    return my pasteInWorkBuddy(deadlineMs, expectedHash, targetPid)
                end if
                error number lookupCode
            end try
            set safetyResult to my fieldSafety(initialField)
            if safetyResult is not "ready" then return safetyResult
        end tell
        set diagnosticStage to "modifiers"
        if my modifiersHeld() then return "modifier_held"
        repeat
            if my nowMs() ≥ deadlineMs then return "clipboard_timeout"
            set diagnosticStage to "focus_recheck"
            tell application "System Events"
                set currentPid to my frontmostPid()
                if currentPid is not targetPid then return "focus_changed"
                set currentProcess to targetProcess
                if unix id of currentProcess is not targetPid then return "focus_changed"
                if (value of attribute "AXFocusedWindow" of currentProcess) is not initialWindow then return "focus_changed"
                if (value of attribute "AXFocusedUIElement" of currentProcess) is not initialField then return "focus_changed"
            end tell
            set diagnosticStage to "clipboard_read"
            set beforeChange to (current application's NSPasteboard's generalPasteboard()'s changeCount()) as integer
            set localText to my clipboardText()
            -- Static command only: clipboard plaintext stays in the local pipe.
            -- LC_ALL enforces UTF-8 pbpaste, matching browser TextEncoder SHA-256.
            set diagnosticStage to "clipboard_hash"
            set hashLine to do shell script "LC_ALL=en_US.UTF-8 /usr/bin/pbpaste | /usr/bin/shasum -a 256"
            if (text 1 thru 64 of hashLine) is expectedHash then
                if not my exactClipboard(localText, beforeChange) then return "clipboard_changed"
                tell application "System Events"
                    set currentPid to my frontmostPid()
                    if currentPid is not targetPid then return "focus_changed"
                    set currentProcess to targetProcess
                    if unix id of currentProcess is not targetPid then return "focus_changed"
                    if (value of attribute "AXFocusedWindow" of currentProcess) is not initialWindow then return "focus_changed"
                    if (value of attribute "AXFocusedUIElement" of currentProcess) is not initialField then return "focus_changed"
                    set diagnosticStage to "input_role"
                    set safetyResult to my fieldSafety(initialField)
                    if safetyResult is not "ready" then return safetyResult
                    if my modifiersHeld() then return "modifier_held"
                    if my nowMs() ≥ deadlineMs then return "expired"
                    if not my exactClipboard(localText, beforeChange) then return "clipboard_changed"
                    if my frontmostPid() is not targetPid then return "focus_changed"
                    if my nowMs() ≥ deadlineMs then return "expired"
                    set diagnosticStage to "key_dispatch"
                    keystroke "v" using command down
                end tell
                return "executed"
            end if
            delay 0.025
        end repeat
    on error errText number errNumber
        my recordFailure(diagnosticStage, errNumber)
        if errNumber is -1743 or errNumber is -1719 or errNumber is -25211 then return "permission_missing"
        return "paste_failed|" & diagnosticStage & "|" & errNumber
    end try
end run

on clipboardText()
    set t to current application's NSPasteboard's generalPasteboard()'s stringForType:(current application's NSPasteboardTypeString)
    if t is missing value then return missing value
    return t as text
end clipboardText

on exactClipboard(expectedText, expectedChange)
    set pb to current application's NSPasteboard's generalPasteboard()
    if (pb's changeCount() as integer) is not expectedChange then return false
    set actualText to pb's stringForType:(current application's NSPasteboardTypeString)
    if actualText is missing value or expectedText is missing value then return false
    return (actualText's isEqualToString:expectedText) as boolean
end exactClipboard

on nowMs()
    return (current application's NSDate's timeIntervalSinceReferenceDate() + 978307200) * 1000
end nowMs

on modifiersHeld()
    set flags to (current application's NSEvent's modifierFlags()) as integer
    repeat with mask in {131072, 262144, 524288, 1048576}
        if (flags div mask) mod 2 is 1 then return true
    end repeat
    return false
end modifiersHeld

-- Each action loads this script afresh. Keep bounded diagnostics available even
-- to an already-running receiver; never record error text, field values or body.
on recordFailure(stageName, errorCode)
    try
        set logDirectory to (current application's NSHomeDirectory() as text) & "/Library/Logs/DoubaoInputSync"
        set logPath to logDirectory & "/native-errors.log"
        set diagnosticLine to (my nowMs() as text) & "|" & stageName & "|" & (errorCode as text)
        do shell script "umask 077; /bin/mkdir -p " & quoted form of logDirectory & "; /usr/bin/printf '%s\\n' " & quoted form of diagnosticLine & " >> " & quoted form of logPath
    end try
end recordFailure

on pasteInWorkBuddy(deadlineMs, expectedHash, targetPid)
    set nativePath to (current application's NSHomeDirectory() as text) & "/Library/Application Support/DoubaoInputSync/native/workbuddy-paste"
    set ownerPid to current application's NSProcessInfo's processInfo()'s processIdentifier() as integer
    return do shell script quoted form of nativePath & " " & quoted form of (deadlineMs as text) & " " & quoted form of expectedHash & " " & (ownerPid as text) & " " & (targetPid as text)
end pasteInWorkBuddy

on frontmostPid()
    set frontApp to current application's NSWorkspace's sharedWorkspace()'s frontmostApplication()
    if frontApp is missing value then return -1
    return frontApp's processIdentifier() as integer
end frontmostPid

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
