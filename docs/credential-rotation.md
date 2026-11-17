# Rotating the estate's vault credentials

Every consumer in the estate reads its secrets from the edge vault's `cerulean` KV v2
mount, so the age of a secret is the age of the last write to that path — and nothing in
the estate notices a secret that is merely old. That is the gap
`scripts/check-credential-age.py` closes: it reads each path's metadata (never its data)
and reports, per path, how long since the current version was written.

## What the check tells you

| Reading | Meaning | What to do |
| --- | --- | --- |
| `current` (note) | inside the 60-day window | nothing |
| `overdue` (warning) | older than 60 days | schedule the rotation below |
| `missing-secret` (failure) | the path does not exist | a consumer is reading a secret the estate believes it has — restore it or retire the consumer |
| exit 2 | vault, token or transport unreadable | fix that first: a silent series is the failure this check exists to catch |

The 60-day window sits inside the 90-day policy deliberately: it opens the conversation a
month before the policy would, so a rotation can be scheduled instead of squeezed in. The
window is `--max-age-days`, and `systemd/estate-checks.service` passes the same number a
test pins, so the reported window and the running window cannot part company.

The listing comes from the mount itself (`--all --mount cerulean`), so a credential added
tomorrow is covered without editing a list — but a path nobody has written still ages, and
that is the point.

## Procedure

1. **See what is due.** `sudo systemctl status estate-checks` shows the last run; the
   textfile in `/var/lib/node_exporter/textfile/estate-check-credential-age.prom` carries
   the age per path, and the journal names each overdue one with its window.
2. **Rotate at the issuer, not in the vault.** A new vault version of an unchanged value
   resets the clock and fixes nothing. Create the new credential with the product that
   owns it (Mailjet, Authentik, a database, a registry token, an SMTP account).
3. **Write it as a new version** of the same path, keeping every other key in that
   document — consumers read the document, not one field:
   `vault kv put cerulean/<name> <key>=<value> …`. Never paste a secret into a ticket,
   a commit, or a chat.
4. **Prove the consumer picked it up.** Restart or re-materialise only what reads that
   path (`vault-*.sh` helpers write into `/run`), then run the product's own check — the
   Mailjet pair, for example, is only correct if `check-mailjet-inbound` passes its
   sender/parseroute stage.
5. **Retire the old credential at the issuer** once the new one is proven. Where the
   issuer cannot revoke through its API, say so in that secret's runbook and record the
   manual step; leaving the old value live is a rotation that has not finished.
6. **Confirm.** Re-run `scripts/check-credential-age.py --all --mount cerulean`; the path
   should read `current`, and the next timer run publishes the new age.

## Per-secret runbooks

- `cerulean/mailjet` — [mailjet-key-rotation.md](mailjet-key-rotation.md). The master pair
  cannot be rotated through the API: regeneration and deletion are both UI actions, and a
  sub-key cannot back the estate checks.

## Exceptions

- **The vault's own root token** is not a KV secret: it lives in the `proxy` container
  under `/usr/src/projects/complete/1-primary/cerulean/data/vault/init/init.json` and is
  not covered by this check. Treat any exposure of it as an incident
  ([security-incident-2026-09-27-git-miner.md](security-incident-2026-09-27-git-miner.md)).
- **The check's token** (`estate-credential-age`, read-only on `cerulean/metadata/*`) is
  itself periodic and is rotated with the rest of the operational tokens; it grants
  timestamps only.
- **A secret that cannot be rotated** (an upstream that will not issue a second
  credential, a hard-coded pair in a vendor appliance) is recorded here with the reason
  and the compensating control, rather than left to warn forever with no owner.

## Probes are credentials too

`cerulean/__scope_probe__` holds no key, but it is the object the read-only policy is
proved against, and it ages like everything else in the mount — it was the first thing
the sixty-day window caught (67 days, 2026-11-17). It rotates with the same five steps
and takes seconds.

Do not add a name-based exemption for probe paths. A path called `__probe__` is
indistinguishable from a real secret renamed to escape this window, and the check would
have to trust the name to tell them apart — which is the one thing it must not do. If a
path carries no secret, the fix is a new version, not an allowlist.
