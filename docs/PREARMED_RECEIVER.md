# macOS prepared clipboard receiver

The prepared receiver connects an explicit phone action to text that an input method has already synchronized to the Mac clipboard. It does not transfer the paste body from the relay or infer authorization from a fingerprint match.

## Start and stop

Requirements: macOS, Python 3.10+, Xcode Command Line Tools for Swift compilation, and accessibility/automation permission for the actual terminal/runtime host. The native helper checks existing permissions and does not request or grant them.

```sh
python3 scripts/install_workbuddy_native.py
python3 scripts/mac_prearmed_receiver.py --server-url http://127.0.0.1:18766 --room demo
```

Replace the URL and room with your own protected relay configuration. A browser's gateway login does not authenticate this process. Its control requests have no HTTP Basic, browser-cookie or Cloudflare service-token option; use loopback or a private device-authorized route as described in [Operations](OPERATIONS.md). The receiver runs until stopped, or until an optional `--duration` limit in seconds. Stop it with Ctrl-C. This does not install startup automation. It uses a local exclusive lock; stop competing consumers yourself before starting it.

The installer writes its binary and build receipt beneath `~/Library/Application Support/DoubaoInputSync/native/`. It preserves previous binaries for local rollback. Build receipts and runtime logs belong on the local device, not in published release artifacts. A successful compilation or pure self-test does not demonstrate GUI permission or insertion.

## Protocol

1. Phone typing prepares an exact UTF-8 SHA-256 fingerprint.
2. The receiver starts passive clipboard observation and reports preparation readiness. This cannot paste.
3. A successful phone copy plus explicit click authorizes one action, bound to that preparation and receiver session. The phone may wait up to 15 seconds before dispatch.
4. The Mac checks the deadline, clipboard text/hash/change count, foreground process, window, editable input and modifier keys, then triggers at most one paste.
5. The result returns to the phone. Failure/uncertainty retains the draft; an executed receipt clears only the unchanged submitted text.

Prepared and explicit relay actions share one local action lock. Secure fields and unsupported accessibility roles are refused by the prepared path. Focus must remain stable until dispatch. There is no fixed 500/650 ms sync delay, automatic server-body fallback or second paste attempt after uncertain delivery.

## Native and System Events routes

| Route | Trigger | Confirmation |
| --- | --- | --- |
| WorkBuddy | Accessibility `AXPress` on its standard `paste:` menu item | Read the original field and verify expected insertion |
| Native fallback after a System Events focused-object error or enabled nontext wrapper | `CGEvent` Command-V, with the same expected front process | Resolve the actual focused text field, then read the original field and verify expected insertion |
| Ordinary System Events | `keystroke "v" using command down` | Dispatch only; no generic field readback |

WorkBuddy is an intentional compatibility exception shown in the phone UI. The helper also initializes accessibility for recognized Electron applications. This does not promise support for every editor or establish that menu paste is universally faster or more reliable.

Ordinary apps are not selected by an app-name allowlist. When System Events exposes an enabled nontext wrapper, native AX may independently resolve the real focused field from the same app or the system focus. Both representations must belong to the expected foreground process and stable window. Wrappers are never accepted as text inputs, no descendant search is used to guess a field, and disabled/protected inputs or unreadable safety attributes stop the action. This is generic focus resolution, not a browser- or website-specific driver. The native helper's installed filename retains its historical WorkBuddy name.

Initial System Events representation errors (`-1728` or type-conversion `-1700` while reading the initial input, window or safety metadata) may also use native resolution before any dispatch. The native interface must independently pass its strict type, owner and protected-input checks. Permission errors and explicit unsafe/disabled results do not switch paths. Later focus, clipboard or dispatch errors never switch paths or restart the original deadline.

An advertised optional attribute can still have no value. The System Events adapter normalizes `missing value` for subrole/enabled/protected metadata to the same absence defaults used by native AX. It does not coerce present strings or numbers into booleans, suppress read exceptions or accept a missing required role. This avoids treating a browser's empty optional subrole as a type-conversion failure.

Before initial native focus discovery, the helper reads the application's standard `AXRole` once and requires `AXApplication`. This allows on-demand accessibility providers to prepare their native interface. Preparation remains within the original deadline and foreground/receiver checks; a failed role read cannot enter the focus retry path. This is shared native behavior, with no website selector or new app-name branch.

If initial native focus discovery returns a transient AX `cannotComplete` error, the helper retries the read once after 25 ms. Generic retries do not set accessibility attributes or change the target app. They stay within the original deadline, check receiver liveness and foreground PID around the retry, and preserve any window captured before the error. A changed window, hard focus-read error or unreadable input stops the action. App-level and global focus errors are both retained as stage/code diagnostics. Subsequent focus checks and post-dispatch confirmation never initialize or switch the target.

Ordinary native keyboard paste constructs a complete two-event V pair before dispatch: key-down carries Command, and key-up carries no modifier flags. The final event no longer declares Command held. No extra Command-key release or global modifier reset is sent. The existing initial, clipboard-ready and pre-dispatch modifier checkpoints refuse the action if AppKit, Quartz combined-session or HID-system state reports Shift, Control, Option or Command. A blocked checkpoint records its phase and three four-bit masks from one sample. These state tables include different event sources; their values alone do not establish whether a physical key or a particular process caused a held state.

Readiness reports receiver connectivity and clipboard-observer preparation, not target-input readiness. The target is inspected only after an explicit action. Also, `executed` currently has different evidence across routes: ordinary System Events confirms command dispatch; native paths confirm insertion. The phone currently treats both as executed receipts. A receipt alone therefore does not prove insertion on the ordinary route. Planned convergence must distinguish not dispatched, dispatched but unconfirmed, and insertion confirmed, with clearing reserved for confirmed insertion; this protocol change is not implemented yet.

Native confirmation reads field text only in memory. It does not log field or clipboard bodies. Transient read failures can be retried within the original deadline, but the paste itself is never retried. Logs contain stage names, error codes, timestamps and process metadata; treat them as local diagnostic data.

## Check failures without duplicate input

First inspect the target: insertion may succeed while confirmation or result delivery fails. For a missing insertion, distinguish receiver connectivity, preparation, clipboard match, focus/permission checks, dispatch and readback. Do not disable guards to turn unknown results into success.

Use synthetic text and an ordinary empty input for acceptance. Check actual text, an executed receipt, unchanged-draft clearing, preservation of newer edits, mismatched clipboard rejection and cancellation on focus change. Then validate the intended app. Automated unit tests and Swift self-tests cover logic but not the complete phone/input-method/desktop chain.

## Local failure diagnostics

The production receiver enables `~/Library/Logs/DoubaoInputSync/paste-trace.jsonl`.
It records an opaque action UUID, timestamps, remaining budget, poll/queue/lock
and subprocess timings, fixed AppleScript phases, and control endpoint/status
codes. Idle successful polls are omitted. ACK start and success are separate from
the local paste result: logging an action result does not prove the server received it.

The native helper's `native-errors.log` adds the same action UUID as a fourth
column when called by this receiver; older three-column lines remain readable.
A legacy receiver already running must restart before Python network/queue tracing
is enabled. The per-action AppleScript still hot-loads phase logging immediately,
using a locally generated diagnostic UUID when the server action UUID is unavailable.
It shares that UUID with the native helper. Its fixed phases distinguish focus capture, clipboard matching, dispatch intent,
dispatch return and insertion verification. An intent may precede a refusal;
a dispatch return still does not prove insertion. Parent deadline termination can
also interrupt a call after dispatch, so `expired` alone never authorizes retry.

Each diagnostic family retains at most three files of 2 MiB each, with private
file permissions; native events are also capped per process. Logging failures
are ignored. The logs omit clipboard/field text, content fingerprints, URLs,
window titles, room names, credentials and raw subprocess errors. Keep runtime
logs out of Git and redact incident excerpts before sharing. Importing the Python
receiver in a fixture does not enable its log sink.

These diagnostics do not extend action deadlines, change the selected paste
route or retry uncertain paste commands. Pure subprocess tests cover timeout
before/after dispatch markers without operating the desktop. Real input-method
and target-app insertion still require their own acceptance.

Python receiver diagnostic writes use a bounded background queue. The action and poll threads
never wait for file writes; a full queue drops diagnostic records and reports a
bounded drop count after recovery. Producer timestamps include a monotonic clock
so scheduling and wall-clock adjustments can be distinguished from writing delay.
Queued records may be lost when the process exits; this is best-effort tracing,
not a durable execution receipt. Native-helper file logging remains separate.
The extra System Events frontmost query used only to compare diagnostic PIDs has
been removed. NSWorkspace remains the target source; all exact field/window,
clipboard, modifier and deadline guards remain.

A conservative budget still subtracts the entire poll duration, including server
waiting. These latency changes do not alter that protocol rule or the five-second
server lifetime. They cannot guarantee paste when the desktop is heavily loaded.
