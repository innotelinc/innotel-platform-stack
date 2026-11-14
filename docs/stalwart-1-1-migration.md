# Moving Stalwart off 0.16.27

Status: **planned, not started.** The server runs 0.16.27 and the updater is deliberately
held there. This is the document the hold points at.

## Why it cannot be a version bump

Stalwart 1.0 does not open a data store written by a pre-1.0 release. Started against one
it stops with:

```
The data store was created by a Stalwart version prior to 1.0 (schema version <N>)
and cannot be opened by this version.
```

There is no in-place upgrade and no `--upgrade-store` flag. Upstream ships a separate
tool, `stalwart-migrate`, which copies a whole 0.16 deployment into a **new, empty** 1.x
store. The copy reads the old store read-only, so 0.16 stays runnable throughout and
afterwards — which is also the rollback.

`update-mail-stack.sh` refuses to cross this boundary: without `--allow-stalwart-major`
it only ever offers 0.16.x tags, and it now reports the 1.x release by name instead of
calling the server "current".

## What the estate actually has

| Thing | Value | Where it comes from |
|---|---|---|
| Stalwart | 0.16.27, native binary | `/usr/local/bin/stalwart --config /etc/stalwart/config.json` |
| Newest releases | 1.1.0 (latest), 1.0.3, 1.0.2, 1.0.1, 1.0.0 | GitHub releases, `stalwartlabs/stalwart` |
| Data store | 54 MB | `du -sh /var/lib/stalwart` |
| Free space in `mail` | 430 GB of 440 GB | `df -h /` |
| Container | `mail`, 1024 MiB memory cap, on `i1` | `incus config show mail` |
| Config | `/etc/stalwart/config.json`, plus `/etc/stalwart-oidc/` | |
| OIDC | Authentik directory through a local discovery cache | `/etc/stalwart-oidc/hosts` bound over `/etc/hosts` for Stalwart only |
| Client | Loomix Mail 1.21.2 (`bulwark`, image pinned `:1.21.2-loomix`) | `/opt/bulwark/docker-compose.yml` |
| Backups | `/root/mail-backups/<ts>/stalwart-store.tar.gz`, `stalwart-etc.tar.gz`, `stalwart-binary` | written by `update-mail-stack.sh` before any change |

Two consequences of the numbers: the "second copy of the store" that upstream requires is
**54 MB**, not a capacity problem, and the migration tool can run inside the container as
long as it is given a terminal for `prepare` (it refuses piped answers).

## What changes for people, not just files

- IMAP UIDs, account and message identifiers and change history are preserved, so mail
  clients do not re-download.
- OAuth sign-ins are lost: everyone using Authentik sign-in to the webmail signs in again
  once. Password/app-password clients keep working.
- The full-text index is rebuilt in the background after the switch, so search is thin
  for a while.
- The webmail depends on Stalwart's session and capabilities. Loomix 1.21.2 predates
  1.0.2, and upstream already has a branch for "public keys and encryption at rest on
  Stalwart 1.0.2+" — budget for a webmail release alongside the server move, and do not
  pin the server to 1.1.0 without checking that fix is in the image.

## Procedure (agreed shape, to be rehearsed)

The tool needs a terminal for `prepare`; the final copy needs 0.16 **stopped**. Plan for a
short maintenance window, not a long one: the bulk copy can run while 0.16 serves mail.

1. **Back up by hand first**, outside the script's rotation:
   `update-mail-stack.sh --check` (expect `blocked 1.1.0`), then take
   `/root/mail-backups/<ts>/` and copy it off the host.
2. **Get `stalwart-migrate`** onto the container (`stalwartlabs/stalwart-migrate:<ver>`,
   or the release binary). It converts settings and copies data; it does not need the
   server stopped except for the final run.
3. **`prepare`** with a terminal attached:
   `stalwart-migrate prepare --source /etc/stalwart/config.json --state /var/lib/stalwart-migrate`
   Answer the conversion questions (blob store reuse, data categories). Read
   `prepare-report.txt` in the state directory: every dropped object and changed setting
   is listed there, and this is the step that decides whether the move is acceptable.
4. **Create the empty 1.x store and config** at `/etc/stalwart-v1/config.json`. It must be
   empty when the first run starts.
5. **Bulk copy while 0.16 runs** (repeatable, incremental):
   `stalwart-migrate run --source ... --target /etc/stalwart-v1/config.json --state ...`
6. **Window:** stop `stalwart.service`, run the same command with `--final --yes`, confirm
   it reports completion, then start 1.x on the new store with the config the migration
   wrote.
7. **Verify before declaring it done:** `:25` and `:993` listening, IMAP login with a real
   mailbox, one message delivered end-to-end, the webmail's `/api/health`, Authentik
   sign-in, and `check-mail-relay.py` / `check-mail-sso.py` from the edge. A migration is
   not "done" because the process started.
8. **Move the updater's ceiling** only after that: the script follows 0.16 today;
   `--allow-stalwart-major` opens *all* releases, so after 1.x is proven, pin the target
   series in the script (1.0.x, or 1.1.x on purpose) rather than leaving it open-ended.

## Rollback

Nothing the tool writes touches the 0.16 store, so rollback is: stop 1.x, restore
`/usr/local/bin/stalwart` from the backup, restore `/etc/stalwart` if `prepare` or a later
step changed it, start `stalwart.service`, and keep the 0.16 data directory as it was.
Because mail queued during the window is the only new data, expect to re-run the
incremental copy rather than to lose messages.

## Why this is written down and not scheduled

The estate's rule is that an unreachable or unknown state is never a pass. "Stalwart is
current" was that failure in a different shape: the server is *correctly* held on 0.16
while the newest release is 1.1.0, and a green check that says nothing about the gap reads
as health. The updater now names the gap (`blocked 1.1.0 — migration required`), this
document carries the procedure, and the migration itself stays a decision with a window
and a person in it.
