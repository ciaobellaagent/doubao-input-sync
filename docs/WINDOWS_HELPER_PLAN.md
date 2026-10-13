# Windows relay helpers

Windows helpers consume relay captures from Flow or Breathing mode. They do **not** implement the Mac prepared clipboard-match protocol. Copy-only archives are not delivery events.

Requirements: Python 3.10+, PowerShell and a permitted connection to your own relay. Use one room per destination, and run only one consumer for that room. Room separation is routing, not authorization.

## Start with clipboard-only delivery

```powershell
python scripts/windows_paste_helper.py --server-url http://127.0.0.1:18766 --room-id demo --mode clipboard
```

For a remote relay, replace the URL with your own protected endpoint. The default loopback address is usable only when the relay is on this Windows computer. A web browser login does not automatically provide credentials to the helper.

Use a disposable room and synthetic text. Select Breathing mode on the phone, explicitly send, and confirm the exact clipboard text once. The helper ignores existing archives at startup by default and consumes new captures. It acknowledges delivery and uses streaming with a polling fallback.

## Optional foreground paste

```powershell
python scripts/windows_paste_helper.py --server-url http://127.0.0.1:18766 --room-id demo --mode paste
```

The helper writes Unicode clipboard text, then uses PowerShell COM `WScript.Shell.SendKeys('^v')` for the foreground target. Keep the intended input focused when the capture arrives. This route does not provide the Mac prepared path's focus checks or exact insertion readback. A delivery receipt does not establish that the intended app inserted the text.

The PowerShell wrappers accept `SERVER_URL` and `ROOM_ID` environment variables if preferred. For Cloudflare Access deployments, the helper supports externally supplied `CF_ACCESS_CLIENT_ID` and `CF_ACCESS_CLIENT_SECRET`; keep them in a local secret manager and out of shell history, source files and reports. Configure proxy authorization separately. No shared hosted endpoint or credentials are bundled.

## Acceptance scope

Automated tests cover protocol and helper behavior with mocks. Validate clipboard-only delivery, exact-once handling during reconnect, explicit paste into a test editor and behavior after focus changes on your Windows system. Then test the intended application. No universal Windows or application compatibility is claimed.
