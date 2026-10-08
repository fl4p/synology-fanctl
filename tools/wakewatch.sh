#!/bin/sh
# Find what keeps the disks from hibernating.
# Run as root, then leave the box alone (no SSH, no browser) for DURATION.
# Every disk-reaching access by a process is logged via block_dump; each
# reset of DSM's per-disk idle counter is marked. Output goes to tmpfs only,
# so the tool itself does not touch the disks.
OUT=${OUT:-/run/wakewatch.log}
DURATION=${DURATION:-2700}
DISKS=${DISKS:-"sda sdb"}

trap 'echo 0 > /proc/sys/vm/block_dump' EXIT
trap 'exit 1' INT TERM

: > "$OUT"
log() { echo "$(date +%T) $*" >> "$OUT"; }
idle() { for d in $DISKS; do printf "%s=%s " "$d" "$(cat /sys/block/$d/device/syno_idle_time)"; done; }
state() { for d in $DISKS; do printf "%s=%s " "$d" "$(hdparm -C /dev/$d 2>/dev/null | awk '/state/ {print $NF}')"; done; }

dmesg -c > /dev/null
echo 1 > /proc/sys/vm/block_dump
log "start duration=${DURATION}s disks=$DISKS idle: $(idle)"
end=$(( $(date +%s) + DURATION ))
prev=0; tick=0
while [ "$(date +%s)" -lt "$end" ]; do
    sleep 5
    # keep process-attributed I/O that reaches a disk or the system RAID; drop
    # tmpfs/proc dirtying and the kernel threads that only carry it out
    dmesg -c | grep -E "(READ|WRITE) block|dirtied inode" \
        | grep -E " on (md[0-9]+|dm-[0-9]+|sd[a-z][0-9]*)" \
        | grep -vE "\((jbd2/|md[0-9]+_raid|flush-|kworker)" >> "$OUT.raw"
    first=$(cat /sys/block/${DISKS%% *}/device/syno_idle_time)
    if [ "$first" -lt "$prev" ]; then
        log "RESET idle was ${prev}s; recent I/O:"
        tail -n 8 "$OUT.raw" | sed 's/^/    /' >> "$OUT"
    fi
    prev=$first
    tick=$((tick + 1))
    [ $((tick % 12)) -eq 0 ] && log "idle: $(idle) state: $(state)"
done
log "end"
