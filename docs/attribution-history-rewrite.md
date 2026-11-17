# Removing the Codebuff trailers from the platform-stack history — plan, not applied

**Status: not applied. Needs an explicit go-ahead, because applying it rewrites published
history and requires a force-push to `main`.**

## Why

`Attribution guard` fails on `main` with:

```
guard: bad trailer:Co-Authored-By: Codebuff <noreply@codebuff.com>
::error::Attribution guard failed on history — only Darnel Hunter <dhunter@innotel.us> may be credited.
```

The guard scans every reachable commit message and every added line (it has no baseline
file in this repository — `82af91a` and `86ac681` fail the same way), so it fails on history
rather than on any one change. Nine commits carry the trailer:

| Commit | Date | Subject |
| --- | --- | --- |
| `d8d0b78` | 2026-11-16 | Assert the route chunks Jellyfin loads after its page boots |
| `e72173e` | 2026-11-16 | Read Mailjet credentials from cerulean-vault instead of host env files |
| `8b5f2a0` | 2026-11-16 | Stop calling a message sent minutes ago unreported |
| `a57450d` | 2026-11-16 | Provision the mail container's LAN-edge host pins |
| `eee9ca3` | 2026-11-16 | Record every name the mail container pins to the LAN edge |
| `9f54436` | 2026-11-16 | Count mail the way Stalwart 1.1 actually reports it |
| `d25a8fd` | 2026-11-16 | Count the delivery events Stalwart 1.1 actually emits |
| `8b420ea` | 2026-11-16 | Give the SSO-door check its docker prefix and declare two edge targets dark |
| `1a043b1` | 2026-11-16 | Guard incus instances against concurrent sessions, and make migrations resumable |

## Blast radius, measured

- The trailers are on `main` only: no other remote branch contains them (`git branch -r
  --contains 1a043b1` → `origin/main`), and no tag contains them.
- `main` is the protected default branch; the account has `admin` on the repository, so a
  force-push is possible if protection is briefly relaxed or bypassed.
- Rewriting the message of `1a043b1` (the oldest of the nine) changes every SHA from
  `1a043b1` to `main`'s tip. Anything that pins a SHA — CI links in chat, this repository's
  own docs, a deployment that records `git rev-parse` — will name a commit that no longer
  exists.
- The tree is unaffected: this is a message-only rewrite, so every checkout ends with
  identical file contents.

## The change

1. Take a full backup first, and keep it until the rewrite is verified:

   ```bash
   git bundle create /root/platform-stack-pre-attribution.bundle --all
   git clone --mirror . /root/platform-stack-pre-attribution.git
   ```

2. Rewrite messages only — no tree edits — with `git filter-repo` (or `git rebase -i
   --root` editing each of the nine messages by hand):

   ```bash
   git filter-repo --message-callback '
       return re.sub(rb"\n?Co-Authored-By: Codebuff <noreply@codebuff\.com>\s*", b"", message)
   ' --force
   ```

   The pattern must also drop a trailing `the Codebuff footer` line if one is present;
   check with `git log --format=%B <range> | grep -i codebuff` afterwards.

3. Prove the tree is unchanged before pushing anything:

   ```bash
   git diff --stat /root/platform-stack-pre-attribution.git  # expect no output
   git log --format=%B | grep -ci codebuff                    # expect 0
   git fsck --no-progress
   ```

4. Push with a lease, so a concurrent push cannot be clobbered:

   ```bash
   git push --force-with-lease=main origin main
   ```

5. Re-run the guard locally against the rewritten history before the push, and after it,
   confirm `Attribution guard` is green on the new tip.

## Rollback

The bundle and the mirror clone are the rollback: `git push --force-with-lease origin
<old-tip>:main` restores the previous history, and the guard returns to failing exactly as
it does now. Nothing in the tree needs restoring.

## Why this is not done automatically

A force-push to a protected default branch is destructive and hard to undo for anyone
holding an old clone, and the user has not authorized it. The two alternatives are both
worse: adding `.githooks/attribution-baseline` would silence the guard over the offending
range instead of removing the trailers (and the guard exists to prevent exactly that kind of
silencing), and leaving it red means the guard's verdict carries no information.
