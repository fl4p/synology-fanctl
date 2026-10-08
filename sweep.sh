#!/bin/sh
# Descending fan-duty sweep: lowest duty that holds the SoC below the limit.
# Run as root with fanctl stopped. Always leaves the fan at V20 on exit.
LOG=/var/log/fansweep.log
HOLD=${HOLD:-360}          # s per level
ABORT=${ABORT:-65}          # C: fail level immediately (sensor updates only every ~60 s)
RISING_MAX=${RISING_MAX:-60}     # C: fail level if above this at the end of the hold
LEVELS="${*:-09 08 07 06 05 04 03 02}"

cpu() { grep -o '[0-9]\+' /run/hwmon/cpu_temperature.json | tail -1; }
say() { echo "$(date +%T) $*" | tee -a "$LOG"; }
set_duty() { printf "V$1" > /dev/ttyS1; }
trap 'set_duty 20; say "exit: fan V20"' EXIT
trap 'exit 1' INT TERM

printf t > /dev/ttyS1
say "sweep start levels=$LEVELS hold=${HOLD}s abort=${ABORT}"
for d in $LEVELS; do
    say "level V$d"
    t0=$(date +%s); prev=""; first=""; last=""
    while [ $(( $(date +%s) - t0 )) -lt $HOLD ]; do
        set_duty "$d"
        sleep 15
        c=$(cpu)
        [ -z "$first" ] && first=$c
        last=$c
        if [ "$c" -ge "$ABORT" ]; then
            say "V$d FAIL cpu=$c >= $ABORT"
            exit 0
        fi
        [ "$c" != "$prev" ] && say "  V$d cpu=$c"
        prev=$c
    done
    if [ "$last" -gt "$RISING_MAX" ]; then
        say "V$d FAIL cpu=$last > $RISING_MAX at end of hold"
        exit 0
    fi
    say "V$d PASS end_cpu=$last"
done
say "all levels passed"
