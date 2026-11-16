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
# Preconditions (all verified before the failure, re-checked here):
#   /root/install/vmlinuz + /root/install/initrd on pm3 (extracted from the live-server ISO)
#   http://192.168.1.54:8080/user-data serving the validated autoinstall
#   pm3 reachable over ssh as root (vault: /srv/vault/secrets/proxmox-nodes.txt on i1)
set -euo pipefail
HOST="${1:-192.168.1.3}"
: "${SSHPASS:?set SSHPASS to the pm3 root password}"
SSH() { sshpass -e ssh -o StrictHostKeyChecking=no -o PubkeyAuthentication=no \
        -o PreferredAuthentications=password -o ConnectTimeout=10 "root@${HOST}" "$@"; }

echo "== preflight"
SSH 'pveversion; ls -l /root/install/vmlinuz /root/install/initrd | awk "{print \$5, \$9}"
     curl -s -m 5 -o /dev/null -w "autoinstall fetch=%{http_code}\n" http://192.168.1.54:8080/user-data
     echo "vm: $(qm list | sed 1d | awk "{print \$2, \$3}")"'

echo "== loading the installer kernel with the autoinstall command line"
SSH 'kexec -l /root/install/vmlinuz --initrd=/root/install/initrd \
       --command-line="autoinstall ds=nocloud-net;s=http://192.168.1.54:8080/ console=tty0 ip=dhcp nomodeset ---" \
     && echo staged'
echo "== executing (kexec -e never returns; ssh drops here)"
SSH 'kexec -e' || true
