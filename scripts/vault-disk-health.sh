#!/bin/bash
# i1: the vault disk's own health, from SMART.
#
# The 2026-11-14 outage was the USB link, not the platters: the drive dropped off the
# bus mid-write, ext4 went to `shutdown`, and it came back as a different device node.
# SMART cannot see a bridge reset -- but it can see the things that make a *disk* fail
# silently, which is the other half: reallocated and pending sectors, offline
# uncorrectable reads, a rising UDMA CRC count (cable or enclosure, not the disk), and a
# temperature the drive has already run past once (its worst reading, 43 C, is inside a
# 45 C threshold).
#
# Address the disk by LABEL, never by /dev/sdX: the whole point of the incident is that
# the kernel's letter for this drive is not stable.
#
# Exit codes follow the estate: 0 clean, 1 something does not hold, 2 could not run.
set -u
NAME=vault-disk-health
LABEL=${LABEL:-VAULT}
LOG=${LOG:-/var/log/vault-disk-health.log}
# The bridge in the enclosure is SATA behind USB, so the attribute page needs `-d sat`.
# Overridable for the day the disk is plugged into a real SATA port.
SMART_DTYPE=${SMART_DTYPE:-sat}

# Thresholds are env-overridable so each branch can be exercised; production uses these.
TEMP_WARN=${TEMP_WARN:-50}
TEMP_CRIT=${TEMP_CRIT:-55}
CRC_WARN=${CRC_WARN:-1}

say() { echo "$*"; }
findings=()
add() { findings+=("$1"); }

if ! command -v smartctl >/dev/null 2>&1; then
  say "ERROR: smartctl is not installed -- the vault disk cannot be judged"
  say "       (apt-get install smartmontools)"
  exit 2
fi

DEV=$(readlink -f "/dev/disk/by-label/$LABEL" 2>/dev/null)
if [ -z "$DEV" ] || [ ! -b "$DEV" ]; then
  say "ERROR: no block device is labelled $LABEL -- the drive is absent"
  exit 2
fi

# `-H` is the overall verdict, `-A` the attributes, `-i` the identity: the model and serial
# live only in the information section, so reading them from `-A` yields an empty name and
# an alert that cannot say which disk it is about.
health=$(smartctl -H -d "$SMART_DTYPE" "$DEV" 2>/dev/null | sed -n 's/^SMART overall-health self-assessment test result: *//p')
info=$(smartctl -i -d "$SMART_DTYPE" "$DEV" 2>/dev/null)
attrs=$(smartctl -A -d "$SMART_DTYPE" "$DEV" 2>/dev/null)
if [ -z "$attrs" ]; then
  say "ERROR: SMART attributes unreadable from $DEV (bridge: $SMART_DTYPE) -- cannot judge the disk"
  exit 2
fi

# RAW_VALUE is the last column; the leading columns are normalized scores and are not
# what a threshold is about. Seagate packs extra bytes into some raw values (Power_On_Hours
# reads `5510 (207 0 0)`), so take the first number only.
raw() { awk -v id="$1" '$1 == id { print $10; exit }' <<<"$attrs" | tr -d '\r' | awk '{print $1}'; }
norm() { awk -v id="$1" '$1 == id { print $3; exit }' <<<"$attrs"; }
whosefail() { awk -v id="$1" '$1 == id { print $9; exit }' <<<"$attrs"; }

model=$(sed -n 's/^Device Model: *//p' <<<"$info" | head -1)
serial=$(sed -n 's/^Serial Number: *//p' <<<"$info" | head -1)
hours=$(raw 9)
temp=$(raw 194)
[ -n "$temp" ] || temp=$(raw 190)

if [ -n "$health" ] && [ "$health" != "PASSED" ]; then
  add "CRIT overall SMART health is '$health' on $model ($serial)"
fi

for pair in "5:Reallocated_Sector_Ct" "197:Current_Pending_Sector" "198:Offline_Uncorrectable" "187:Reported_Uncorrect"; do
  id=${pair%%:*}; label=${pair#*:}
  value=$(raw "$id")
  if [ -n "$value" ] && [ "$value" -gt 0 ] 2>/dev/null; then
    add "CRIT $label=$value on $model ($serial) -- the drive has already lost or cannot read sectors"
  fi
done

crc=$(raw 199)
if [ -n "$crc" ] && [ "$crc" -ge "$CRC_WARN" ] 2>/dev/null; then
  add "WARN UDMA_CRC_Error_Count=$crc -- the SATA link through the enclosure has errored; this is the cable or the bridge, not the platters"
fi

if [ -n "$temp" ] && [ "$temp" -ge "$TEMP_CRIT" ] 2>/dev/null; then
  add "CRIT temperature ${temp} C (>= ${TEMP_CRIT} C) on $model -- it was 43 C against a 45 C threshold on 2026-11-14"
elif [ -n "$temp" ] && [ "$temp" -ge "$TEMP_WARN" ] 2>/dev/null; then
  add "WARN temperature ${temp} C (>= ${TEMP_WARN} C) on $model"
fi

# A drive whose normalized score has already crossed its manufacturer threshold is
# failing by the vendor's own arithmetic, whatever the raw count says.
for id in 5 10 184 187 197 198 199; do
  failed=$(whosefail "$id")
  if [ -n "$failed" ] && [ "$failed" != "-" ] && [ "$failed" != "In_the_past" ]; then
    add "CRIT attribute $id is failing now ($failed) on $model ($serial)"
  fi
done

if [ ${#findings[@]} -eq 0 ]; then
  say "ok   $model ($serial) at $DEV: health ${health:-unknown}, ${hours:-?} power-on hours, ${temp:-?} C, 0 reallocated/pending/uncorrectable/CRC"
  exit 0
fi

stamp=$(date '+%Y-%m-%dT%H:%M:%S%z')
for finding in "${findings[@]}"; do
  say "$finding"
  echo "$stamp $finding" >>"$LOG" 2>/dev/null || say "WARN could not append to $LOG"
done
say ""
say "$NAME: ${#findings[@]} finding(s) on $DEV ($model $serial)"
exit 1
