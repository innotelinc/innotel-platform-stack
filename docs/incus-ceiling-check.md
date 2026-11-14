# The Incus ceiling check: measuring the bound that exists

`swap-ceiling-check` runs every ten minutes on every estate host and stays silent unless an
instance is filling a memory or swap ceiling. From 2026-10-25 until **2026-11-14** it was
red on `i1` on every single run, 344 lines of log, all saying the same thing:

```
CRIT  atheniq swap 122% of ceiling (2509 MiB of 2048 MiB) — an OOM kill is one burst away
```

**That sentence was false**, and it is the reason the check had become wallpaper.

## What was measured

`limits.memory.swap: 2GiB` is a *configured number*. What the kernel enforces is the cgroup
limit, and this is what `atheniq` actually had:

| source | value |
|---|---|
| `incus config get atheniq limits.memory` | `3GiB` |
| `incus config get atheniq limits.memory.enforce` | **`soft`** |
| `incus config get atheniq limits.memory.swap` | `2GiB` |
| `/sys/fs/cgroup/memory.max` inside the instance | **`max`** |
| `/sys/fs/cgroup/memory.swap.max` inside the instance | **`max`** |
| `memory.current` / `memory.swap.current` | 683 MiB / 2627 MiB (2479–2630 MiB across runs) |

With `enforce: soft`, Incus programs no `memory.max` and no `memory.swap.max`. There was no
instance ceiling to be 122 % of, and no cgroup OOM kill was "one burst away" — the comparison
was against a target, not a bound. The old script read the config (`limits.memory.swap`) and
never looked at the cgroup.

## What the check does now

- Reads **`memory.max`** and **`memory.swap.max`** from inside each instance and compares
  usage against those. `max` is treated as *no ceiling*, not as a zero-byte ceiling.
- Therefore a soft-capped instance cannot produce an instance alert. It cannot: there is
  nothing to reach.
- Adds the bound that *does* exist for a soft-capped instance: **host swap**. An instance
  with no ceiling of its own can still exhaust the swap every instance shares. WARN at 75 %
  used, CRIT at 90 %, and the line names the top consumer.
- Thresholds read from the environment when set (`WARN_PCT`, `CRIT_PCT`, `MEM_WARN_PCT`,
  `MEM_CRIT_PCT`, `HOST_WARN_PCT`, `HOST_CRIT_PCT`) so the branches can be exercised on a
  host that is comfortable.

## How it was verified (2026-11-14, `i1`)

| test | expected | measured |
|---|---|---|
| normal run | silent, exit 0, no new log lines | `rc=0 new_lines=0` (log stayed at 344) |
| `HOST_WARN_PCT=60` | WARN + exit 1 | `WARN host swap 68% used (2791 MiB of 4095 MiB); top consumer atheniq 2479 MiB`, `rc=1` |
| `HOST_WARN_PCT=50 HOST_CRIT_PCT=50` | CRIT + exit 1 | `CRIT host swap 68% ..., top consumer atheniq 2480 MiB`, `rc=1` |

The first forced run produced **nothing**, which exposed a second bug: the host-swap reader
matched `/^Swap:/`, a line `/proc/meminfo` does not have (it reports `SwapTotal:` and
`SwapFree:`). The branch was dead code that looked correct. It now derives used from
`SwapTotal - SwapFree`, and the test above is what proves it.

## The condition that is real

`atheniq` holds ~2.4–2.6 GiB of the host's 4095 MiB swap, with a working set of ~456 MiB and
no enforced cgroup cap (`enforce: soft`, 3 GiB declared). That is a **capacity note, not an
OOM risk**: the instance cannot be OOM-killed by its own cgroup because it has no hard cap.
It is the largest single consumer of a shared resource, so if host swap crosses 75 % the
check names it. As of 2026-11-14 the host sits at 68 % — under the warning line, close to it.

If a hard ceiling is actually wanted for `atheniq`, set `limits.memory.enforce=hard`; the
check will then read `memory.max` and alert against it. Nothing in the check should be
adjusted to make a soft limit look hard.
