#!/usr/bin/env bash
# Spike 4: memory per desktop VM, cold boot / stateful wake / pause times, and how many fit on a host.
# Run as root on the Incus host after host/setup.sh, image/build.sh and the gateway.
#
#   spikes/04-density/measure.sh [count] [memory] [cpus]      defaults: 3 4GiB 2
#
# Each VM boots the desktop image, trusts the gateway CA, opens Chromium on a real page and idles
# for a minute before memory is read. Memory is read on the host (the QEMU process: RSS and PSS,
# PSS splits pages KSM or the page cache share between VMs) and in the guest (free).
set -uo pipefail
N=${1:-3}; MEM=${2:-4GiB}; CPU=${3:-2}
IMG=${PAC_IMAGE:-pac-desktop/dev}
P=(--project pac)
OUT=${OUT:-/root/spike4-$(date -u +%Y%m%dT%H%M%S).log}
GWCA=$(incus exec "${P[@]}" pac-gw -- cat /var/lib/pac-egress/mitm/mitmproxy-ca-cert.pem)
HERE=$(cd "$(dirname "$0")/../.." && pwd)

log() { printf '%s\n' "$*" | tee -a "$OUT"; }
now() { date +%s.%N; }
since() { awk -v a="$1" -v b="$(now)" 'BEGIN{printf "%.1f", b-a}'; }
as_agent() { local vm=$1; shift; incus exec "${P[@]}" "$vm" -- runuser -u agent -- env DISPLAY=:1 "$@"; }
wait_agent() { for _ in $(seq 1 300); do incus exec "${P[@]}" "$1" -- true >/dev/null 2>&1 && return 0; sleep 0.5; done; return 1; }
wait_desktop() {
  for _ in $(seq 1 300); do
    as_agent "$1" sh -c 'xdpyinfo >/dev/null 2>&1 && pgrep -x xfdesktop >/dev/null' 2>/dev/null && return 0; sleep 0.5
  done; return 1
}
qemu_pid() { incus info "${P[@]}" "$1" | awk '/^PID:/{print $2}'; }
mem_line() {  # host RSS/PSS of the VM's QEMU process and guest-side used memory, MiB
  local vm=$1 pid; pid=$(qemu_pid "$vm")
  local rss pss used
  rss=$(awk '/^VmRSS/{printf "%d", $2/1024}' /proc/"$pid"/status)
  pss=$(awk '/^Pss:/{printf "%d", $2/1024}' /proc/"$pid"/smaps_rollup)
  used=$(incus exec "${P[@]}" "$vm" -- free -m | awk '/^Mem:/{print $3"/"$7}')
  log "mem  $vm  host_rss=${rss}MiB host_pss=${pss}MiB guest_used/avail=${used}MiB"
}
host_avail() { awk '/MemAvailable/{printf "%d", $2/1024}' /proc/meminfo; }

log "== spike 4 on $(hostname), $(date -u +%FT%TZ): $N x ($CPU vCPU, $MEM), image $IMG"
log "host: $(nproc) threads, $(awk '/MemTotal/{printf "%d", $2/1024}' /proc/meminfo) MiB total, $(host_avail) MiB available"
log "ksm: run=$(cat /sys/kernel/mm/ksm/run 2>/dev/null) pages_sharing=$(cat /sys/kernel/mm/ksm/pages_sharing 2>/dev/null)"
AVAIL0=$(host_avail)

for i in $(seq 1 "$N"); do
  vm=pac-d$i
  incus delete -f "${P[@]}" "$vm" >/dev/null 2>&1
  t=$(now)
  incus launch "$IMG" "$vm" "${P[@]}" --vm -c limits.cpu="$CPU" -c limits.memory="$MEM" \
    -c security.secureboot=false -c migration.stateful=true \
    -d root,size=20GiB -d root,size.state="$MEM" >/dev/null
  wait_agent "$vm"; a=$(since "$t")
  wait_desktop "$vm"; d=$(since "$t")
  log "boot $vm  agent_ready=${a}s desktop_ready=${d}s (first boot, includes cloud-init)"
  printf '%s\n' "$GWCA" | incus exec "${P[@]}" "$vm" -- bash -c "cat > /tmp/ca.pem"
  incus file push "${P[@]}" "$HERE/image/trust-gateway-ca.sh" "$vm/tmp/trust-gateway-ca.sh" >/dev/null
  incus exec "${P[@]}" "$vm" -- bash -c 'bash /tmp/trust-gateway-ca.sh < /tmp/ca.pem' >/dev/null
  as_agent "$vm" sh -c 'setsid chromium --no-first-run --password-store=basic https://en.wikipedia.org/wiki/Personal_computer >/dev/null 2>&1 &'
done

log "== idle 60s with Chromium open"
sleep 60
for i in $(seq 1 "$N"); do mem_line pac-d$i; done
AVAIL1=$(host_avail)
log "host available: before=${AVAIL0}MiB after=${AVAIL1}MiB  => $(( (AVAIL0 - AVAIL1) / N )) MiB per VM"
log "ksm: pages_sharing=$(cat /sys/kernel/mm/ksm/pages_sharing 2>/dev/null)"

log "== stateful stop and wake (pac-d1)"
before=$(as_agent pac-d1 pgrep -o chromium)
t=$(now); incus stop "${P[@]}" --stateful pac-d1; log "stateful stop: $(since "$t")s"
log "after stop: host available $(host_avail)MiB"
t=$(now); incus start "${P[@]}" pac-d1; s=$(since "$t"); wait_agent pac-d1; a=$(since "$t")
after=$(as_agent pac-d1 pgrep -o chromium)
log "stateful wake: start=${s}s agent_ready=${a}s chromium_pid before=$before after=$after ($( [ "$before" = "$after" ] && echo same process, session kept || echo NOT the same process))"
mem_line pac-d1

log "== pause / resume (pac-d2, memory stays resident)"
if [ "$N" -ge 2 ]; then
  t=$(now); incus pause "${P[@]}" pac-d2; log "pause: $(since "$t")s"
  t=$(now); incus start "${P[@]}" pac-d2; wait_agent pac-d2; log "resume to agent_ready: $(since "$t")s"
fi

log "== cold stop and boot (pac-d$N)"
t=$(now); incus stop "${P[@]}" "pac-d$N"; log "stop: $(since "$t")s"
t=$(now); incus start "${P[@]}" "pac-d$N"; wait_agent "pac-d$N"; a=$(since "$t"); wait_desktop "pac-d$N"
log "cold boot: agent_ready=${a}s desktop_ready=$(since "$t")s"

log "results: $OUT"
