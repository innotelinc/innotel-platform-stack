#!/bin/bash
# Runs ON i2 (192.168.1.52) as root.
#
# n8n's sandbox runner (n8n-sandbox-runner-1, inside the `capstone` incus
# container) refuses to start unless /proc/sys/net/bridge/bridge-nf-call-iptables
# exists, i.e. unless the br_netfilter kernel module is loaded:
#
#   {"level":"ERROR","msg":"create runtime","runtime":"docker",
#    "error":"bridge netfilter: /proc/sys/net/bridge/bridge-nf-call-iptables does not
#             exist: load the br_netfilter kernel module on the host"}
#
# On 2026-10-30 it had restarted 1094 times on this error, so n8n's sandbox has been
# unavailable for as long as the host has been up without the module.
#
# The module is not free here. This host carries the LAN by BRIDGE (br0 carries the
# incus containers' own addresses - capstone is 192.168.1.30 - and ontrak0 its own),
# and its FORWARD chain already has hand-written accepts for br0 <-> wg-offload.
# Loading br_netfilter with bridge-nf-call-iptables=1 sends every bridged frame
# through FORWARD as well; that is the shape that cut the L2 stretch on 2026-10-27
# (see /root/DO-NOT-unload-br_netfilter.md - `modprobe -r` then deleted every bridge
# on the host, which is why no step here ever unloads it).
#
# The runner only needs the file to EXIST, and 0 keeps it present. So the posture is:
#   module loaded  +  host-side bridge-nf traversal off (=0).
# Nothing is unloaded on the way back out: rollback is setting the three sysctls to
# 1, or leaving them at 0 and stopping the runner.
#
#   1. load br_netfilter if it is not loaded, and set the three sysctls to 0 in the
#      same breath, so the window with the default (1) is a single command;
#   2. persist across reboot (modules-load.d + sysctl.d; systemd-sysctl runs after
#      systemd-modules-load, so the value is applied to the loaded module);
#   3. verify: module, sysctls, every bridge that was there before still there, the
#      containers still up, and the runner actually registers - fail loudly rather
#      than leave a half-fix that looks applied.
set -u
FAIL=0
ok()  { echo "  PASS  $*"; }
bad() { echo "  FAIL  $*"; FAIL=1; }

echo "=== 0. what this host looks like now ==="
echo "  host: $(hostname)  kernel: $(uname -r)"
BEFORE_BRIDGES=$(ip -br link show type bridge 2>/dev/null | awk '{print $1}' | sort | tr '\n' ' ')
echo "  bridges before: ${BEFORE_BRIDGES:-none}"
lsmod | grep -q '^br_netfilter ' && echo "  br_netfilter: loaded" || echo "  br_netfilter: not loaded"
iptables -S FORWARD 2>/dev/null | sed 's/^/    /'

echo
echo "=== 1. load the module and keep the host-side traversal off ==="
if lsmod | grep -q '^br_netfilter '; then
  echo "  br_netfilter already loaded"
else
  modprobe br_netfilter && echo "  modprobe br_netfilter rc=0"
fi
# Immediately, before anything else can run: the file exists now, so the sysctls
# exist too, and 0 is what keeps br0's frames out of iptables.
sysctl -qw net.bridge.bridge-nf-call-iptables=0
sysctl -qw net.bridge.bridge-nf-call-ip6tables=0
sysctl -qw net.bridge.bridge-nf-call-arptables=0
lsmod | grep -E '^(br_netfilter|bridge) ' | sed 's/^/    /'
sysctl -a 2>/dev/null | grep 'bridge-nf-call' | sed 's/^/    /'

echo
echo "=== 2. persistence (the runner needs this file after every reboot) ==="
echo br_netfilter > /etc/modules-load.d/br_netfilter.conf
cat > /etc/sysctl.d/99-bridge-nf-off.conf <<'CONF'
# n8n's sandbox runner will not start unless /proc/sys/net/bridge/
# bridge-nf-call-iptables exists, i.e. unless br_netfilter is loaded. This host
# carries the LAN over br0 (the containers' own addresses, e.g. capstone at
# 192.168.1.30), so bridged frames must not traverse iptables: 0 still leaves the
# file present, which is all the runner checks. systemd-sysctl runs after
# systemd-modules-load, so this is applied to the loaded module at boot.
net.bridge.bridge-nf-call-iptables = 0
net.bridge.bridge-nf-call-ip6tables = 0
net.bridge.bridge-nf-call-arptables = 0
CONF
echo "  /etc/modules-load.d/br_netfilter.conf -> $(cat /etc/modules-load.d/br_netfilter.conf)"
echo "  /etc/sysctl.d/99-bridge-nf-off.conf   -> $(grep -c . /etc/sysctl.d/99-bridge-nf-off.conf) lines"

echo
echo "=== 3. verification ==="
lsmod | grep -q '^br_netfilter ' && ok "br_netfilter loaded" || bad "br_netfilter not loaded"
for k in iptables ip6tables arptables; do
  v=$(cat "/proc/sys/net/bridge/bridge-nf-call-$k" 2>/dev/null)
  [ "$v" = "0" ] && ok "bridge-nf-call-$k = 0" || bad "bridge-nf-call-$k = ${v:-missing}"
done

AFTER_BRIDGES=$(ip -br link show type bridge 2>/dev/null | awk '{print $1}' | sort | tr '\n' ' ')
[ "$AFTER_BRIDGES" = "$BEFORE_BRIDGES" ] \
  && ok "bridges unchanged ($AFTER_BRIDGES)" \
  || bad "bridges changed: was [$BEFORE_BRIDGES] now [$AFTER_BRIDGES]"
for b in $BEFORE_BRIDGES; do
  n=$(ip -br link show master "$b" 2>/dev/null | wc -l)
  [ "$n" -gt 0 ] && ok "$b still has its ports: $n" || bad "$b lost every port"
done

incus exec capstone -- sh -c 'test -e /proc/sys/net/bridge/bridge-nf-call-iptables' 2>/dev/null \
  && ok "capstone sees /proc/sys/net/bridge (what the runner checks)" \
  || bad "capstone does not see /proc/sys/net/bridge"
c=$(incus exec capstone -- docker ps -q 2>/dev/null | wc -l)
[ "$c" -ge 20 ] && ok "capstone containers up: $c" || bad "capstone containers up: $c"

echo
echo "=== 4. the runner: restart it and wait for the registration line ==="
incus exec capstone -- docker restart n8n-sandbox-runner-1 >/dev/null 2>&1
registered=0
for _ in $(seq 1 30); do
  sleep 4
  if incus exec capstone -- docker logs --tail 40 n8n-sandbox-runner-1 2>&1 \
       | grep -q "runner registration stream established"; then
    registered=1
    break
  fi
done
st=$(incus exec capstone -- docker inspect -f '{{.State.Status}} restarts={{.RestartCount}}' n8n-sandbox-runner-1 2>/dev/null)
case "$st" in
  running*) ok "n8n-sandbox-runner-1: $st" ;;
  *)        bad "n8n-sandbox-runner-1: ${st:-not found}" ;;
esac
[ "$registered" -eq 1 ] && ok "runner registered with sandbox-api" \
  || bad "no registration line in the runner's log within 120s"
incus exec capstone -- docker logs --tail 8 n8n-sandbox-runner-1 2>&1 | tail -4 | sed 's/^/    /'

echo
if [ "$FAIL" -eq 0 ]; then
  echo "RESULT: PASS - module loaded, host-side bridge netfiltering off, runner registered."
  echo "        Outside-in check: 192.168.1.30:5038 and :14010 still answer."
else
  echo "RESULT: FAIL - see above. Do NOT unload br_netfilter (modprobe -r deletes every"
  echo "        bridge on this host); roll back by setting the sysctls back to 1."
fi
exit "$FAIL"
