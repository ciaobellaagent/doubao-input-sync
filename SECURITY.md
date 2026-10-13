# Security policy and deployment boundary

Doubao Input Sync handles clipboard text and can trigger desktop input. It is intended for one trusted user and their devices. It has not undergone a comprehensive independent security audit.

## Network access is a trust boundary

The relay has **no built-in account authentication or per-room authorization**. Anyone who can reach its APIs may read or alter room data, read copy archives, or attempt actions. Room names, random pairing codes, device slots, fingerprints and receiver protocol tokens are not a replacement for user/device authentication.

The default listener is `127.0.0.1:18766`. Non-loopback binding requires `--allow-remote`. Use an authenticated HTTPS reverse proxy, or a private VPN restricted by ACL to the user's devices. Phone clipboard APIs still need a secure browser context. Protect every route, including static pages, API reads, writes and streaming connections. Block direct access to the backend so it cannot bypass the gateway. Do not expose it through an unauthenticated public tunnel.

Configure `--public-origin` for the exact browser-facing origin. Host and cross-origin request checks reduce browser-based attacks against the local service; they do **not** authenticate clients or protect against malicious software already able to reach the backend. Local users and local processes remain part of the trust boundary. This is not a public multi-tenant service or a hardened Internet-facing HTTP server.

## Data and credentials

- Copy archives contain exact user text and remain on disk until explicitly removed. Relay drafts/history are in memory; browser drafts, retry records and recovery text persist locally.
- New archive files are created with restrictive permissions (0600 on POSIX systems), but existing database and directory permissions are not retroactively changed. Check them when upgrading or restoring a backup.
- These stores are not encrypted by the application. Use OS account isolation, restricted file permissions, disk encryption and protected backups as appropriate for the text you handle.
- SHA-256 clipboard fingerprints reveal equality and may allow guesses of short or predictable text. Treat them as sensitive metadata, not encryption.
- Keep gateway tokens, environment files, SSH keys, database files, private endpoints and real test output outside Git. `.gitignore` helps prevent accidents; it does not remove already-tracked files or history.
- Review logs before sharing: even text-free diagnostics can include process IDs, timestamps, room IDs and endpoint metadata. Proxy logs and third-party clipboard synchronization have their own retention policies.
- A browser login is not automatically available to a command-line receiver. Configure each client’s permitted route and credentials separately, without disabling the gateway.

## Desktop automation

macOS helpers need accessibility/automation permissions for their actual runtime host. The native helper checks existing trust; installation does not grant permission. Do not grant broad permissions to an unreviewed binary.

The prepared Mac path checks clipboard equality, deadline, focus and supported editable roles before one action. It does not send Enter. WorkBuddy uses a native paste menu; other paths use keyboard events. The native path reads field text in memory to verify insertion. The ordinary System Events route only confirms dispatch; legacy relay and Windows helpers provide weaker focus/receipt guarantees. A failed confirmation can follow a real insertion: inspect the target before manually retrying.

Run only one desktop consumer per destination. Do not enable Flow mode or automatic paste around sensitive inputs until its behavior has been checked on that device. The repository's scripts do not replace operating-system protections.

## Reporting a vulnerability

Do not put secrets, exploit traces containing personal text, or deployment details into a public issue. If the repository's Security tab offers **Report a vulnerability**, use its private reporting flow. Otherwise, open a minimal issue requesting a private contact channel without disclosure details. Private reporting availability must be checked in GitHub; this document does not imply it is enabled.

For accidental exposure, first restrict access and rotate/revoke affected credentials. Deleting a file or rewriting Git history cannot retract copies already downloaded, forks, caches or earlier public artifacts.

Only the current public source is maintained; there is no supported-version security SLA. Release review steps are in [the checklist](docs/RELEASE_CHECKLIST.md).
