# Attachment trial

Use the existing `/mobile/demo` and `/pc/demo` pages and the same protected gateway as text. Expand **Images and files** on the phone, select a photo/file, and explicitly upload it. A browser that exposes clipboard files also supports pasting/dropping one into the editor. Ordinary text paste remains text paste.

An upload stores exact bytes plus a sanitized filename, size, detected media type, SHA-256 and timestamp. It does not authorize clipboard access or paste. Downloads use room-scoped UUIDs, `Content-Disposition: attachment`, `nosniff` and `no-store`; HTML/SVG and unknown types are ordinary binary downloads. The server does not execute or decode uploaded files. A room is not an access-control boundary: protect all attachment routes through the same authenticated gateway/private device route as the rest of the application.

## Transfer and paste

Selecting a saved upload prepares that file on the connected Mac. The optional attachment worker belongs to the existing prepared receiver and uses its current signal-session credentials. It downloads to a private cache, verifies size and SHA-256, and optionally converts an image to PNG through ImageIO. Preparation never touches the clipboard or foreground input.

The user must click **Paste attachment to computer**. The phone can wait at most 15 seconds for the matching preparation, consumes that intent before its one HTTP action request, and never reconstructs an intent from archive history. Changing selection, editing text or leaving the page cancels an unsent intent. A readiness timeout requires another explicit click. A failed preparation is shown immediately and requires an explicit retry.

The receiver consumes a delivered action once. Its authorization lasts at most five seconds; it shares the text/relay action lock and does not extend the deadline while waiting. Under the existing native permission, stable focus, ordinary editable-role and modifier checks, the helper stages the original file URL and, when prepared, a PNG representation in one clipboard item. It rechecks clipboard/focus and dispatches one standard paste. WorkBuddy keeps its existing menu route; other apps use the existing key-event plan. No Enter, application launch, file execution or uncertain paste retry occurs.

The destination must support the file or image type. Native `paste_dispatched` becomes an attachment `executed` receipt, whose UI explicitly confirms dispatch only. It does not prove that a chat accepted an attachment. Receipts have a separate 15-second window after delivery and are not paste authorization. The original token can acknowledge its original delivered action after reconnect; it cannot use a new session to replay it. Phone text drafts remain untouched, including on success. Failed/uncertain results retain the server archive; inspect the destination before trying again.

## Storage and lifecycle

Server storage lives beside the configured text-archive database in `attachments/`: a private SQLite database and randomly named binary blobs. New directories/files are private (0700/0600). Limits are 20 MiB per file, 512 MiB total and 512 saved files. Quota rejection preserves existing archives. Deletion is explicit and rejects active transfers/actions. The UI displays the latest 50 files; other saved files still count toward storage.

The Mac cache is `~/Library/Application Support/DoubaoInputSync/attachments/<UUID>/<sanitized filename>`. The optional `.clipboard.png` is in the same private UUID directory. Native validation rejects symlinks, hardlinks, path escapes and unsafe ownership/permissions. Image conversion checks dimensions before decoding (at most 25 million pixels and 16,384 pixels per side) and bounds PNG output to 50 MiB. Unsupported/oversized images remain file URLs. The Mac admits at most 512 MiB of cache data before each download, plus at most one newly converted PNG. A full cache rejects preparation rather than deleting files a target application may still need.

Deleting the server archive does not clear the Mac cache, existing clipboard data or downloaded copies. Stop the receiver and finish using pasted attachments before manually removing its cache. These files are not encrypted by this application. Keep them and backups outside the public repository.

Actions/preparations are in memory and never replay after a relay restart. Action UUID tombstones are bounded to 4096 per relay process; capacity rejection requires a controlled restart after outstanding work completes. The attachment worker renews only its own readiness observation; the original text receiver still maintains its signal lease. Failed optional attachment transport does not terminate the text receiver.

## Upgrade and checks

Update server sources/static assets and receiver sources together, run `python3 scripts/install_workbuddy_native.py`, and restart the prepared receiver. Do not run a second independent desktop consumer. A server restart still loses transient text relay drafts/history, so preserve or finish them first; durable text archives and attachments stay on disk. Refresh the existing phone and PC pages for the attachment UI.

Run Python tests, the three JavaScript suites, native `--self-test`, source-release checks, and an isolated server smoke test. Pure tests/download tests do not establish target-app file acceptance. A real foreground paste test needs a coordinated idle window; do not steal focus from ongoing user work. Windows can download the archive but has no attachment automation in this trial.
