# Clipboard mode and copy archives

Clipboard mode is the default. The copy button begins a browser clipboard write inside the user gesture, before waiting for network access. Once copying succeeds, the page archives the same immutable text snapshot. Your input method handles cross-device clipboard sync; test that it recognizes copies from the browser button on your devices.

## Copy only

Copy-only archival never enters the relay delivery queue. Intermediate clipboard-mode drafts remain in the browser; a preparation may send a SHA-256 fingerprint, but it does not authorize insertion. If copying fails, the draft remains and no copy record is created.

An offline archive is queued in this browser and retried on reconnection. Each click has a unique `copy_id`; retrying the same ID and text returns the existing record, while reusing an ID for different text is rejected. An archive retry never requests another desktop paste.

The copy-only clear-after-archive option is separate from copy-and-paste receipts. A late response cannot clear newly edited text.

## Copy and paste

With nonempty text, the user can click immediately. Copying starts at once; the current page then waits up to 15 seconds for matching receiver preparation. It submits one action when ready. Editing, clearing, leaving the page or changing a bound receiver cancels an unsent action. Refreshing does not restore queued paste actions. A suspended browser checks the absolute deadline on resuming.

After dispatch, the page observes the result separately for up to 15 seconds. Thus the preparation wait is not a bound on the complete phone-to-desktop experience. Failures, expiry and unknown outcomes retain the draft. Only an executed receipt can clear the unchanged submitted draft; a local recovery copy is saved first. Newer edits are preserved. No automatic server-body fallback is performed.

The native receiver route verifies inserted text. The ordinary System Events route confirms only key dispatch. A missing or failed confirmation does not prove that no insertion occurred. Check the target before retrying or sending the same text through Breathing mode.

## Desktop history

Open `/pc/<room>` with the same room as the phone's `/mobile/<room>`. Copy history loads independently of relay state, receiver readiness and streaming support. The page starts the archive request before opening the optional event stream. If stream construction is blocked or unsupported, relay state falls back to polling and archive reads continue.

The desktop copy list is available in every input mode. The main box shows the latest copy in Clipboard mode, and also in Breathing or Flow mode before any sent archive exists. Once a sent archive exists in a sending mode, the main box and its copy button use that record; the saved-copy list remains available below. The room, record time and copy-list count make the displayed source explicit.

While visible, the page refreshes copy history every 15 seconds; returning to the page or clicking the archive refresh button also requests it. A failed read does not erase previously displayed records. **Copy again** writes only to the desktop clipboard, without creating another copy archive or requesting a paste.

For subpath deployments, the rendered HTML carries the base path in a data attribute. The client also recognizes the `/mobile/<room>` or `/pc/<room>` route in older HTML. Archive requests therefore do not require an inline configuration script, which an HTTPS gateway's content security policy may block. HTML references a versioned client asset so a page refresh requests the updated code.

## Storage

`POST /api/copy-archive` stores `{room_id, copy_id, text, copied_at}` in SQLite and returns success only after commit. `GET /api/copy-history?room_id=…` reads copy records. Copy archives are separate from transient relay delivery history.

Choose the database with `--archive-db` or `DOUBAO_ARCHIVE_DB`. Defaults are outside the repository:

- macOS: `~/Library/Application Support/DoubaoInputSync/archives.sqlite3`
- Linux: `${XDG_STATE_HOME:-~/.local/state}/doubao-input-sync/archives.sqlite3`
- Windows: `%LOCALAPPDATA%/DoubaoInputSync/archives.sqlite3` (with a user-local AppData fallback)

The page shows the latest 50 records; older records remain stored. There is no automatic deletion policy. Browser local storage also retains drafts, pending archives and last-cleared recovery text. Clearing browser data may lose pending records; clearing the editor does not delete server archives.

Restrict access to the database and backups. Existing database and directory permissions are not retroactively tightened by the server; review them after upgrades or restores. For online SQLite backups, use `sqlite3.Connection.backup()`; copying only the live database file can miss journaled data. Before deletion, stop writers and account for browser retry queues, backups and SQLite sidecar files. Never commit real archives or test records containing personal text.

## Device acceptance

Use synthetic text in a disposable room. Verify copy-only archival, one offline retry without duplicates, and archive survival after a controlled server restart. For automatic paste, confirm actual insertion exactly once and the phone's result; then test changed drafts and an unavailable receiver. These checks supplement automated tests and must be repeated for the intended browser, input method and target application.
