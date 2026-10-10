#!/bin/sh
# Turn off Samba's sendfile on DSM 7 so Time Machine over SMB stops failing
# with "The network disk disconnected" (see README, "Time Machine").
# Idempotent. Run as root; safe as a DSM boot-up task, which re-applies the
# setting if a DSM or SMB Service update rewrites smb.conf.
CONF=${CONF:-/etc/samba/smb.conf}
BIN=/usr/local/packages/@appstore/SMBService/usr/bin

current() { "$BIN/testparm" -s -v "$CONF" 2>/dev/null | awk -F' = ' '/^\tuse sendfile = / {print $2}'; }

[ -f "$CONF" ] || { echo "$CONF not found" >&2; exit 1; }
[ -x "$BIN/testparm" ] || { echo "$BIN/testparm not found" >&2; exit 1; }

if [ "$(current)" = "No" ]; then
    echo "use sendfile = No (already)"
    exit 0
fi

cp -p "$CONF" "$CONF.bak-$(date +%Y%m%d-%H%M%S)"
# drop any existing setting, then add ours at the top of [global]
sed -i -e '/^[[:space:]]*use sendfile[[:space:]]*=/d' \
       -e '/^\[global\]/a\	use sendfile=no' "$CONF"
"$BIN/smbcontrol" smbd reload-config

v=$(current)
echo "use sendfile = $v"
[ "$v" = "No" ]
