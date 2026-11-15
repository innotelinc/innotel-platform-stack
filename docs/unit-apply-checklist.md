# Applying the units on a host

Everything in `systemd/` is a piece that has to be installed **somewhere**, and half of the
incidents in this repository are a piece that was installed in the wrong order or on the
wrong host and then looked healthy. `docs/root-paths.md` is why a script is not in `/root`;
this is the order the pieces go on in, the host each one belongs to, and the one command
per piece that says it landed rather than that it was copied.

## The order, and what each step is for

1. **Read the unit's own header first.** Most of them name their host in the first six
   lines (`# On i1 only.`, `# Runs on the host that carries the agent sessions — i4`), and
   that comment is the authority — `docs/host-map.md` has the addresses when it is missing.
2. **The script, before the unit.** `ExecStart=` naming a file that is not there is every
   night `status=203/EXEC` and no other symptom; the 2026-10-25 cleanup
   (`docs/root-paths.md`) and the vault mirror are both that. Install to
   `/usr/local/sbin` (or the container's `/usr/local/bin`), then `test -x` it. For the
   estate checks that step is `bash scripts/install-estate-checks.sh`: it takes its list
   from the unit's own `ExecStart=` lines (the scripts *and* the tables passed to
   `--pins/--expect`), adds the shared `estate_check.py` and the sibling files a check
   reads, and prints `MISSING`/`FAIL` rather than exiting 0 over a copy that did not land.
3. **The env file, before `enable --now`.** `EnvironmentFile=-` is *optional* on purpose
   in most units, so a missing credential does not stop the job — it makes it fail
   differently. `estate-notify` is the exception: with no `/etc/innotel/ntfy.env` it exits
   `CANNOT RUN` rather than pretend to have notified anyone. Every one is `0600`.
4. **The unit and its timer**, into `/etc/systemd/system`, then `systemctl daemon-reload`.
   A unit edited in place without the reload keeps running the old text, which is why a
   fixed `ExecStart=` can look unfixed.
5. **`systemctl enable --now`**, then force one run rather than waiting for the cadence:
   `systemctl start <unit>` and read `systemctl show -p Result -p ExecMainStatus <unit>`.
   The status is the verdict; "active" is only whether it is still going.
6. **Confirm the artefact the unit promises**, not that the unit ran: a `.prom` in
   `TEXTFILE_DIR`, a message on the ntfy topic, a restore point on disk. Those are in the
   table below.

## Where each piece goes

| piece | host | installs to | proves it |
|---|---|---|---|
| `estate-checks.{service,timer}` | the Cerulean edge (`proxy` on `i2`) | `/etc/systemd/system`, and `scripts/install-estate-checks.sh` fills `/opt/innotel/estate-checks/` | a `.prom` per check in `TEXTFILE_DIR`, and `EstateCheckAbsent` quiet for 30 min |
| `estate-notify@.service` + `estate-notify.sh` | wherever the wired unit runs | unit in `/etc/systemd/system`, script in `/usr/local/sbin` | `systemctl start estate-notify@<unit>` exits 0 and the topic holds the message |
| `proxy-backup.{service,timer}` | `i2` (`hosts/i2/proxy-backup/`) | script `/usr/local/sbin/backup-proxy.sh` **first** | a dated restore point, and `Result=success` |
| `vault-replica-sync.{service,timer}` | the host that owns `/srv/vault` | `/usr/local/sbin/vault-replica-sync`, log in `/var/log/` | the replica's newest mtime moved |
| `vault-disk-health.{service,timer}` | the same host | `/usr/local/sbin/vault-disk-health.sh` | `Result=success` (a missing drive is a finding, not an error) |
| `media-orphans.{service,timer}` | the host running the media stack | `/usr/local/sbin/media-orphans.sh` | its report; `OnFailure` wired |
| `regcache-credential.{service,timer}` | the host running the `regcache` container | `/usr/local/sbin/regcache-credential-check.sh` | `Result=success` |
| `monarch-torrent-perms.{service,timer}` | **i1 only** (its own header says so) | `/usr/local/sbin/monarch-torrent-perms.sh` | ownership on the media folders after one run |
| `agent-reaper.{service,timer}` | **i4** (its own header) | `/usr/local/sbin/reap-wedged-agents.py` | `agent-reaper.prom` in `TEXTFILE_DIR` |
| `edge-liveness.{service,timer}` | a host that is **not** the edge | `/usr/local/sbin/check-edge-liveness.py` | its own channel, not the ntfy topic |
| `swap-ceiling-check.{service,timer}` | an incus host | `/usr/local/sbin/swap-ceiling-check.sh` | the log line; **do not** wire `OnFailure` (see its header) |
| `memguard.service` | the host it guards | `/usr/local/sbin/memguard.py` | it is long-running: `active`, not a timer |
| `sync-mail-identities.{service,timer}` | **inside** the `mail` container | `/usr/local/bin/sync-mail-identities.py` | `mailboxes=… missing=0`, and `check-mail-identities` clean |
| `stalwart-watchdog.{service,timer}`, `stalwart-oidc-proxy`, `stalwart-oidc-redirect` | **inside** the `mail` container | `scripts/stalwart-oidc-discovery-setup.sh` installs all of it and refuses to run half-armed | `check-stalwart-oidc-cache` clean |

`sync-mail-identities` is the one scheduled job in the estate with no `OnFailure=` and a
metric instead: it retries every fifteen minutes, so a notification would repeat a standing
cause 96 times a day. That decision, and the per-unit verdict table it comes from, is
`docs/estate-alerts-ntfy.md`.

## The three traps, and the symptom each one has

| done out of order | symptom | why it is invisible |
|---|---|---|
| unit copied before its script | `203/EXEC`, every run | the timer, the unit and the path all exist; only `ExecMainStatus` differs from a quiet success |
| `estate-checks` enabled before `TEXTFILE_DIR` is right | every check fires `EstateCheckAbsent` a day later, nothing else | the checks publish fine, into a directory nothing scrapes |
| `OnFailure=` written with a suffix (`@%n.service`) | a notification for a unit that does not exist | the message arrives; only its `journal` section is empty |

## After the apply

```bash
# what is scheduled, and when it next runs
systemctl list-timers --all --no-pager | grep -E 'estate|vault|proxy-backup|media|sync-mail'

# every unit's verdict, not its state
systemctl show -p Result -p ExecMainStatus estate-checks.service proxy-backup.service

# the invariant, so a re-apply has not reintroduced the 203/EXEC
grep -rnE 'Exec(Start|Stop)=.*/root/' /etc/systemd/system

# and the estate's own view: a check that stops reporting is a page, not silence
curl -s 127.0.0.1:9100/metrics | grep -c innotel_estate_check_last_run_timestamp
```
