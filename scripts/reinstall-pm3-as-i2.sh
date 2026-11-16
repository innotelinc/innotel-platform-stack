#!/bin/bash
# Reinstall pm3 (192.168.1.3) as bare-metal Ubuntu 26.04.1 and rename it i2.
#
# WHY THIS EXISTS: the first attempt (2026-11-15 22:19 UTC) staged the installer with
# `kexec -l` and then triggered it with `systemctl kexec`. systemd re-loads a kernel of
# its own instead of executing the staged image, so the Ubuntu kernel came up WITHOUT
# the autoinstall command line: the box answered ICMP on .3, listened on no port, and
# the config server never saw a request from it. Nothing was wiped (`local-lvm` was
# never given a partition plan), but the box needed a physical power cycle to return to
# Proxmox. The trigger below is `kexec -e`, which executes exactly what `kexec -l`
# staged, and it never returns.
#
# SECOND FAILURE (2026-11-16 03:49 UTC): attempt 2 used `kexec -e` with the correct command
# line and still installed nothing. The config server received nothing after the preflight
# fetch, not even /meta-data, which only the installer's own cloud-init asks for; the box
# answered ICMP on .3 from its own MAC (50:a0:30:0d:1c:c4) with every port closed, i.e. the
# live kernel was up with ip=dhcp and no userland. Cause: kexec boots a kernel and an initrd
# but does not carry the ISO mount across, and casper only looks for
# /casper/filesystem.squashfs on mountable devices unless it is told where the image is.
# The ISO is therefore served over HTTP and named on the command line with `url=`; a GET of
# the ISO and of /meta-data in the i2-install-http log is the proof the installer really ran.
#
# THIRD CHANGE (2026-11-16 04:1x UTC): casper's own no-medium path calls panic(), and
# initramfs-tools' panic() reboots (`sleep $panic; reboot -f`) whenever panic= is on the
# command line -- it only drops to an interactive shell without it. With no monitor on this
# box, an interactive shell is the same as a dead machine, which is exactly the state both
# failed attempts left behind. `panic=120` converts that failure mode into an automatic
# reboot into Proxmox in two minutes, so the reinstall can be run remotely without risking
# a box that needs hands on it. Verified at source in the ISO's initrd: scripts/functions
# panic() lines 45-85, scripts/casper line 970 (no live medium), and the url= fetch at
# scripts/casper line 267 (`wget "${URL}" -O "${target}"`, so the image lands in initramfs
# RAM: 2.9 GB of pm3's 9.9 GB free is fine).
#
# Preconditions (all verified before the failures, re-checked here):
#   /root/install/vmlinuz + /root/install/initrd on pm3 (extracted from the live-server ISO)
#   http://192.168.1.54:8080/user-data serving the validated autoinstall
#   http://192.168.1.54:8080/ubuntu-26.04.1-live-server-amd64.iso serving the ISO
#   pm3 reachable over ssh as root (vault: /srv/vault/secrets/proxmox-nodes.txt on i1)
set -euo pipefail
HOST="${1:-192.168.1.3}"
SERVE="http://192.168.1.54:8080"
ISO="${SERVE}/ubuntu-26.04.1-live-server-amd64.iso"
: "${SSHPASS:?set SSHPASS to the pm3 root password}"
SSH() { sshpass -e ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
        -o PreferredAuthentications=password -o ConnectTimeout=10 "root@${HOST}" "$@"; }

echo "== preflight"
# casper must be able to fetch the image; without this the kernel stops in the initramfs.
ISO_BYTES=$(curl -sI -m 5 "${ISO}" | awk 'BEGIN{IGNORECASE=1} /^content-length:/{gsub("\r",""); print $2}')
if [ -z "${ISO_BYTES}" ] || [ "${ISO_BYTES}" -lt 2000000000 ]; then
  echo "FATAL: ${ISO} is not being served (got '${ISO_BYTES}' bytes); copy the ISO into /root/i2-install-http/" >&2
  exit 1
fi
echo "   ISO served: ${ISO_BYTES} bytes"
SSH 'pveversion; ls -l /root/install/vmlinuz /root/install/initrd | awk "{print \$5, \$9}"
     curl -s -m 5 -o /dev/null -w "autoinstall fetch=%{http_code}\n" http://192.168.1.54:8080/user-data
     echo "vm: $(qm list | sed 1d | awk "{print \$2, \$3}")"'

echo "== loading the installer kernel with the autoinstall command line"
SSH "kexec -l /root/install/vmlinuz --initrd=/root/install/initrd \
       --command-line=\"autoinstall ds=nocloud-net;s=${SERVE}/ console=tty0 ip=dhcp nomodeset panic=120 url=${ISO} ---\" \
     && echo staged"
echo "== executing (kexec -e never returns; ssh drops here)"
SSH 'kexec -e' || true
