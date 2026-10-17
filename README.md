# Doubao Input Sync

Use a phone as a text input surface, keep a copy archive, and optionally paste into the focused desktop input.

[中文说明](README.zh-CN.md) · [Security](SECURITY.md) · [Operations](docs/OPERATIONS.md)

This is an unofficial tool, unaffiliated with Doubao or WorkBuddy. The public repository is [ciaobellaagent/doubao-input-sync](https://github.com/ciaobellaagent/doubao-input-sync). This release is a source-only snapshot: private deployment history, runtime data and operational records are excluded. The original MIT license and attribution are retained in [LICENSE](LICENSE).

## What it does

| Phone mode | Text transfer | Desktop action |
| --- | --- | --- |
| Clipboard (default) | The phone copies text; your input method syncs the clipboard. A separate request saves the exact text to the relay's archive. | Copy only, or explicitly request one paste after the Mac clipboard matches. |
| Breathing | The relay receives drafts; an explicit send captures the final text. | A helper copies or pastes the captured text. |
| Flow | The relay receives drafts and captures text after a quiet window (2 seconds by default). | A helper copies or pastes the captured text. |

The desktop page shows delivery history and persistent copy archives. The relay uses Python's standard library; the web UI has no build step.

**Deployment boundary:** this is a personal, single-user service with **no built-in user authentication**. Rooms and pairing slots do not provide access control. Keep the default loopback listener, and provide an authenticated HTTPS gateway or a private VPN with restrictive access rules before connecting a phone. A random room name does not make an exposed relay safe. See [SECURITY.md](SECURITY.md).

## Start locally

Requirements: Python 3.10 or later. Node.js is needed only for JavaScript tests. The optional native Mac helper also needs Xcode Command Line Tools and macOS accessibility permissions.

```sh
git clone https://github.com/ciaobellaagent/doubao-input-sync.git
cd doubao-input-sync
python3 app/server.py --default-room demo
```

Open these on the same computer:

- Mobile UI preview: `http://127.0.0.1:18766/mobile/demo`
- Desktop history: `http://127.0.0.1:18766/pc/demo`

`demo` is an example room, not a credential. This loopback URL is not reachable from a phone. For phone access, configure your own protected HTTPS endpoint and its exact origin using `--public-origin`; instructions and checks are in [Operations](docs/OPERATIONS.md). Non-loopback binding requires an explicit `--allow-remote`.

## Retrieve saved text on the desktop

Use the same room on the phone and desktop, such as `/mobile/demo` and `/pc/demo`. After a phone copy is successfully archived, its text appears in the desktop page's copy-history list. The list shows the latest 50 records; older records remain in the database.

The desktop page reads archives on opening, without needing an automatic-paste receiver. It refreshes every 15 seconds while visible and when you return to the page. Use **Refresh archives** for an immediate read. A failed read offers a retry and preserves records already displayed.

In Clipboard mode, the main desktop box shows the latest saved copy. Breathing and Flow modes also show it until a sent record exists; then the main box shows the latest sent text, while the copy-history list remains available below. The page identifies the room and record time to help confirm pairing.

Use **Copy again** on a record to put its text on this computer's clipboard, then paste it where needed. This does not create another archive or request automatic paste. See [clipboard and archive details](docs/CLIPBOARD_ARCHIVE.md#desktop-history).

## Phone clipboard → Mac paste

1. Set up protected phone access and confirm that the phone's copy button triggers your input method's cross-device clipboard sync.
2. Build the local native helper:

   ```sh
   python3 scripts/install_workbuddy_native.py
   ```

3. Start the receiver from a terminal host with the required macOS permissions. For a relay on this Mac:

   ```sh
   python3 scripts/mac_prearmed_receiver.py --server-url http://127.0.0.1:18766 --room demo
   ```

4. Focus an ordinary editable input on the Mac. On the phone, use Clipboard mode and tap **Copy and paste**.

The button copies immediately and can wait up to 15 seconds for preparation. Editing the draft or leaving the page cancels an unsent action. The receiver requires an exact SHA-256 clipboard match, checks focus and timing, and triggers at most one paste. Preparation alone cannot paste. There is no automatic fallback that fetches the archived body.

An executed receipt clears only the unchanged submitted draft and saves local recovery. Failed or unknown results preserve it. **Receipts have different strengths:** the native path verifies inserted text; the ordinary System Events path only confirms that the shortcut was dispatched. If the phone reports failure after text appeared, inspect the target before retrying to avoid duplicates.

Ordinary apps use system focus and standard Command-V without per-app or website configuration. The native fallback reads the standard application role before resolving the actual focused input, allowing on-demand accessibility providers to prepare their interface. Its V key-up event carries no modifier flags; all three modifier checkpoints check AppKit, session and hardware state. Validate each intended application: these generic mechanisms do not establish compatibility with every editor.

**WorkBuddy compatibility exception:** WorkBuddy uses its native Edit → Paste menu (`paste:` through accessibility). Other supported apps use simulated Command-V. This is an explicit app-specific route, not proof that keyboard paste cannot work in WorkBuddy. See [Mac receiver details](docs/PREARMED_RECEIVER.md).

## Other desktop options

The Mac receiver above also handles relay sends. Do not run a second paste helper alongside it. Legacy Mac relay helpers and Windows clipboard/paste helpers remain available; Windows does not implement the prepared Mac clipboard-match flow. Start with clipboard-only delivery and validate each target application. See [Windows helper](docs/WINDOWS_HELPER_PLAN.md) and [Operations](docs/OPERATIONS.md).

## Data and limits

- Copy archives store exact text in a local SQLite database outside the checkout. They persist across restarts; the UI shows the latest 50 items, not a retention limit.
- Clipboard drafts, retry records and recovery text also live in browser storage. Relay drafts and relay delivery history are in memory.
- Clipboard copy and archive success do not by themselves prove desktop insertion.
- macOS automation depends on permissions, focus and the target's accessibility implementation. Secure fields are rejected by the guarded prepared path. That guarantee does not extend to every legacy helper.
- Phone/browser/input-method compatibility and real desktop paste behavior require device testing. Unit tests do not establish universal compatibility.

See [clipboard and archive behavior](docs/CLIPBOARD_ARCHIVE.md) for clearing, recovery and storage details.

## Development

```sh
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_client_clipboard.js tests/test_signal_trial_client.js
node --check app/static/client.js
```

Use disposable rooms and databases for integration tests. Review the [release checklist](docs/RELEASE_CHECKLIST.md) before publishing code or artifacts. Contributions should use synthetic text and redact diagnostics; do not attach real clipboard contents, databases or credentials.

## License

[MIT](LICENSE).
