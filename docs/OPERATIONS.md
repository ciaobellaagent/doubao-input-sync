# Running your own instance

## Local server

```sh
python3 app/server.py --host 127.0.0.1 --port 18766 --default-room demo
```

The server stores copy archives outside the repository by default. Choose a separate absolute path with `--archive-db` for test instances. Do not use a production database or room for smoke tests. Settings, relay drafts and delivery history are transient; copy archives persist. After a restart, the room defaults to Clipboard mode.

## Phone access

A phone needs an address it can reach and a secure browser context for clipboard writes. Configure either:

- An authenticated HTTPS reverse proxy, with the backend reachable only by that proxy.
- Private VPN access limited by device/user ACL, with HTTPS for the phone UI.

All pages and APIs need the same access policy. Verify denial from an unauthorized client as well as success from the phone and desktop receiver. Avoid unauthenticated tunnels. The relay has no user authentication or room-level authorization.

For an HTTPS gateway under a subpath, a backend command can be:

```sh
python3 app/server.py --host 127.0.0.1 --port 18766 \
  --base-path /doubao --public-origin https://input.example.com --default-room demo
```

`input.example.com` is a placeholder: replace it with your own configured endpoint. The origin includes scheme and authority, not `/doubao`. The corresponding URLs are `/doubao/mobile/demo` and `/doubao/pc/demo`. The proxy must preserve the backend's expected subpath and use an accepted Host. Forwarded headers are ignored for this decision. Repeat `--public-origin` for additional permitted origins; each browser or CLI connection must also send an accepted Host, including connections through a separate private route.

Keep the listener on loopback when the proxy is on the same host. If a separate proxy or private VPN requires a non-loopback listener, bind the specific intended interface and add `--allow-remote`; apply firewall/ACL restrictions first. That flag acknowledges exposure; it does not secure it. `--public-origin` configures browser origin checks, not authentication. See [SECURITY.md](../SECURITY.md).

[The Caddy example](../deploy/Caddyfile.example) provides browser HTTP Basic authentication over HTTPS at the root path. It needs your own hostname and password hash. Match its origin to `--public-origin`; add the same base path to the server and page URLs if using subpath deployment.

CLI receivers need their own permitted connection. The prepared Mac receiver has no HTTP Basic, browser-cookie or Cloudflare service-token option for its control requests. It cannot directly use the authenticated Caddy URL as shipped. When relay and receiver share a host, the receiver can use loopback while the phone uses the HTTPS gateway. Otherwise provide a private device-authorized route with restrictive network rules and accepted Host configuration. Legacy Mac/Windows relay helpers support Cloudflare service-token headers over HTTPS, but that does not add authentication to the prepared receiver. Do not bypass authentication by opening the backend to the Internet.

## Desktop lifecycle

Start the [Mac prepared receiver](PREARMED_RECEIVER.md) in the foreground and validate permissions plus real paste behavior before configuring persistence. It also handles explicit relay sends. Do not run the legacy helper at the same time.

Legacy relay helper scripts include LaunchAgent install/status/uninstall commands and tmux wrappers. They operate the legacy `mac_paste_helper.py`, not the prepared receiver. Inspect the generated configuration before use; installing a background job does not establish GUI permissions. Test login recovery and shutdown separately. No external control-plane service is required or managed by this repository.

## Upgrades and rollback

1. Keep a private backup of configuration and copy archives. For a live database use SQLite's backup API. Record the deployed public version locally without copying credentials into release notes.
2. Test the candidate in a disposable room and database. Review data migrations and helper/API compatibility.
3. Stop affected consumers before switching versions; ensure only one receiver starts. Relay restarts lose in-memory drafts and delivery history, so finish or preserve outstanding work first.
4. Verify the protected route, static UI version, archive read/write persistence and actual target insertion. Health checks alone do not prove end-to-end success.
5. If rollback is needed, restore compatible code/configuration while retaining user archives. Do not silently delete or replace the database.

## Diagnostics

Investigate server connectivity, phone copy, archive storage and desktop insertion separately. Copy/archive success is independent of paste success. A failure receipt may mean insertion was not confirmed; check the target before resending.

The 5-second dispatch deadline is independent of receipt collection: delivered actions accept an ACK until 10 seconds after that deadline and are never delivered again. Closed sessions permit only ACK with the original token and explicit action-ID status reads, not control operations. Each room retains at most four previous sessions for up to 120 seconds; pending receipts cannot be evicted by session or room capacity pressure. Other old receipts can be evicted sooner. All receipts remain transient across relay restarts. The receiver retries a transport-failed ACK once, never the paste, and an `action_already_finished` conflict no longer forces reconnection.

Native `verify_observation` codes contain no input text: 0 means no read, 1 unchanged value, 2 changed but unexpected value, 3 read error, and 4 exact match (check `paste_verified` for timely confirmation). Verification stops 200 ms before the original deadline to allow its result to return. `paste_unconfirmed` is emitted only after command dispatch; it preserves the phone draft. Late results after the phone observation timeout update the message without clearing text.

Keep logs private and bounded. Share redacted stage/error codes and synthetic reproduction steps instead of full clipboard data, database snapshots or gateway responses. Native Mac logs can contain process IDs and timestamps even when they contain no text. Follow the [release checklist](RELEASE_CHECKLIST.md) before sharing a diagnostic bundle.
