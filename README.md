# synology-fanctl

A small fan controller for Synology DiskStations that lets the fan **stop**.
DSM never stops the fan: even "Quiet mode" keeps it spinning. This drives
the fan directly through the board's microcontroller, keeps it off while the
SoC and disks are cool, and spins it up only when it is needed.

Developed and tested on a **DS214play** (Intel Atom CE5335 "evansport",
DSM 7.1.1-42962). Other models that use the same microcontroller protocol on
`/dev/ttyS1` may work, but **check them yourself first** (see
[Porting](#porting-to-another-model)).

> **Use at your own risk.** This disables DSM's fan-failure detection and
> lets the SoC run hot (≈80 °C at idle on the DS214play). It is meant for
> old boxes whose owner prefers silence over margin. DSM's own thermal
> shutdown (CPU 95 °C, disks 61 °C) stays active.

## How it works

DSM's `scemd` daemon talks to a microcontroller over `/dev/ttyS1`, using
one-byte commands (documented in Synology's GPL `external.h`; see
[smallhacks, 2012][smallhacks]). The ones used here:

| Bytes | Meaning |
|---|---|
| `V00` … `V99` | set the fan PWM duty in percent (`V00` stops the fan) |
| `t` (0x74) | disable the microcontroller's fan-failure check |
| `u` (0x75) | enable it again |
| **`1` (0x31)** | **hard power-off.** Never send a `1` byte. |

Because of that last line, the controller only ever sends duties whose
digits contain no `1`, and asserts this before every write.

`fanctl.py` runs as a systemd service. Every 15 s it decides on a duty from:

- **SoC temperature**: `/run/hwmon/cpu_temperature.json`, written by scemd
  about every 60 s;
- **disk temperatures**: `/run/synostorage/disks/sdX/temperature`, written by
  DSM about every 30 s, with `smartctl -n standby` as a fallback;
- **disk power state**: `hdparm -C`, which does not spin a disk up.

Every 2 s it re-sends `t` and the current duty. That re-send is needed:
once the box is warm, scemd re-enables the fan check by itself. A stopped
fan is then reported as failed about 3 minutes later and gets driven hard.
That shows up as periodic strong spin-ups and "Fan stops" notifications. A
re-send every 5 minutes was too slow. With 2 s, no fan events were logged in
10 minutes at 70–81 °C.

### Default policy

| Sensor | off | 5 % | 20 % | 40 % | 60 % | 99 % |
|---|---|---|---|---|---|---|
| SoC | < 83 °C | 83 | 87 | – | 90 | 93 |
| hottest spinning disk | < 40 °C | 40 | 45 | 50 | – | 54 |

- The fan follows whichever sensor wants more.
- If all disks are in standby, only the SoC counts.
- **Stepping down:** happens 3 °C below a threshold. The last step to *off* is
  10 °C below for the SoC (off below 73 °C) and 3 °C for the disks.
- **Minimum on-time:** once started, the fan runs for at least 10 minutes,
  so it does not chatter.
- **Kick-start:** a stopped fan does not start at 5 %, so each start gets
  3 s at 20 % first.
- **Failsafe:** a missing, stale, implausible or unreadable input sets the
  fan to 99 %.
- **Clean stop** (`systemctl stop fanctl`): leaves the fan at 60 % and
  re-enables the fan check.

### Measurements (DS214play, idle, two WD Red 3 TB disks spinning)

| Fan duty | SoC after 6–15 min |
|---|---|
| 0 % (off) | levels off at ~79 °C after ~15 min |
| 2 %, 3 % | climbs like "off"; the fan does not turn |
| 5 % | steady at 52 °C (lowest duty that moves air) |
| 6–9 % | 46–52 °C |

The disks stayed at 25–31 °C throughout. On this box the SoC, not the
disks, decides when the fan has to run.

## Install

As root on the NAS. SSH must be enabled; then `sudo -i`.

```sh
# put the repo on a volume so it survives DSM updates, e.g.
cd /volume1/<share>
git clone https://github.com/fl4p/synology-fanctl.git   # or copy the files
sh synology-fanctl/install.sh
tail -f /run/fanctl.log
```

`install.sh` is idempotent. It copies `fanctl.py` to `/usr/local/bin`,
installs and enables `fanctl.service`, and (re)starts it.

**After DSM updates:** an update may remove the service. Re-run
`install.sh` automatically with **Control Panel → Task Scheduler → Create →
Triggered Task → User-defined script**: user `root`, event *Boot-up*,
command `sh /volume1/<share>/synology-fanctl/install.sh`.

**Also recommended:**
- **Beep:** **Control Panel → Hardware & Power → General → Beep Control**:
  untick the fan-failure beep.
- **Notifications:** **Control Panel → Notification → Rules**: untick
  "fan stops / resumes". This covers the moments around service restarts.

**Try it before installing:** `python3 fanctl.py --once --dry-run` reads
all sensors and prints the decision, without touching the fan.

**Uninstall:**

```sh
systemctl disable --now fanctl     # leaves fan at 60 %, fan check re-enabled
rm /etc/systemd/system/fanctl.service /usr/local/bin/fanctl.py
systemctl daemon-reload
```

Afterwards toggle **Fan Speed Mode** in DSM once, so that scemd sets its own
speed again.

## Tuning

Edit the constants at the top of `fanctl.py`:

- `CPU_CURVE` and `DISK_CURVE`: (threshold °C, duty %) pairs;
- `OFF_HYST`, `HYST`: hysteresis;
- `MIN_ON`: minimum on-time;
- `ALLOWED`: duties the controller may send. Keep every `1` digit out.

Then run `install.sh` again. `python3 tests/test_fanctl.py fanctl.py` checks
the curves, hysteresis, failsafe paths and the no-`1` rule.

`sweep.sh` measures what your box needs. Stop `fanctl` first. The script
holds a list of duties for `HOLD` seconds each and aborts a level at `ABORT`
°C. On exit it always leaves the fan at 20 %.

```sh
systemctl stop fanctl
HOLD=600 ABORT=80 sh sweep.sh 00        # where does the SoC level off with the fan off?
sh sweep.sh 09 08 07 06 05 04 03 02     # lowest duty that still holds the SoC
cat /var/log/fansweep.log
systemctl start fanctl
```

## Porting to another model

Check these before running the service on anything that is not a DS214play:

1. **Does `/dev/ttyS1` take `V` commands?** With fanctl stopped, run
   `printf V99 > /dev/ttyS1`: the fan should get louder. Then `printf V20 >
   /dev/ttyS1`. If nothing happens, your model uses a different fan path
   (ADT7490, BMC, …) and this tool will not work as-is.
2. **Do the sensor files exist?** Check `/run/hwmon/cpu_temperature.json`
   and `/run/synostorage/disks/*/temperature`. Run `--once --dry-run`.
3. **What does your SoC do with the fan off?** Use `sweep.sh` (above) and
   set `CPU_CURVE` accordingly.
4. **Watch for fan events:**
   `grep -E "fan_fail|fan_speed_handler" /var/log/scemd.log`. There should
   be none while fanctl runs.

## Things that did *not* work (DS214play, DSM 7.1.1)

- **Editing the fan table in `/usr/syno/etc/scemd.xml`** (0 %, `STOP`, `FULL`
  in the lowest Quiet band, followed by a fan-mode toggle) made no audible
  difference.
- **Adding a `DUAL_MODE_LOW_STOP` table and setting
  `fan_config_type_internal="low_stop"`** (the "low-power" fan mode of some
  models) made scemd log `No such fan config type 2`. Its fan handler and
  disk-enumeration handler then failed to start. Reverted.
- **Sending `t` only every 5 minutes:** scemd re-enables the check once the
  box is warm (see above).

## License

MIT. See [LICENSE](LICENSE).

[smallhacks]: https://smallhacks.wordpress.com/2012/04/17/working-with-synology-hardware-devsynobios-and-devttys1/
