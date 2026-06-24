#!/bin/bash
# =============================================================================
# WANPTEK Web Application — Remote Installer / Updater
# =============================================================================
# Usage:  ./install_wanptek.sh <IP_ADDRESS> [MAC_ADDRESS]
# Example: ./install_wanptek.sh 10.140.1.130 22:33:44:55:66:01
#          ./install_wanptek.sh 10.140.1.130          (will prompt for MAC)
#
# Idempotent: safe to run multiple times (update mode on 2nd+ run).
# Log: /run/wanptek/wanptek.log  (tmpfs RAM — zero SD card writes)
#
# MAC address note:
#   Uses a systemd oneshot service (set-mac.service) with Before=network-pre.target
#   so the MAC is set before DHCP runs. This survives reboots and works correctly
#   with the LuckFox rk_gmac driver which ignores device-tree and U-Boot ethaddr.
#   Service is enabled via systemctl enable set-mac.service.
# =============================================================================

set -uo pipefail

REMOTE_USER="pico"
REMOTE_HOST="${1:-}"
MAC_ARG="${2:-}"
REMOTE_DIR="/home/pico"
WEBAPP_SCRIPT="wanptek_webapp.py"
PYTHON="/usr/bin/python3"
LOG_DIR="/run/wanptek"
LOG_FILE="${LOG_DIR}/wanptek.log"
AUTOSTART_CMD="${PYTHON} ${REMOTE_DIR}/${WEBAPP_SCRIPT}"
RC_MARKER="# wanptek-webapp-autostart"
APT_PACKAGES="python3 python3-flask python3-serial"

LOCAL_FILES=(
    "wanptek_webapp.py" "wanptek_controller.py"
    "static/style.css" "templates/index.html" "templates/help.html"
)

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
step()  { echo -e "\n${CYAN}━━ $* ${NC}"; }
error() { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }
die_if_fail() { local rc=$?; [[ $rc -ne 0 ]] && error "$1 (exit $rc)"; }

[[ -z "$REMOTE_HOST" ]] && error "Usage: $0 <IP> [MAC]   e.g.  $0 10.140.1.130 22:33:44:55:66:01"
for cmd in ssh scp sshpass; do
    command -v "$cmd" &>/dev/null || error "'$cmd' not found. Install: sudo apt install $cmd"
done
for f in "${LOCAL_FILES[@]}"; do
    [[ -e "$f" ]] || error "Missing local file: '$f'"
done

echo -e "\n${GREEN}WANPTEK Installer / Updater${NC}"
echo "  Target : ${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}"
echo "  Log    : ${LOG_FILE}  (RAM — no SD card writes)"
echo
read -rsp "Password for '${REMOTE_USER}@${REMOTE_HOST}' (ssh + sudo): " SSHPASS
echo
export SSHPASS

# --------------------------------------------------------------------------- #
# MAC address — accept from $2 or prompt interactively
# --------------------------------------------------------------------------- #
MAC_REGEX='^([0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}$'
ETH_MAC=""

if [[ -n "${MAC_ARG}" ]]; then
    if echo "$MAC_ARG" | grep -qE "$MAC_REGEX"; then
        ETH_MAC="$MAC_ARG"
        info "MAC address (from command line): ${ETH_MAC}"
    else
        error "Invalid MAC format: '${MAC_ARG}'  Expected: XX:XX:XX:XX:XX:XX"
    fi
else
    echo
    echo -e "${CYAN}━━ MAC address configuration ${NC}"
    echo "  The LuckFox rk_gmac driver ignores device-tree and U-Boot ethaddr."
    echo "  The only reliable method is:  ip link set eth0 address <MAC>"
    echo "  Applied immediately to the live interface AND written to rc.local"
    echo "  so the MAC is restored on every boot."
    echo
    echo "  Format:  XX:XX:XX:XX:XX:XX   (hex octets, colon-separated)"
    echo "  Example: 22:33:44:55:66:01"
    echo "  Press Enter alone to skip MAC configuration."
    echo
    while true; do
        read -rp "  MAC address (or Enter to skip): " ETH_MAC
        if [[ -z "$ETH_MAC" ]]; then
            warn "Skipping MAC — eth0 will keep its random MAC."
            break
        fi
        if echo "$ETH_MAC" | grep -qE "$MAC_REGEX"; then
            info "MAC address accepted: ${ETH_MAC}"
            break
        else
            warn "Invalid format. Use XX:XX:XX:XX:XX:XX  e.g.  22:33:44:55:66:01"
        fi
    done
fi

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"
ssh_run()  { sshpass -e ssh  $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "$@"; }
scp_one()  { sshpass -e scp  $SSH_OPTS -r "$1" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"; die_if_fail "scp '$1' failed"; info "  → $1"; }
scp_tmp()  { sshpass -e scp  $SSH_OPTS "$1" "${REMOTE_USER}@${REMOTE_HOST}:/tmp/"; die_if_fail "scp '$1' to /tmp failed"; }
run_sudo()    { sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
                    "echo '${SSHPASS}' | sudo -S -p '' bash $1"; }
run_sudo_py() { sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
                    "echo '${SSHPASS}' | sudo -S -p '' ${PYTHON} $1"; }

# --------------------------------------------------------------------------- #
step "Step 1 — Connectivity"
ssh_run "echo 'SSH OK'" || error "Cannot reach ${REMOTE_HOST}."
info "Connected."
[[ -n "${ETH_MAC}" ]] && info "Will set eth0 MAC → ${ETH_MAC}" \
                       || warn "No MAC address — eth0 keeps its random MAC."

# --------------------------------------------------------------------------- #
step "Step 2 — Install state"
FIRST_INSTALL=true
ssh_run "test -f ${REMOTE_DIR}/${WEBAPP_SCRIPT}" 2>/dev/null && FIRST_INSTALL=false || true
[[ "$FIRST_INSTALL" == "true" ]] && info "FRESH INSTALL mode." || info "UPDATE mode."

# MAC will be applied in the very last step after all work is done,
# so the SSH session is not interrupted mid-install.

# --------------------------------------------------------------------------- #
step "Step 3 — APT packages (no pip)"
cat > /tmp/wanptek_apt.sh << APTEOF
#!/bin/bash
set -e
STALE=\$(find /var/lib/apt/lists -maxdepth 1 -name '*.InRelease' -mmin +1440 2>/dev/null | wc -l)
[ "\$STALE" -gt 0 ] && { echo "[apt] Updating package list..."; apt-get update -qq; } \
                     || echo "[apt] Package list fresh — skipping update."
echo "[apt] Installing: ${APT_PACKAGES}"
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ${APT_PACKAGES} 2>&1 | grep -E '(Setting up|already installed|Unable|Err)' || true
echo "[apt] Done."
APTEOF
scp_tmp /tmp/wanptek_apt.sh
run_sudo /tmp/wanptek_apt.sh; die_if_fail "APT install failed"
ssh_run "rm -f /tmp/wanptek_apt.sh" || true

info "Verifying Python imports ..."
IMPORT_CHECK=$(ssh_run "${PYTHON} -c 'import flask, serial; print(\"OK\")' 2>&1")
[[ "$IMPORT_CHECK" == "OK" ]] || error "Import check failed: ${IMPORT_CHECK}"
info "Python imports OK."

# --------------------------------------------------------------------------- #
step "Step 3.5 — System upgrade"
# The LuckFox has no RTC battery and may boot with a wrong clock.
# 1. Set the timezone to match the installer machine.
# 2. Set the system time from the installer machine.
# Both are needed: wrong timezone causes apt to see release files as future-dated.
LOCAL_TZ=$(cat /etc/timezone 2>/dev/null || timedatectl show -p Timezone --value 2>/dev/null || echo 'Europe/Berlin')
CURRENT_DATE=$(date '+%Y-%m-%d %H:%M:%S')   # local time, not UTC
info "Setting remote timezone to: ${LOCAL_TZ}"
sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
    "echo '${SSHPASS}' | sudo -S -p '' bash -c \
    'echo ${LOCAL_TZ} > /etc/timezone && \
     ln -sf /usr/share/zoneinfo/${LOCAL_TZ} /etc/localtime && \
     date -s \"${CURRENT_DATE}\"'" \
    >/dev/null 2>&1 || warn "Timezone/clock sync failed — apt may reject release files"
info "Remote clock set to: ${CURRENT_DATE} (${LOCAL_TZ})"

cat > /tmp/wanptek_upgrade.sh << UPGEOF
#!/bin/bash
set -e
echo "[upgrade] apt update ..."
apt-get update -q
echo "[upgrade] apt upgrade ..."
DEBIAN_FRONTEND=noninteractive apt-get -y upgrade
echo "[upgrade] dist-upgrade ..."
DEBIAN_FRONTEND=noninteractive apt-get -y dist-upgrade
echo "[upgrade] autoremove ..."
DEBIAN_FRONTEND=noninteractive apt-get -y autoremove
echo "[upgrade] Done."
UPGEOF
scp_tmp /tmp/wanptek_upgrade.sh
info "Running full system upgrade (this may take a while) ..."
run_sudo /tmp/wanptek_upgrade.sh; die_if_fail "System upgrade failed"
ssh_run "rm -f /tmp/wanptek_upgrade.sh" || true
info "System upgrade done."

# --------------------------------------------------------------------------- #
step "Step 3.6 — Enable UART1 (/dev/ttyS1) via luckfox-config"
# Check if luckfox-config is present on the remote
if ssh_run "test -x /usr/bin/luckfox-config" 2>/dev/null; then
    cat > /tmp/wanptek_uart.sh << UARTEOF
#!/bin/bash
set -e
CFG='/etc/luckfox.cfg'
KEY='UART1_M1_STATUS'

# Ensure the config file exists
[ -f "\$CFG" ] || touch "\$CFG"

# Check current value
current=\$(grep -m1 "^\${KEY}=" "\$CFG" | cut -d= -f2)
if [ "\$current" = '1' ]; then
    echo "[uart] UART1_M1 already enabled in \$CFG — skipping."
    exit 0
fi

# Write the key into luckfox.cfg (same logic as luckfox_set_pin_cfg)
echo "[uart] Setting \$KEY=1 in \$CFG ..."
if grep -q "^\${KEY}=" "\$CFG"; then
    sed -i "s/^\${KEY}=.*/\${KEY}=1/" "\$CFG"
else
    echo "\${KEY}=1" >> "\$CFG"
fi

# Replay the full config (this is what rc.local already calls at boot).
# It reads luckfox.cfg and applies all DTB overlays non-interactively.
echo "[uart] Applying config via: luckfox-config load ..."
luckfox-config load
echo "[uart] Done. /dev/ttyS1 will be available after reboot."
UARTEOF
    scp_tmp /tmp/wanptek_uart.sh
    run_sudo /tmp/wanptek_uart.sh; die_if_fail "UART enable failed"
    ssh_run "rm -f /tmp/wanptek_uart.sh" || true
    UART_NEEDS_REBOOT=true
else
    warn "luckfox-config not found at /usr/bin/luckfox-config — skipping UART config."
    warn "Run 'sudo luckfox-config' manually and enable UART1_M1 to get /dev/ttyS1."
    UART_NEEDS_REBOOT=false
fi

# --------------------------------------------------------------------------- #
step "Step 4 — Directories"
ssh_run "mkdir -p ${REMOTE_DIR}/static ${REMOTE_DIR}/templates"
die_if_fail "mkdir failed"; info "Directories ready."

# --------------------------------------------------------------------------- #
step "Step 5 — Stop app, copy files"
info "Stopping any running wanptek_webapp ..."
ssh_run "pkill -f wanptek_webapp.py" || true
sleep 1
scp_one "wanptek_webapp.py"
scp_one "wanptek_controller.py"
scp_one "static"
scp_one "templates"
info "All files copied."

# --------------------------------------------------------------------------- #
step "Step 6 — Permissions"
cat > /tmp/wanptek_perms.sh << PERMEOF
#!/bin/bash
set -e
chown ${REMOTE_USER}:${REMOTE_USER} \
    ${REMOTE_DIR}/wanptek_webapp.py \
    ${REMOTE_DIR}/wanptek_controller.py
chmod 644 \
    ${REMOTE_DIR}/wanptek_webapp.py \
    ${REMOTE_DIR}/wanptek_controller.py
chown -R ${REMOTE_USER}:${REMOTE_USER} ${REMOTE_DIR}/static ${REMOTE_DIR}/templates
find ${REMOTE_DIR}/static ${REMOTE_DIR}/templates -type d -exec chmod 755 {} +
find ${REMOTE_DIR}/static ${REMOTE_DIR}/templates -type f -exec chmod 644 {} +
mkdir -p ${LOG_DIR} && chmod 755 ${LOG_DIR}
echo "[perms] Port-80 binding ..."
setcap 'cap_net_bind_service=+ep' ${PYTHON} 2>/dev/null \
    && echo "[perms] setcap OK." \
    || { echo "[perms] setcap failed, trying authbind ..."
         DEBIAN_FRONTEND=noninteractive apt-get install -y authbind -qq 2>/dev/null || true
         mkdir -p /etc/authbind/byport
         touch /etc/authbind/byport/80
         chown ${REMOTE_USER}:${REMOTE_USER} /etc/authbind/byport/80
         chmod 500 /etc/authbind/byport/80
         echo "[perms] authbind configured."; }
usermod -aG dialout ${REMOTE_USER} 2>/dev/null || true
chmod a+rw /dev/ttyS1 2>/dev/null || true
echo "[perms] Done."
PERMEOF
scp_tmp /tmp/wanptek_perms.sh
run_sudo /tmp/wanptek_perms.sh; die_if_fail "Permissions failed"
ssh_run "rm -f /tmp/wanptek_perms.sh" || true
info "Permissions done."

# --------------------------------------------------------------------------- #
step "Step 7 — Autostart (rc.local)"
# ETH_MAC is expanded by the local shell when this heredoc is written to disk.
# The Python script therefore sees the literal MAC string (or empty string)
# baked in — no remote shell quoting involved at all.
cat > /tmp/wanptek_rclocal.py << PYEOF
import os
rc      = '/etc/rc.local'
mark    = '${RC_MARKER}'
# MAC is handled by udev (10-eth0-mac.rules + set-eth0-mac.sh).
# rc.local only needs to create the log dir and launch the app.
block = [mark + '\n', 'mkdir -p ${LOG_DIR}\n',
         '${AUTOSTART_CMD} >> ${LOG_FILE} 2>&1 &\n']

lines = open(rc).readlines() if os.path.exists(rc) else ['#!/bin/bash\n']

# Remove previous wanptek block: marker + (len(block)-1) lines after it
clean, skip = [], 0
for l in lines:
    if skip:
        skip -= 1
        continue
    if l.strip() == mark:
        skip = 2  # block is always 3 lines: marker + mkdir + autostart
        continue
    clean.append(l)

# Insert before 'exit 0', or append at end
out, done = [], False
for l in clean:
    if not done and l.strip() == 'exit 0':
        out.extend(block + ['\n'])
        done = True
    out.append(l)
if not done:
    out.extend(['\n'] + block)

open(rc, 'w').writelines(out)
os.chmod(rc, 0o755)
print('rc.local OK')
PYEOF
scp_tmp /tmp/wanptek_rclocal.py
run_sudo_py /tmp/wanptek_rclocal.py; die_if_fail "rc.local update failed"
ssh_run "rm -f /tmp/wanptek_rclocal.py" || true
info "rc.local content:"
ssh_run "cat /etc/rc.local" | sed 's/^/    /'

# --------------------------------------------------------------------------- #
step "Step 8 — Starting application"
cat > /tmp/wanptek_start.sh << STARTEOF
#!/bin/bash
mkdir -p ${LOG_DIR} && chmod 755 ${LOG_DIR}
pkill -f wanptek_webapp.py 2>/dev/null || true
sleep 1
nohup ${AUTOSTART_CMD} >> ${LOG_FILE} 2>&1 &
disown
echo "Launched (PID \$!)."
STARTEOF
scp_tmp /tmp/wanptek_start.sh
run_sudo /tmp/wanptek_start.sh
ssh_run "rm -f /tmp/wanptek_start.sh" || true
sleep 3

RUNNING=$(ssh_run "pgrep -f wanptek_webapp.py | wc -l" 2>/dev/null || echo 0)
RUNNING="${RUNNING//[^0-9]/}"
if [[ "${RUNNING:-0}" -gt 0 ]]; then
    PID=$(ssh_run "pgrep -f wanptek_webapp.py | head -1")
    info "Application running (PID ${PID})."
    info "Last log lines:"
    ssh_run "tail -20 ${LOG_FILE} 2>/dev/null || echo '  (not yet written)'" | sed 's/^/    /'
else
    warn "Process did not start. Log:"
    ssh_run "cat ${LOG_FILE} 2>/dev/null || echo '  (no log)'" | sed 's/^/    /'
fi

# --------------------------------------------------------------------------- #
# Step 9 — Apply MAC address (LAST: SSH will drop, device gets new IP)
# --------------------------------------------------------------------------- #
if [[ -n "${ETH_MAC}" ]]; then
    # Resolve ip path on remote
    IP_BIN=$(ssh_run "command -v ip 2>/dev/null || which ip 2>/dev/null || echo ''")
    IP_BIN="${IP_BIN//[$'\t\r\n ']/}"
    [[ -z "$IP_BIN" ]] && { warn "'ip' not found — skipping MAC apply"; IP_BIN=skip; }

    if [[ "$IP_BIN" != skip ]]; then
        # Write the persistent systemd service
        cat > /tmp/wanptek_mac.sh << MACEOF
#!/bin/bash
set -e
SERVICE_FILE='/etc/systemd/system/set-mac.service'
echo "[mac] Writing set-mac.service ..."
cat > "\$SERVICE_FILE" << 'SVCEOF'
[Unit]
Description=Set eth0 MAC address
Before=network-pre.target
Wants=network-pre.target

[Service]
Type=oneshot
ExecStart=/bin/sh -c '${IP_BIN} link set dev eth0 down && ${IP_BIN} link set dev eth0 address ${ETH_MAC} && ${IP_BIN} link set dev eth0 up'
RemainAfterExit=yes

[Install]
WantedBy=multi-user.target
SVCEOF
chmod 644 "\$SERVICE_FILE"
systemctl daemon-reload
systemctl enable set-mac.service
echo "[mac] Service written and enabled."
MACEOF
        scp_tmp /tmp/wanptek_mac.sh
        run_sudo /tmp/wanptek_mac.sh
        ssh_run "rm -f /tmp/wanptek_mac.sh" || true

        step "Step 9 — Applying MAC address (SSH will disconnect)"
        echo
        warn "The MAC change requires bringing eth0 down briefly."
        warn "Your SSH connection WILL drop. This is expected."
        warn "The device will re-appear with MAC ${ETH_MAC}."
        warn "Reconnect with:  ssh ${REMOTE_USER}@<new-IP>"
        echo
        # Fire-and-forget: run in background on the remote so the
        # ssh session can exit cleanly before the interface goes down.
        # The 2-second sleep gives ssh time to send its exit cleanly.
        sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
            "echo '${SSHPASS}' | sudo -S -p '' nohup bash -c \
            'sleep 2; ${IP_BIN} link set dev eth0 down; \
             ${IP_BIN} link set dev eth0 address ${ETH_MAC}; \
             ${IP_BIN} link set dev eth0 up' \
            >/tmp/mac-apply.log 2>&1 &" || true
    fi
fi

MODE="$([[ "$FIRST_INSTALL" == "true" ]] && echo "Fresh install" || echo "Update")"
echo
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  ${MODE} complete!${NC}"
echo -e "${GREEN}============================================${NC}"
[[ -n "${ETH_MAC}" ]] && echo "  eth0 MAC addr  : ${ETH_MAC}  (applied after disconnect)"
echo "  Web interface : http://${REMOTE_HOST}  (IP may change — check DHCP)"
echo "  SCPI server   : telnet <new-IP> 5025"
echo "  Live log      : ssh ${REMOTE_USER}@<new-IP> tail -f ${LOG_FILE}"
echo "  Log resets on reboot (RAM) — autostart in /etc/rc.local"
echo

rm -f /tmp/wanptek_apt.sh /tmp/wanptek_perms.sh \
      /tmp/wanptek_rclocal.py /tmp/wanptek_start.sh \
      /tmp/wanptek_mac.sh 2>/dev/null || true

# --------------------------------------------------------------------------- #
# Final step — reboot
# Required for: system upgrades, UART DTB overlay, MAC systemd service.
# Fire-and-forget with a 3 s delay so this SSH session exits cleanly first.
# --------------------------------------------------------------------------- #
echo -e "${CYAN}━━ Final step — Rebooting device ${NC}"
echo
if [[ -n "${ETH_MAC}" ]]; then
    warn "MAC address ${ETH_MAC} will be active after reboot."
    warn "The device will get a new DHCP lease — check your DHCP server for the new IP."
else
    info "Device will be reachable at the same IP after reboot: ${REMOTE_HOST}"
fi
echo
info "Triggering reboot in 3 seconds — SSH session will close."
sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
    "echo '${SSHPASS}' | sudo -S -p '' nohup bash -c \
    'sleep 3 && reboot' >/dev/null 2>&1 &" || true
echo
echo -e "${GREEN}Done. Reconnect in ~30 seconds.${NC}"
[[ -n "${ETH_MAC}" ]] && \
    echo -e "${YELLOW}  ssh ${REMOTE_USER}@<new-IP>   (look up new IP from DHCP server)${NC}" || \
    echo -e "${GREEN}  ssh ${REMOTE_USER}@${REMOTE_HOST}${NC}"
echo
