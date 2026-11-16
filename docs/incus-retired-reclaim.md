# Reclaiming a retired incus instance (the `monarch-retired` case)

A moved instance leaves two things behind on the source host: the stopped,
autostart-disabled instance and its dataset. Keeping them is the rollback; keeping
them forever is a 25 GB hole in a pool that has to hold something else eventually.
This is the order that reclaims the space without ever being the step that loses the
only copy.

The case this was written from: `monarch` moved from `i1` (`.51`) to `i2` (`.52`) on
2026-11-16 because one spindle could not carry the media stack. The source became
`monarch-retired`, stopped, `boot.autostart=false`, dataset
`tank/incus/containers/monarch-retired` (25.4 GB) retained as rollback.

## Before reclaiming anything

The reclaim is safe only while the destination is the instance that matters. Prove
that, rather than trusting that the copy "finished":

```bash
# on i2 — the live instance is running, autostarting, and carries the data
incus list monarch -c ns -f csv                      # RUNNING
incus config get monarch boot.autostart              # true
incus exec monarch -- docker ps -q | wc -l           # 36 (38 total, 2 one-shot)
incus exec monarch -- ls /data/media                 # movies music temp tv xxx
incus exec monarch -- systemctl is-system-running    # running
```

Two more things decide the confidence window:

* **Time at the destination.** A day of normal traffic is the smallest honest window
  for a media stack; a week is cheap. The rollback is only worth keeping while the
  copy is still unproven.
* **The destination's own headroom.** `monarch` occupies ~34 GB on i2's `fast` pool;
  reclaiming i1's copy does not buy i2 anything, it only returns space to `i1`'s
  spindle. Reclaim because `i1` needs the space, not because the number looks tidy.

## The order

```bash
# 1. Take the lease. Cooperative, but it is what stops two sessions doing this at
#    once, and `incus-guard run` releases it even when the body fails.
incus-guard claim monarch-retired && trap 'incus-guard release monarch-retired' EXIT

# 2. Prove the instance is the one you think it is, and that nothing is using it.
incus info monarch-retired | sed -n '1,12p'          # STOPPED, correct dataset
incus config get monarch-retired boot.autostart      # false
mountpoint -q /var/tmp/monarch-retired && echo "MOUNTED — stop here"

# 3. Delete the instance *definition* first. This is the reversible-looking step and
#    it is not: incus deletes the instance's dataset with it. Do it only after step 4
#    has been read, because step 3 is the point of no return for the dataset.
```

Step 3 is deliberately the last thing that happens, and it is a single command:

```bash
incus delete monarch-retired            # refuses while running; no --force needed
```

Do **not** use `incus delete --force` on a running instance: it is exactly the case
where `boot.autostart` was flipped back on by something else, and the force hides the
answer to "why is a retired instance running".

If the intent is to keep the definition and reclaim only the filesystem, that is a
different operation — `incus config set monarch-retired limits.disk.priority` and
`zfs destroy` are not interchangeable, and destroying a dataset under a live instance
leaves an instance that looks real and is unusable (this is what the aborted 2026-11-16
`incus copy` produced on i2: 22 GB of instance with zero docker state dirs).

## Verify the reclaim

```bash
# i1 — the name is gone, the pool is larger, the spindle is unbothered
incus list -c ns -f csv | grep -i monarch        # nothing
zfs list tank/incus/containers | tail -3         # no monarch-retired row
zpool list tank                                  # ALLOC shrank by ~25.4 GB
cat /proc/pressure/io                            # some avg10 stays low

incus-guard release monarch-retired
```

Then run the estate checks that assert the declaration: `check-retired-instances.py`
will now fail with `retired_missing` **by design**, so remove the `monarch-retired`
row from its `RETIRED` table in the same change that deletes the instance. A check
that keeps asserting a rollback nobody kept is how a real regression starts reading
as noise.

## What this deliberately does not do

* It does not touch `i2`. The live instance and its pool are the thing being
  protected.
* It does not delete the source's *logs* or the operator record; those are the
  evidence that the move happened.
* It is not reversible. The rollback is the dataset; once it is gone, the only copy
  of the media state is the one on i2. That is the decision, stated once, in the
  commit that makes it.
