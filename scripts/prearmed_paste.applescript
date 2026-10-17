use framework "AppKit"
use framework "Foundation"
use scripting additions

property diagnosticTraceId : ""
property diagnosticCount : 0
property diagnosticCapturePending : true
property diagnosticLegacyStarted : false

-- Only the explicitly authorized action starts this process. The body never
-- travels in argv or stdout. This path never writes the pasteboard.
on run argv
    set deadlineMs to (item 1 of argv) as real
    set expectedHash to item 2 of argv
    set diagnosticStage to "start"
    set diagnosticTraceId to current application's NSUUID's UUID()'s UUIDString()'s lowercaseString() as text
    set diagnosticCount to 0
    set diagnosticCapturePending to true
    set diagnosticLegacyStarted to false
    set incomingTraceId to current application's NSProcessInfo's processInfo()'s environment()'s objectForKey:"DOUBAO_PASTE_TRACE_ID"
    if incomingTraceId is not missing value then
        set parsedId to current application's NSUUID's alloc()'s initWithUUIDString:incomingTraceId
        if parsedId is not missing value then set diagnosticTraceId to parsedId's UUIDString()'s lowercaseString() as text
    end if
    try
        if my nowMs() ≥ deadlineMs then return "expired"
        tell application "System Events"
            set diagnosticStage to "target_process"
            set targetPid to my frontmostPid()
            my progress("as_start", 0)
            my recordFailure("workspace_target_pid", targetPid)
            my progress("as_legacy_lookup", 0)
            try
                set legacyPid to unix id of first application process whose frontmost is true
                if legacyPid is not targetPid then my recordFailure("system_events_disagrees_pid", legacyPid)
            end try
            my progress("as_process_lookup", 0)
            set targetProcess to first application process whose unix id is targetPid
            my progress("as_bundle_lookup", 0)
            if (bundle identifier of targetProcess) is "com.tencent.workbuddy.mac" then
                set diagnosticStage to "native_workbuddy"
                return my pasteNative(deadlineMs, expectedHash, targetPid)
            end if
            try
                set diagnosticStage to "focused_input"
                my progress("as_focused_input", 0)
                set initialField to value of attribute "AXFocusedUIElement" of targetProcess
                set diagnosticStage to "focused_window"
                my progress("as_focused_window", 0)
                set initialWindow to value of attribute "AXFocusedWindow" of targetProcess
                set diagnosticStage to "input_role"
                my progress("as_input_role", 0)
                set fieldRole to value of attribute "AXRole" of initialField
                set safetyResult to my fieldSafety(initialField)
            on error lookupText number lookupCode
                if my nativeFocusError(diagnosticStage, lookupCode) then
                    -- System Events may not represent the focused AX object
                    -- or its metadata types. Nothing has been dispatched.
                    -- Native AX must independently confirm the same front PID
                    -- and a safe editable field before it may send a key.
                    my recordFailure("system_events_representation_error", lookupCode)
                    my recordFailure("system_events_target_pid", targetPid)
                    return my pasteNative(deadlineMs, expectedHash, targetPid)
                end if
                error number lookupCode
            end try
            if safetyResult is "native_focus" then
                -- A browser may expose a wrapper to System Events. Native AX
                -- must independently resolve the actual focused editable field.
                -- No key has been dispatched and no broad role is allowed here.
                my recordFailure("system_events_nontext_role", my roleCode(fieldRole))
                my recordFailure("system_events_target_pid", targetPid)
                return my pasteNative(deadlineMs, expectedHash, targetPid)
            end if
            if safetyResult is not "ready" then return safetyResult
        end tell
        set diagnosticCapturePending to false
        my progress("as_focus_ready", 0)
        set diagnosticStage to "modifiers"
        if my modifiersHeld() then return "modifier_held"
        my progress("as_clipboard_wait", 0)
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
                my progress("as_clipboard_matched", 0)
                if not my exactClipboard(localText, beforeChange) then return "clipboard_changed"
                -- Intent does not prove dispatch. All final safety checks run
                -- after the diagnostic write, including the exact field/window.
                my progress("as_dispatch_intent", 0)
                tell application "System Events"
                    set currentPid to my frontmostPid()
                    if currentPid is not targetPid then return "focus_changed"
                    set currentProcess to targetProcess
                    if unix id of currentProcess is not targetPid then return "focus_changed"
                    if (value of attribute "AXFocusedWindow" of currentProcess) is not initialWindow then return "focus_changed"
                    if (value of attribute "AXFocusedUIElement" of currentProcess) is not initialField then return "focus_changed"
                    set diagnosticStage to "input_role"
                    set safetyResult to my fieldSafety(initialField)
                    if safetyResult is "native_focus" then return "focus_changed"
                    if safetyResult is not "ready" then return safetyResult
                    if my modifiersHeld() then return "modifier_held"
                    if my nowMs() ≥ deadlineMs then return "expired"
                    if not my exactClipboard(localText, beforeChange) then return "clipboard_changed"
                    if my frontmostPid() is not targetPid then return "focus_changed"
                    if my nowMs() ≥ deadlineMs then return "expired"
                    set diagnosticStage to "key_dispatch"
                    keystroke "v" using command down
                    my progress("as_dispatch_returned", 0)
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

-- Each action reloads this script, including on legacy running receivers.
-- New receivers filter a dedicated stderr pipe. Legacy receivers get bounded
-- local phase logs with a diagnostic UUID (not the unavailable server action ID).
on recordFailure(stageName, errorCode)
    -- Preserve existing failure/PID records on a legacy receiver. Added initial
    -- phase records stay pipe-only until the original field/window is captured.
    if my traceEnabled() then
        my progress(stageName, errorCode)
    else
        my legacyProgress(stageName, errorCode)
    end if
end recordFailure

on traceEnabled()
    try
        set flagValue to current application's NSProcessInfo's processInfo()'s environment()'s objectForKey:"DOUBAO_PASTE_TRACE"
        if flagValue is missing value then return false
        return (flagValue as text) is "1"
    on error
        return false
    end try
end traceEnabled

on progress(stageName, errorCode)
    try
        if diagnosticCount ≥ 64 then return
        set diagnosticCount to diagnosticCount + 1
        if my traceEnabled() then
            set diagnosticLine to "DOUBAO_TRACE|" & stageName & "|" & (errorCode as text) & linefeed
            set diagnosticData to (current application's NSString's stringWithString:diagnosticLine)'s dataUsingEncoding:(current application's NSUTF8StringEncoding)
            current application's NSFileHandle's fileHandleWithStandardError()'s writeData:diagnosticData
        else
            if diagnosticCapturePending then return
            my legacyProgress(stageName, errorCode)
        end if
    end try
end progress

on legacyProgress(stageName, errorCode)
    try
        set fm to current application's NSFileManager's defaultManager()
        set logDirectory to (current application's NSHomeDirectory() as text) & "/Library/Logs/DoubaoInputSync"
        set permissions to current application's NSDictionary's dictionaryWithObject:448 forKey:(current application's NSFilePosixPermissions)
        fm's createDirectoryAtPath:logDirectory withIntermediateDirectories:true attributes:permissions |error|:(missing value)
        set logPath to logDirectory & "/native-errors.log"
        -- Rotate once at the first phase, reserving room for this bounded action.
        -- Reject special files/links; diagnostic failure never changes the result.
        repeat with suffix in {"", ".1", ".2"}
            set candidate to logPath & suffix
            if (fm's fileExistsAtPath:candidate) as boolean then
                set attrs to fm's attributesOfItemAtPath:candidate |error|:(missing value)
                if (attrs's objectForKey:(current application's NSFileType)) as text is not (current application's NSFileTypeRegular as text) then return
                if (attrs's objectForKey:(current application's NSFileSize)) as real > 2097152 then
                    set oversizedHandle to current application's NSFileHandle's fileHandleForWritingAtPath:candidate
                    if oversizedHandle is missing value then return
                    oversizedHandle's truncateFileAtOffset:0
                    oversizedHandle's closeFile()
                end if
            end if
        end repeat
        if not diagnosticLegacyStarted and (fm's fileExistsAtPath:logPath) as boolean then
            set attrs to fm's attributesOfItemAtPath:logPath |error|:(missing value)
            if (attrs's objectForKey:(current application's NSFileSize)) as real > 2080768 then
                fm's removeItemAtPath:(logPath & ".2") |error|:(missing value)
                if (fm's fileExistsAtPath:(logPath & ".1")) as boolean then
                    if not (fm's moveItemAtPath:(logPath & ".1") toPath:(logPath & ".2") |error|:(missing value)) as boolean then return
                end if
                if not (fm's moveItemAtPath:logPath toPath:(logPath & ".1") |error|:(missing value)) as boolean then return
            end if
        end if
        set diagnosticLegacyStarted to true
        if not (fm's fileExistsAtPath:logPath) as boolean then
            set filePermissions to current application's NSDictionary's dictionaryWithObject:384 forKey:(current application's NSFilePosixPermissions)
            if not (fm's createFileAtPath:logPath |contents|:(missing value) attributes:filePermissions) as boolean then return
        end if
        set diagnosticLine to (my nowMs() as text) & "|" & stageName & "|" & (errorCode as text) & "|" & diagnosticTraceId & linefeed
        set diagnosticData to (current application's NSString's stringWithString:diagnosticLine)'s dataUsingEncoding:(current application's NSUTF8StringEncoding)
        set handle to current application's NSFileHandle's fileHandleForWritingAtPath:logPath
        if handle is missing value then return
        set currentSize to handle's seekToEndOfFile()
        if (currentSize as real) + (diagnosticData's |length|() as real) > 2097152 then
            handle's closeFile()
            return
        end if
        handle's writeData:diagnosticData
        handle's closeFile()
    end try
end legacyProgress

on pasteNative(deadlineMs, expectedHash, targetPid)
    set nativePath to (current application's NSHomeDirectory() as text) & "/Library/Application Support/DoubaoInputSync/native/workbuddy-paste"
    set ownerPid to current application's NSProcessInfo's processInfo()'s processIdentifier() as integer
    my progress("as_native_handoff", 0)
    set nativeCommand to quoted form of nativePath
    if diagnosticTraceId is not "" then set nativeCommand to "DOUBAO_PASTE_TRACE_ID=" & quoted form of diagnosticTraceId & " " & nativeCommand
    return do shell script nativeCommand & " " & quoted form of (deadlineMs as text) & " " & quoted form of expectedHash & " " & (ownerPid as text) & " " & (targetPid as text)
end pasteNative

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
        if "AXSubrole" is in attributeNames then set fieldSubrole to my optionalAXText(value of attribute "AXSubrole" of theField, "")
        if "AXEnabled" is in attributeNames then set fieldEnabled to my optionalAXBool(value of attribute "AXEnabled" of theField, true)
        if "AXProtectedContent" is in attributeNames then set fieldProtected to my optionalAXBool(value of attribute "AXProtectedContent" of theField, false)
    end tell
    return my inputRoute(fieldRole, fieldSubrole, fieldEnabled, fieldProtected)
end fieldSafety

-- Fixed diagnostic codes only; never record AX labels, descriptions or values.
on roleCode(fieldRole)
    if fieldRole is "AXWebArea" then return 4
    if fieldRole is "AXGroup" then return 5
    if fieldRole is "AXUnknown" then return 6
    if fieldRole is "AXWindow" then return 7
    if fieldRole is "AXScrollArea" then return 8
    return 0
end roleCode

on nativeFocusError(stageName, errorCode)
    return stageName is in {"focused_input", "focused_window", "input_role"} and errorCode is in {-1728, -1700}
end nativeFocusError

-- Chrome can advertise an optional attribute and return missing value.
-- Normalize absence only, matching native AX noValue handling. Actual read
-- exceptions still propagate; present values must have the exact expected type.
on optionalAXText(attributeValue, defaultValue)
    if attributeValue is missing value then return defaultValue
    if class of attributeValue is not text then error number -1700
    return attributeValue
end optionalAXText

on optionalAXBool(attributeValue, defaultValue)
    if attributeValue is missing value then return defaultValue
    if class of attributeValue is not boolean then error number -1700
    return attributeValue
end optionalAXBool

on inputRoute(fieldRole, fieldSubrole, fieldEnabled, fieldProtected)
    set safetyStatus to my inputSafety(fieldRole, fieldSubrole, fieldEnabled, fieldProtected)
    if safetyStatus is "no_focused_input" and fieldEnabled and fieldRole is not in {"AXTextArea", "AXTextField", "AXComboBox"} then return "native_focus"
    return safetyStatus
end inputRoute

-- Pure policy, also exercised without accessibility or clipboard access.
on inputSafety(fieldRole, fieldSubrole, fieldEnabled, fieldProtected)
    if class of fieldRole is not text or class of fieldSubrole is not text then error number -1700
    if class of fieldEnabled is not boolean or class of fieldProtected is not boolean then error number -1700
    if fieldSubrole is "AXSecureTextField" or fieldProtected then return "unsafe_target"
    if fieldRole is not in {"AXTextArea", "AXTextField", "AXComboBox"} or not fieldEnabled then return "no_focused_input"
    return "ready"
end inputSafety
