# Public release and security review checklist

Use this checklist for the exact candidate commit and its complete published surface. Record results privately with file/line evidence, test output and unresolved limits. A clean text search is one check, not a security certification.

## Automated checks

Stage the intended public files first, then run:

```sh
python3 scripts/check_public_release.py --history
python3 -m unittest discover -s tests -p 'test_*.py'
node --test tests/test_client_clipboard.js tests/test_signal_trial_client.js
```

The release checker scans **staged contents** and every commit reachable from local refs, including commit metadata. Re-stage edited files before re-running it, and make sure all refs intended for publication are present locally. It does not inspect untracked/unstaged files, remote-only refs, GitHub issues or downloadable artifacts. Use `--denylist-file` with a private file outside the repository to catch deployment-specific identifiers. A passing result still requires manual review and an independent credential scan; the patterns cannot recognize every secret.

## Repository and history

- [ ] Inventory every tracked path, hidden file, symlink, submodule, Git LFS object and binary. Exclude runtime databases, backups, user text, screenshots, archives, private notes and generated native binaries.
- [ ] Scan source, examples, tests and documentation for secrets and private infrastructure: tokens, keys, credential-store identifiers, real endpoints/IPs, personal paths, SSH commands, account identifiers and copied logs. Manually classify matches, including encoded or split values.
- [ ] Inspect every branch, tag and reachable commit, including author/committer emails and commit messages. Removing a file at HEAD does not remove history.
- [ ] If releasing a fresh source-only snapshot, create it from an allowlist and confirm no parent/private refs or objects were imported. Preserve required license notices and attribution.
- [ ] Review repository description, homepage, branch names, tags, issues, PRs, comments, wikis, Pages, releases/assets, Actions logs/artifacts/caches and other downloadable content before changing visibility. A clean Git tree does not sanitize these surfaces.
- [ ] Rotate exposed credentials and record affected copies if any are found; deletion/history rewriting alone is insufficient.

## Defaults and deployment

- [ ] Confirm a fresh checkout binds only to loopback on the documented generic port and makes no connection to a maintainer endpoint.
- [ ] Verify non-loopback binding requires explicit opt-in. Examples must not expose an unauthenticated tunnel or publish broad firewall rules.
- [ ] Check there is no bundled credential, hard-coded account lookup, private host mapping or dependency on the maintainer's local control plane.
- [ ] Test unauthorized requests at the actual gateway and confirm direct backend access cannot bypass it. Protect reads, writes, streams and static routes.
- [ ] Verify accepted Host and configured public-origin behavior; reject hostile browser origins, mismatched hosts and unsafe state-changing requests. Include proxied/subpath and no-Origin CLI cases in regression tests.
- [ ] State the limitations clearly: no built-in identity/room authorization, no multi-tenant isolation, no authentication provided by room names or fingerprints.

## Storage, privacy and local automation

- [ ] Check data locations, permissions, backup handling, browser persistence and retention. Test durable archive failure without false success and retry without duplicate records.
- [ ] Ensure diagnostics do not record clipboard/field bodies, credentials or raw gateway responses. Review metadata, dry-run output, error paths and command-line arguments too.
- [ ] Verify clipboard/focus/deadline checks, secure-field rejection where supported, single-consumer locking and cleanup. Test permission denial without bypasses.
- [ ] Confirm app-specific compatibility paths are documented, do not send Enter, and do not retry a paste whose dispatch outcome is uncertain.
- [ ] Review installers and startup scripts for shell/XML injection, restrictive configuration permissions, explicit targets and rollback. They must not stop unrelated private services.

## Regression and publication

- [ ] Run Python and JavaScript tests, syntax checks, native pure self-tests where available, and archive smoke tests with synthetic text and temporary data.
- [ ] Perform real device acceptance for any claimed platform/app: actual text insertion once, receipt meaning, draft clearing only when eligible, preservation of newer edits, offline/timeout handling and reconnect.
- [ ] Do not label mocked or generated tests as real phone/OS acceptance. Separate dispatch success from insertion verification in docs and UI.
- [ ] Re-scan the final export, its fresh Git history and packaged artifacts after all edits. Review the exact diff and verify no unresolved private-data findings remain.
- [ ] Confirm the license, public documentation links, reporting channel and GitHub settings match the intended release. Verify the published tree/downloads after publication and record remaining risks honestly.
