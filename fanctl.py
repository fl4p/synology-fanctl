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
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

TTY = "/dev/ttyS1"
CPU_JSON = "/run/hwmon/cpu_temperature.json"
DISK_TEMP = "/run/synostorage/disks/{}/temperature"
IDLE_TIME = "/sys/block/{}/device/syno_idle_time"  # s since the last disk I/O
SYNOINFO = "/etc/synoinfo.conf"
# Treat a disk as asleep this many s before DSM spins it down, so the
# smartctl fallback below can never be what touches a disk about to sleep.
STANDBY_MARGIN = 60
# tmpfs: /var/log sits on the system RAID across all disks, so any write
# there wakes them and a periodic status line would block hibernation.
LOG = "/run/fanctl.log"

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
# Read-only JSON status on the LAN (0 = off). Answered from memory and sysfs,
# so asking whether the disks sleep does not wake them, unlike an SSH or DSM
# login, which writes logs to the system RAID.
STATUS_PORT = 8765
WAKES_KEPT = 20

# (threshold C, duty %), ascending; below the first threshold -> 0 (off).
# Measured 2026-10-08, idle, disks spinning: fan off -> SoC levels at ~79 C,
# 5% -> ~52 C; 2-3% does not move air. scemd shuts down at CPU 95 C.
DISK_CURVE = [(40, 5), (45, 20), (50, 40), (54, 99)]
CPU_CURVE = [(86, 5), (89, 20), (91, 60), (93, 99)]

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


def standby_seconds():
    """DSM's HDD hibernation time (Control Panel -> Hardware & Power), in s;
    0 if hibernation is off."""
    try:
        for line in open(SYNOINFO):
            k, _, v = line.partition("=")
            if k.strip() == "standbytimer":
                return int(v.strip().strip('"')) * 60
    except (OSError, ValueError) as e:
        raise Unevaluable("%s: %s" % (SYNOINFO, e))
    return 0


def idle_seconds(disk):
    try:
        return int(open(IDLE_TIME.format(disk)).read().strip())
    except (OSError, ValueError) as e:
        raise Unevaluable("%s idle time: %s" % (disk, e))


def in_standby(disk):
    """Decided from DSM's idle counter in sysfs, never by asking the disk:
    on this kernel any ATA command to a sleeping disk, even CHECK POWER MODE
    (hdparm -C, smartctl -n standby), spins it up again (measured 2026-10-27:
    with a 15 s hdparm -C poll the disks woke ~10 s after every spin-down)."""
    timer = standby_seconds()
    idle = idle_seconds(disk)
    return timer > 0 and idle >= timer - STANDBY_MARGIN


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
    """Fallback when DSM's file is stale. Only called for a disk that
    in_standby() says is spinning: on a sleeping disk this would wake it."""
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


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(t))


class SleepTracker:
    """Remembers when disks woke, from drops of their idle counter after it
    passed the hibernation time. Sampled every PERIOD and on each status
    request, so a sleep shorter than that can be missed."""

    def __init__(self):
        self.lock = threading.Lock()
        self.prev = {}
        self.wakes = []  # (time, disk, slept s), newest last

    def sample(self, now, timer, idles):
        with self.lock:
            for d, idle in idles.items():
                p = self.prev.get(d)
                if timer > 0 and p is not None and p >= timer and idle < p:
                    self.wakes.append((now - idle, d, p - timer))
                    del self.wakes[:-WAKES_KEPT]
                self.prev[d] = idle


TRACKER = SleepTracker()
STATUS = {"fan_pct": None, "decision": None, "decided_at": None}


def sample_disks(now):
    """Returns (hibernation time s, {disk: idle s}) and feeds TRACKER."""
    timer = standby_seconds()
    idles = {d: idle_seconds(d) for d in data_disks()}
    TRACKER.sample(now, timer, idles)
    return timer, idles


def status_doc(now):
    """Disk state as DSM would see it: asleep once a disk has been idle for
    the full hibernation time. Inferred, never asked from the disk."""
    timer, idles = sample_disks(now)
    disks = {}
    for d, idle in idles.items():
        asleep = timer > 0 and idle >= timer
        disks[d] = {"state": "asleep" if asleep else "spinning", "idle_s": idle}
        if asleep:
            disks[d]["asleep_since"] = iso(now - idle + timer)
        elif timer > 0:
            disks[d]["sleeps_in_s"] = timer - idle
    with TRACKER.lock:
        wakes = [{"at": iso(t), "disk": d, "slept_s": s}
                 for t, d, s in reversed(TRACKER.wakes)]
    return {"time": iso(now), "hibernate_after_s": timer, "disks": disks,
            "wakes": wakes, "fan_pct": STATUS["fan_pct"],
            "decision": STATUS["decision"], "decided_at": STATUS["decided_at"]}


class StatusHandler(BaseHTTPRequestHandler):
    timeout = 5

    def do_GET(self):
        try:
            code, doc = 200, status_doc(time.time())
        except Exception as e:  # unevaluable is an error, never "spinning"
            code, doc = 500, {"error": str(e)}
        body = (json.dumps(doc, indent=1) + "\n").encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass  # stderr goes to the journal on the system RAID


def serve_status(port):
    try:
        srv = HTTPServer(("", port), StatusHandler)
    except OSError as e:
        log("status port %d: %s" % (port, e))
        return
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    log("status on http://:%d/" % port)


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
    if STATUS_PORT and not once:
        serve_status(STATUS_PORT)
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
        try:
            sample_disks(now)
        except Unevaluable:
            pass  # decide() has already failed safe on the same input
        STATUS.update(fan_pct=duty, decision=desc, decided_at=iso(now))
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
