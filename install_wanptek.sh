#!/bin/bash
# =============================================================================
# WANPTEK Web Application — Remote Installer / Updater
# =============================================================================
# Usage:
#   chmod +x install_wanptek.sh
#   ./install_wanptek.sh <IP_ADDRESS>
#
# Example:
#   ./install_wanptek.sh 10.140.1.42
#
# Run as many times as you like — fully idempotent:
#   • 1st run  → fresh install (packages, files, rc.local, permissions)
#   • 2nd run+ → update files, re-check packages, restart app
#
# Logging:
#   App output goes to /run/wanptek/wanptek.log — a RAM-backed tmpfs path.
#   Nothing is written to the SD card. The log resets cleanly on every reboot.
#   Read it live with:  ssh pico@<IP> tail -f /run/wanptek/wanptek.log
#
# Requirements on the LOCAL machine:
#   ssh, scp, sshpass   →   sudo apt install sshpass
# =============================================================================

set -euo pipefail

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
REMOTE_USER="pico"
REMOTE_HOST="${1:-}"
REMOTE_DIR="/home/pico"
WEBAPP_SCRIPT="wanptek_webapp.py"
PYTHON="/usr/bin/python3"

# Log goes to tmpfs (/run is always RAM on Linux — zero SD card writes)
LOG_DIR="/run/wanptek"
LOG_FILE="${LOG_DIR}/wanptek.log"

AUTOSTART_CMD="${PYTHON} ${REMOTE_DIR}/${WEBAPP_SCRIPT}"

# Exact set of files that must exist locally next to this script
LOCAL_FILES=(
    "wanptek_webapp.py"
    "wanptek_controller.py"
    "static/style.css"
    "templates/index.html"
    "templates/help.html"
)

# apt packages only — no pip
APT_PACKAGES=(
    python3
    python3-flask
    python3-serial
)

# Marker line in rc.local — used to detect / replace the autostart block
RC_MARKER="# wanptek-webapp-autostart"

# --------------------------------------------------------------------------- #
# Colour helpers
# --------------------------------------------------------------------------- #
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; CYAN='\033[0;36m'; NC='\033[0m'
info()  { echo -e "${GREEN}[+]${NC} $*"; }
warn()  { echo -e "${YELLOW}[!]${NC} $*"; }
step()  { echo -e "\n${CYAN}━━ $* ${NC}"; }
error() { echo -e "${RED}[✗]${NC} $*" >&2; exit 1; }

# --------------------------------------------------------------------------- #
# Pre-flight: local checks
# --------------------------------------------------------------------------- #
[[ -z "$REMOTE_HOST" ]] && error "Usage: $0 <IP_ADDRESS>   e.g.  $0 10.140.1.42"

for cmd in ssh scp sshpass; do
    command -v "$cmd" &>/dev/null || \
        error "'$cmd' not found locally. Install with:  sudo apt install $cmd"
done

for f in "${LOCAL_FILES[@]}"; do
    [[ -e "$f" ]] || error "Required local file not found: '$f'"
done

# --------------------------------------------------------------------------- #
# Password prompt — entered once, reused for ssh and sudo throughout
# --------------------------------------------------------------------------- #
echo -e "\n${GREEN}WANPTEK Installer / Updater${NC}"
echo    "  Target  : ${REMOTE_USER}@${REMOTE_HOST}"
echo    "  Dest    : ${REMOTE_DIR}"
echo    "  Log     : ${LOG_FILE}  (RAM — no SD card writes)"
echo
read -rsp "Password for '${REMOTE_USER}@${REMOTE_HOST}' (ssh + sudo): " SSHPASS
echo
export SSHPASS

# --------------------------------------------------------------------------- #
# ssh / scp wrappers
# --------------------------------------------------------------------------- #
SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"

ssh_run()  { sshpass -e ssh  $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "$@"; }
ssh_sudo() { sshpass -e ssh  $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
                 "echo '${SSHPASS}' | sudo -S $*"; }
scp_put()  { sshpass -e scp  $SSH_OPTS -r "$@" \
                 "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"; }

# --------------------------------------------------------------------------- #
# Step 1 — Connectivity check
# --------------------------------------------------------------------------- #
step "Step 1 — Connectivity"
ssh_run "echo 'SSH OK'" || error "Cannot connect to ${REMOTE_HOST}. Check IP and password."
info "Connected to ${REMOTE_HOST}."

# --------------------------------------------------------------------------- #
# Step 2 — Detect first install vs. update
# --------------------------------------------------------------------------- #
step "Step 2 — Install state"

FIRST_INSTALL=true
if ssh_run "test -f ${REMOTE_DIR}/${WEBAPP_SCRIPT}" 2>/dev/null; then
    FIRST_INSTALL=false
    info "Existing installation detected — running in UPDATE mode."
else
    info "No existing installation found — running in FRESH INSTALL mode."
fi

# --------------------------------------------------------------------------- #
# Step 3 — APT packages (apt only, no pip)
# --------------------------------------------------------------------------- #
step "Step 3 — APT packages"

# Only refresh the package list if it is older than 24 hours,
# so repeated update runs don't hammer the mirror unnecessarily.
LISTS_STALE=$(ssh_run \
    "find /var/lib/apt/lists -maxdepth 1 -name '*.InRelease' -mmin +1440 2>/dev/null | wc -l" \
    || echo 1)
if [[ "$LISTS_STALE" -gt 0 ]]; then
    info "Package list is stale — running apt-get update ..."
    ssh_sudo "apt-get update -qq"
else
    info "Package list is fresh — skipping apt-get update."
fi

PKG_LIST="${APT_PACKAGES[*]}"
info "Ensuring installed: ${PKG_LIST}"
ssh_sudo "DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    ${PKG_LIST} 2>&1 | grep -E '(Setting up|already installed|Unable|error)' || true"

# Hard verification — if imports fail, tell the user which package to check,
# rather than silently falling back to pip.
info "Verifying Python imports ..."
IMPORT_CHECK=$(ssh_run "${PYTHON} -c 'import flask, serial; print(\"OK\")' 2>&1")
if [[ "$IMPORT_CHECK" != "OK" ]]; then
    error "Python import check failed: ${IMPORT_CHECK}
    The apt packages could not satisfy the imports. On this distro/version
    the package names may differ. Try:
      apt-cache search flask      → look for python3-flask
      apt-cache search serial     → look for python3-serial"
fi
info "Python imports OK."

# --------------------------------------------------------------------------- #
# Step 4 — Directory structure
# --------------------------------------------------------------------------- #
step "Step 4 — Directory structure"
ssh_run "mkdir -p ${REMOTE_DIR}/static ${REMOTE_DIR}/templates"
info "Directories ready."

# --------------------------------------------------------------------------- #
# Step 5 — Stop running instance, then copy files
# --------------------------------------------------------------------------- #
step "Step 5 — Copying files"

info "Stopping any running wanptek_webapp instance ..."
ssh_run "pkill -f wanptek_webapp.py 2>/dev/null || true"
sleep 1

info "Copying wanptek_webapp.py and wanptek_controller.py ..."
scp_put "wanptek_webapp.py" "wanptek_controller.py"

info "Copying static/ ..."
scp_put "static"

info "Copying templates/ ..."
scp_put "templates"

info "Files copied."

# --------------------------------------------------------------------------- #
# Step 6 — Ownership, permissions, serial access, port-80 capability
# --------------------------------------------------------------------------- #
step "Step 6 — Permissions"

ssh_sudo "chown ${REMOTE_USER}:${REMOTE_USER} \
    ${REMOTE_DIR}/wanptek_webapp.py \
    ${REMOTE_DIR}/wanptek_controller.py"
ssh_sudo "chmod 644 \
    ${REMOTE_DIR}/wanptek_webapp.py \
    ${REMOTE_DIR}/wanptek_controller.py"

ssh_sudo "chown -R ${REMOTE_USER}:${REMOTE_USER} \
    ${REMOTE_DIR}/static \
    ${REMOTE_DIR}/templates"
ssh_sudo "find ${REMOTE_DIR}/static ${REMOTE_DIR}/templates \
    -type d -exec chmod 755 {} \;"
ssh_sudo "find ${REMOTE_DIR}/static ${REMOTE_DIR}/templates \
    -type f -exec chmod 644 {} \;"

# RAM log directory — created here AND recreated in rc.local on every boot
# because /run is a tmpfs and is empty after each reboot.
ssh_sudo "mkdir -p ${LOG_DIR}"
ssh_sudo "chown ${REMOTE_USER}:${REMOTE_USER} ${LOG_DIR}"
ssh_sudo "chmod 755 ${LOG_DIR}"

# Port 80 without running as root: prefer setcap, fall back to authbind
info "Configuring port-80 binding ..."
if ssh_sudo "setcap 'cap_net_bind_service=+ep' ${PYTHON}" 2>/dev/null; then
    info "setcap: CAP_NET_BIND_SERVICE granted to ${PYTHON}."
else
    warn "setcap not available — trying authbind ..."
    ssh_sudo "DEBIAN_FRONTEND=noninteractive apt-get install -y authbind -qq 2>/dev/null || true"
    ssh_sudo "touch /etc/authbind/byport/80"
    ssh_sudo "chown ${REMOTE_USER}:${REMOTE_USER} /etc/authbind/byport/80"
    ssh_sudo "chmod 500 /etc/authbind/byport/80"
    AUTOSTART_CMD="authbind --deep ${PYTHON} ${REMOTE_DIR}/${WEBAPP_SCRIPT}"
    warn "authbind configured; autostart command updated to use authbind."
fi

# Serial port access
info "Granting serial port access ..."
ssh_sudo "usermod -aG dialout ${REMOTE_USER} 2>/dev/null || true"
# On minimal Buildroot images the device may be root:root — open it up
ssh_sudo "chmod a+rw /dev/ttyS1 2>/dev/null || true"

info "Permissions done."

# --------------------------------------------------------------------------- #
# Step 7 — /etc/rc.local (idempotent, replaces old block on update)
# --------------------------------------------------------------------------- #
step "Step 7 — Autostart via rc.local"

# Create rc.local if absent (some systemd images omit it)
ssh_sudo "bash -c '
    if [ ! -f /etc/rc.local ]; then
        printf \"#!/bin/bash\n# rc.local\nexit 0\n\" > /etc/rc.local
        echo \"Created /etc/rc.local\"
    fi
    chmod +x /etc/rc.local
'"

# Remove any previous wanptek block before (re-)inserting,
# so running the script a second time just updates the block cleanly.
ALREADY=$(ssh_run "grep -c '${RC_MARKER}' /etc/rc.local 2>/dev/null || echo 0")
if [[ "$ALREADY" -gt 0 ]]; then
    info "Removing old autostart block (update) ..."
    # Delete the marker line and the 3 lines that follow it
    ssh_sudo "sed -i '/${RC_MARKER}/{N;N;N;d}' /etc/rc.local"
fi

# Build the 4-line block we want to insert.
# Line 1: marker (used for future detection/removal)
# Line 2: recreate the RAM log dir (tmpfs is empty after reboot)
# Line 3: fix ownership of the freshly-created log dir
# Line 4: launch the app; append stdout+stderr to the RAM log
#
# We write a small helper script to /tmp on the remote side to avoid the
# quoting nightmare of embedding newlines in a single ssh command.
ssh_run "cat > /tmp/wanptek_rc_block.sh" << EOF
#!/bin/bash
# Writes the wanptek autostart block into /etc/rc.local

BLOCK='${RC_MARKER}
mkdir -p ${LOG_DIR}
chown ${REMOTE_USER}:${REMOTE_USER} ${LOG_DIR}
${AUTOSTART_CMD} >> ${LOG_FILE} 2>&1 \&'

if grep -q '^exit 0' /etc/rc.local; then
    sed -i "/^exit 0/i \${BLOCK}" /etc/rc.local
else
    printf '%s\n' "\${BLOCK}" >> /etc/rc.local
fi
EOF

ssh_sudo "bash /tmp/wanptek_rc_block.sh"
ssh_run "rm -f /tmp/wanptek_rc_block.sh"

info "rc.local content:"
ssh_run "cat /etc/rc.local" | sed 's/^/    /'

# --------------------------------------------------------------------------- #
# Step 8 — Start the application right now
# --------------------------------------------------------------------------- #
step "Step 8 — Starting application"

ssh_sudo "mkdir -p ${LOG_DIR}"
ssh_sudo "chown ${REMOTE_USER}:${REMOTE_USER} ${LOG_DIR}"

info "Launching wanptek_webapp.py ..."
ssh_run "nohup ${AUTOSTART_CMD} >> ${LOG_FILE} 2>&1 &"
sleep 2

RUNNING=$(ssh_run "pgrep -f wanptek_webapp.py | wc -l" 2>/dev/null || echo 0)
if [[ "$RUNNING" -gt 0 ]]; then
    PID=$(ssh_run "pgrep -f wanptek_webapp.py | head -1")
    info "Application running (PID ${PID})."
    info "Last log lines:"
    ssh_run "tail -15 ${LOG_FILE} 2>/dev/null || echo '  (log not yet written)'" \
        | sed 's/^/    /'
else
    warn "Process did not start. Log output:"
    ssh_run "cat ${LOG_FILE} 2>/dev/null || echo '  (no log file found)'" \
        | sed 's/^/    /'
fi

# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #
MODE="$( [[ "$FIRST_INSTALL" == "true" ]] && echo "Fresh install" || echo "Update" )"
echo
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  ${MODE} complete!${NC}"
echo -e "${GREEN}============================================${NC}"
echo    "  Web interface : http://${REMOTE_HOST}"
echo    "  SCPI server   : telnet ${REMOTE_HOST} 5050"
echo
echo    "  Live log (RAM — no SD card writes):"
echo    "    ssh ${REMOTE_USER}@${REMOTE_HOST} tail -f ${LOG_FILE}"
echo
echo    "  The log is reset on every reboot — this is by design."
echo    "  Autostart is configured in /etc/rc.local."
echo
