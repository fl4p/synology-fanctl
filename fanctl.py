#!/usr/bin/env python3
"""Quiet fan controller for Synology DS214play (DSM 7.1, evansport).

Drives the fan through the board microcontroller on /dev/ttyS1 with the
"Vnn" PWM-duty command (V00 = stop, V99 = full). scemd keeps running for
everything else; this loop re-asserts its duty every PERIOD seconds, which
overrides scemd's own fan writes.

Policy
  * all data disks in standby  -> disks ignored, CPU curve only
  * otherwise fan = max(disk curve(hottest disk), CPU curve)
  * fan off below DISK 35 C and CPU 50 C
  * step down only HYST C below a threshold
  * any unreadable / stale input -> FAILSAFE duty (full speed)
  * microcontroller fan-failure check disabled while running (else scemd
    beeps, notifies and re-kicks a stopped fan); re-enabled on clean stop

Byte 0x31 ('1') on ttyS1 is the microcontroller's hard power-off, so every
duty this script may send is checked to contain no '1'.
"""
import glob
import json
import os
import signal
import subprocess
import sys
import time

TTY = "/dev/ttyS1"
CPU_JSON = "/run/hwmon/cpu_temperature.json"
DISK_TEMP = "/run/synostorage/disks/{}/temperature"
LOG = "/var/log/fanctl.log"

PERIOD = 15          # s between decisions / duty writes
HYST = 3             # C below a threshold before stepping down
OFF_HYST = {"cpu": 10, "disk": 3}  # wider band for the last step to "off"
MIN_ON = 600         # s the fan keeps running once started
ASSERT_EVERY = 2     # s between re-sends of DISABLE_FANCHECK + current duty
# Microcontroller fan-failure check. While enabled, a stopped fan is reported
# as failed after ~3 min and the fan gets driven hard. scemd re-enables the
# check by itself once the system is warm, so it is re-disabled every
# ASSERT_EVERY seconds (a 5-min re-send lost that race on 2026-10-08).
# DSM itself sends 't' at shutdown.
FANCHECK_OFF = b"t"  # UART2_CMD_DISABLE_FANCHECK 0x74
FANCHECK_ON = b"u"   # UART2_CMD_ENABLE_FANCHECK  0x75
CPU_STALE = 180      # s; scemd refreshes ~60 s
DISK_STALE = 120     # s; DSM refreshes ~30 s while disks spin
FAILSAFE = 99
EXIT_DUTY = 60       # duty left behind when the controller stops
STATUS_EVERY = 600   # s between periodic status log lines

# (threshold C, duty %), ascending; below the first threshold -> 0 (off).
# Measured 2026-10-08, idle, disks spinning: fan off -> SoC levels at ~79 C,
# 5% -> ~52 C; 2-3% does not move air. scemd shuts down at CPU 95 C.
DISK_CURVE = [(40, 5), (45, 20), (50, 40), (54, 99)]
CPU_CURVE = [(83, 5), (87, 20), (90, 60), (93, 99)]

ALLOWED = {0, 5, 20, 40, 60, 99}
KICK_DUTY = 20       # brief burst so a stopped fan starts at a low duty
KICK_SECONDS = 3
for _d in ALLOWED:
    assert "1" not in "%02d" % _d, _d


def log(msg):
    line = time.strftime("%Y-%m-%dT%H:%M:%S ") + msg
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 1_000_000:
            os.replace(LOG, LOG + ".1")
        with open(LOG, "a") as f:
            f.write(line + "\n")
    except OSError:
        pass
    print(line, flush=True)


class Unevaluable(Exception):
    pass


def data_disks():
    """Internal, non-removable disks with media (excludes card-reader sdq/sdr)."""
    out = []
    for p in sorted(glob.glob("/sys/block/sd*")):
        name = os.path.basename(p)
        try:
            removable = open(p + "/removable").read().strip()
            size = int(open(p + "/size").read().strip())
        except (OSError, ValueError):
            continue
        if removable == "0" and size > 0:
            out.append(name)
    if not out:
        raise Unevaluable("no data disks found")
    return out


def in_standby(disk):
    """hdparm -C issues CHECK POWER MODE, which does not spin the disk up."""
    try:
        r = subprocess.run(["/usr/bin/hdparm", "-C", "/dev/" + disk],
                           capture_output=True, text=True, timeout=20)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unevaluable("hdparm %s: %s" % (disk, e))
    if r.returncode != 0:
        raise Unevaluable("hdparm %s rc=%d" % (disk, r.returncode))
    if "standby" in r.stdout or "sleeping" in r.stdout:
        return True
    if "active/idle" in r.stdout or "idle" in r.stdout:
        return False
    raise Unevaluable("hdparm %s: unknown state %r" % (disk, r.stdout.strip()))


def read_fresh(path, max_age):
    try:
        age = time.time() - os.stat(path).st_mtime
        data = open(path).read()
    except OSError as e:
        raise Unevaluable("%s: %s" % (path, e))
    if age > max_age:
        raise Unevaluable("%s stale (%ds)" % (path, age))
    return data


def plausible(t, what):
    if not 5 <= t <= 110:
        raise Unevaluable("%s implausible: %r" % (what, t))
    return t


def cpu_temp():
    data = read_fresh(CPU_JSON, CPU_STALE)
    try:
        vals = [int(v) for v in json.loads(data)["CPU_Temperature"].values()]
    except (ValueError, KeyError, TypeError, AttributeError) as e:
        raise Unevaluable("cpu json: %s" % e)
    if not vals:
        raise Unevaluable("cpu json empty")
    return plausible(max(vals), "cpu")


def smart_temp(disk):
    """Fallback when DSM's file is stale; -n standby never wakes the disk."""
    try:
        r = subprocess.run(["/usr/bin/smartctl", "-n", "standby", "-A",
                            "-d", "sat", "/dev/" + disk],
                           capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise Unevaluable("smartctl %s: %s" % (disk, e))
    for line in r.stdout.splitlines():
        f = line.split()
        if len(f) >= 10 and f[1] == "Temperature_Celsius":
            try:
                return int(f[9])
            except ValueError:
                break
    raise Unevaluable("smartctl %s: no temperature (rc=%d)" % (disk, r.returncode))


def disk_temp(disk):
    try:
        t = int(read_fresh(DISK_TEMP.format(disk), DISK_STALE).strip())
    except (Unevaluable, ValueError):
        t = smart_temp(disk)
    return plausible(t, disk)


def level(curve, t, prev, off_hyst=HYST):
    """Index into [off] + curve with downward hysteresis."""
    up = 0
    for i, (thr, _) in enumerate(curve, 1):
        if t >= thr:
            up = i
    if up >= prev:
        return up
    # stepping down: stay at prev until t < thr(prev) - hysteresis
    lvl = prev
    while lvl > up and t < curve[lvl - 1][0] - (off_hyst if lvl == 1 else HYST):
        lvl -= 1
    return lvl


def duty_of(curve, lvl):
    return 0 if lvl == 0 else curve[lvl - 1][1]


def apply_min_on(duty, on_since, now):
    """Hold the lowest running duty until the fan has run MIN_ON seconds.
    Returns (duty, on_since, log note)."""
    if duty > 0:
        return duty, on_since or now, ""
    if on_since and now - on_since < MIN_ON:
        return (min(ALLOWED - {0}), on_since,
                " (min-on %ds left)" % (MIN_ON - (now - on_since)))
    return 0, 0.0, ""


def write_duty(duty):
    if duty not in ALLOWED:
        raise ValueError("duty %r not allowed" % duty)
    write_tty(("V%02d" % duty).encode())


def assert_fan(duty, seconds):
    """Hold `duty` for `seconds`, re-sending DISABLE_FANCHECK + duty every
    ASSERT_EVERY s so neither scemd's speed writes nor its re-enabled fan
    check survive longer than that. Logs the first write error only."""
    end = time.time() + seconds
    failed = False
    while True:
        try:
            write_tty(FANCHECK_OFF)
            write_duty(duty)
        except OSError as e:
            if not failed:
                log("write %s failed: %s" % (TTY, e))
            failed = True
        if time.time() >= end:
            return
        time.sleep(min(ASSERT_EVERY, max(0.0, end - time.time())))


def write_tty(cmd):
    assert b"1" not in cmd  # 0x31 = hard power-off
    with open(TTY, "wb", buffering=0) as f:
        f.write(cmd)


class State:
    disk_lvl = 0
    cpu_lvl = 0


def decide(st):
    """Return (duty, description). Raises Unevaluable on bad input."""
    disks = data_disks()
    cpu = cpu_temp()
    st.cpu_lvl = level(CPU_CURVE, cpu, st.cpu_lvl, OFF_HYST["cpu"])
    cpu_duty = duty_of(CPU_CURVE, st.cpu_lvl)
    standby = {d: in_standby(d) for d in disks}
    if all(standby.values()):
        st.disk_lvl = 0
        return cpu_duty, "cpu=%d disks=standby" % cpu
    temps = {d: disk_temp(d) for d in disks if not standby[d]}
    hot = max(temps.values())
    st.disk_lvl = level(DISK_CURVE, hot, st.disk_lvl, OFF_HYST["disk"])
    disk_duty = duty_of(DISK_CURVE, st.disk_lvl)
    desc = "cpu=%d %s" % (cpu, " ".join(
        "%s=%s" % (d, "standby" if standby[d] else temps[d]) for d in disks))
    return max(cpu_duty, disk_duty), desc


def main():
    once = "--once" in sys.argv
    dry = "--dry-run" in sys.argv
    st = State()
    last = None
    last_status = 0.0
    on_since = 0.0

    def on_term(signum, _frame):
        if not dry:
            try:
                write_duty(EXIT_DUTY)
                time.sleep(1)
                write_tty(FANCHECK_ON)
            except OSError:
                pass
        log("stopping (signal %d), fan left at %d%%, fan check re-enabled"
            % (signum, EXIT_DUTY))
        sys.exit(0)

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    log("start: period=%ds disk=%s cpu=%s hyst=%d off_hyst=%s min_on=%ds "
        "failsafe=%d%s" % (PERIOD, DISK_CURVE, CPU_CURVE, HYST, OFF_HYST,
                           MIN_ON, FAILSAFE, " DRY-RUN" if dry else ""))

    while True:
        try:
            duty, desc = decide(st)
        except Unevaluable as e:
            st.disk_lvl = len(DISK_CURVE)
            st.cpu_lvl = len(CPU_CURVE)
            duty, desc = FAILSAFE, "UNEVALUABLE: %s" % e
        except Exception as e:  # never leave the fan at a stale low duty
            st.disk_lvl = len(DISK_CURVE)
            st.cpu_lvl = len(CPU_CURVE)
            duty, desc = FAILSAFE, "ERROR: %r" % e
        now = time.time()
        duty, on_since, note = apply_min_on(duty, on_since, now)
        desc += note
        kick = bool(duty and not last and duty < KICK_DUTY)
        if duty != last or now - last_status >= STATUS_EVERY or once:
            log("fan %2d%%  %s%s" % (duty, desc, " (kick-start)" if kick else ""))
            last, last_status = duty, now
        if dry:
            if once:
                return
            time.sleep(PERIOD)
            continue
        if kick:
            assert_fan(KICK_DUTY, KICK_SECONDS)
        assert_fan(duty, 0 if once else PERIOD)
        if once:
            return


if __name__ == "__main__":
    main()
