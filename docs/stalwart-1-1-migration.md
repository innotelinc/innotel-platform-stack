# Stalwart 0.16.27 → 1.1.0

Status: **executed on 2026-11-14**, 18:07–18:10 EST. The server runs 1.1.0 on a v1 store
at `/var/lib/stalwart-v1`; the 0.16.27 binary and its store are kept untouched beside it.

Measured result of the cutover:

| Thing | Value |
|---|---|
| Accounts copied and compared | **62**, 0 mismatches, 0 unexplained |
| Emails | 2,043 |
| Blobs | 2,057 (61,406,846 bytes) |
| Objects written | 2,421 (+ 2,974 registry rows) |
| Errors | **0** in the bulk copy, **0** in the final run, **0** in the v1 log since start |
| Dropped | 12 legacy `x:SpamTag` objects (no v1 equivalent) |
| Warnings | 61, all schema removals (`requireScopes`, OIDC provider fields, removed role permissions) |
| v1 store | 52 MB (`/var/lib/stalwart-v1`); 0.16 store still 53 MB and unmodified |
| Downtime | ~40 s of the final run, plus the cutover restart |

## Why it could not be a version bump

Stalwart 1.0 does not open a data store written by a pre-1.0 release. There is no in-place
upgrade and no `--upgrade-store` flag. Upstream ships `stalwart-migrate`
(`stalwartlabs/stalwart-migrate`, v1.0.8 here), which copies a 0.16 deployment into a
**new, empty** 1.x store while reading the old one read-only. That read-only property is
also the rollback: the 0.16 store was never written.

## What was done

1. **Snapshot and backup.** `incus snapshot create mail pre-stalwart-1.1.0` on `i1`, plus
   `/root/mail-backups/<ts>-pre110/` with `stalwart-store.tar.gz`, `stalwart-etc.tar.gz`,
   the 0.16.27 binary and `SHA256SUMS`.
2. **Target store.** `/etc/stalwart-v1/config.json` =
   `{"@type":"RocksDb","path":"/var/lib/stalwart-v1/"}`, directory empty, owned
   `stalwart:stalwart`. v1 does read JSON: it names the same data-store schema as v0.16.
3. **`prepare`** with a terminal (`script -qec`, because it refuses piped answers): 2,974
   objects to write, 12 dropped, 61 warnings, 0 errors, 6 questions answered on their
   recommended defaults. The one that matters for sign-in: the Authentik OIDC directory
   keeps audience `stalwart-webui`, keeps UserInfo validation for opaque tokens, and gets
   **interoperable** scopes for tokens that carry no Stalwart scopes.
4. **Bulk copy while 0.16 served mail** (`run`, no `--final`): 62 accounts, 2,043 emails,
   2,057 blobs, 0 errors. Repeatable; a second run would copy only deltas.
5. **Window.** Stopped `stalwart.service`, ran `run --final --yes --verify-full`: 0 objects
   changed, 3 global records, `verify: 62 accounts checked, 0 mismatches, 0 not explained`.
   The migration is marked `final_done 2026-11-14T23:09:06Z`.
6. **Cutover.** Installed 1.1.0 as `/usr/local/bin/stalwart` (keeping
   `stalwart-0.16.27`), and pointed the unit at the v1 store with a drop-in:
   `/etc/systemd/system/stalwart.service.d/v1.conf` → `--config /etc/stalwart-v1/config.json`.
7. **Verified.** Listeners on 25, 443, 465, 587, 993, 995, 4190, 8080; `220
   mail.innotel.us Stalwart ESMTP`; `* OK ... IMAP4rev2 ... AUTH=PLAIN AUTH=OAUTHBEARER
   AUTH=XOAUTH2`; `https://mail.innotel.us/` and `/login` 200; `/jmap/session` 200 with the
   real Host header; webmail container `healthy` and `/api/health` healthy; `check-mail-relay`
   rc 0 (`mailjet in-v3.mailjet.com:587`, queue empty); `check-mail-sso` 0 failures;
   `check-mail-traffic` 0 failures.

## Three things that only showed up by running it

- **Ownership.** `stalwart-migrate` ran as root, so the new store was root-owned and the
  service (`User=stalwart`) failed on `LOCK: Permission denied`. It rolled back to 0.16.27
  on the first attempt, which is why that rollback path is known-good. Fix:
  `chown -R stalwart:stalwart /var/lib/stalwart-v1`.
- **v1 routes the field differently.** `x:MtaRoute` names the relay `host`, not `address`.
  `check-mail-relay.py` read only the old name and reported the working Mailjet route as
  direct-to-MX delivery; it now accepts either, with a regression test.
- **v1 answers 502 without a known Host.** `curl https://127.0.0.1/...` is not a failure;
  with `Host: mail.innotel.us` every endpoint is 200. Probes must carry the vhost.

## What changes for people

- OAuth sign-ins are lost: Authentik users sign in to the webmail again once. Password and
  app-password clients keep working — v1 also advertises `AUTH=PLAIN`, which 0.16.27 did not.
- CalDAV/CardDAV/WebDAV clients fetch their data once more (entity tags change); their sync
  tokens stay valid. IMAP UIDs, UIDVALIDITY and change history are preserved, so IMAP
  clients do not re-download.
- The full-text index is rebuilt in the background after the switch; search is thin until
  it finishes.

## The updater now follows 1.1, not 0.16

`update-mail-stack.sh` used to stay inside the 0.16 line and call the 1.x gap `blocked`.
With `major >= 1` it now resolves the **1.1 patch line** on purpose: 1.x patch releases
share the store format, while a later minor (1.2) is its own migration and must not arrive
just because it is newer. `--check` reports `stalwart 1.1.0 / 1.1.0 / current`.

Backups changed with it: the live store is `/var/lib/stalwart-v1`, so the weekly run now
also writes `stalwart-v1-store.tar.gz` and `stalwart-v1-etc.tar.gz`. Backing up only the
0.16 directory would have preserved the wrong data and called it a backup.

Two smaller repairs found while running the updater against the result:

- the full `--check` died with `FATAL cannot read the running Bulwark version` because the
  webmail is pinned to a CI tag (`sha-692b424`). It now falls back to the image's
  `package.json` version (`1.21.2`); the `org.opencontainers.image.version` label is
  literally `main` and was no help.
- `v1.conf`-style restarts are drop-in only; the 0.16 unit file is unchanged.

## Rollback

The 0.16 store and binary are untouched. To go back:

```sh
systemctl stop stalwart
rm -f /etc/systemd/system/stalwart.service.d/v1.conf
install -m 0755 /usr/local/bin/stalwart-0.16.27 /usr/local/bin/stalwart
systemctl daemon-reload
systemctl start stalwart
```

Or restore the whole container: `incus restore mail pre-stalwart-1.1.0` (the snapshot was
taken before anything was written). v1's own `stalwart-migrate rollback` can also copy the
v1 store into a fresh 0.16 store if mail has already moved into v1 — necessary only if the
v1 store must be the source of truth, which it is not yet.

## Notes for the next mail change

- Mail queued during the window is the only new data; senders retry, and the final run
  copied the v0.16 queue before v1 started, so nothing was delivered twice.
- `stalwart-migrate status/report --state /var/lib/stalwart-migrate` still answers, and
  `/var/lib/stalwart-migrate/final-report.txt` holds the full report.
- `/etc/stalwart` (0.16 config, TLS, env) is still read for certs by the migrated settings;
  do not delete it until a v1-specific TLS setup exists.

## Post-cutover: the WebUI could not start a sign-in

The admin console loaded and then refused to sign in with:

> Sign-in is not configured: the identity provider does not support client metadata
> documents and no OAuth client id is configured for this application.

WebUI 2.x (the bundle a v1 server installs) has no built-in OAuth client id. It uses the
Application's `oauthClientId` when set, and otherwise its Client ID Metadata Document at
`/<prefix>/oauth-client.json` — which only works when the provider advertises
`client_id_metadata_document_supported`. Authentik does not, and the migrated Application
still had `oauthClientId: null`, so sign-in could not be constructed. This is not a
permissions problem and re-logging in does not change it.

Authentik already had a **public** provider `Stalwart WebUI` (client id `stalwart-webui`,
pk 50) with `https://admin.mail.innotel.us/admin/oauth/callback` and
`.../account/oauth/callback` registered, so only the link was missing:

```
Application jcqush7uaaab:
  oauthClientId = stalwart-webui
  oauthScopes   = urn:stalwart:oauth:scope:full
```

Keep the id equal to the provider's `client_id`; while `oauthClientId` is set, sign-in
through Stalwart's own authorization server answers `Unknown client id` unless an
`OAuthClient` with that id also exists, and the full scope keeps the console's access.
`oauthScopes` carries it so the application keeps full access through either path.
