#!/bin/bash
# =============================================================================
# WANPTEK Web Application — Remote Installer / Updater
# =============================================================================
# Usage:  ./install_wanptek.sh <IP_ADDRESS>
# Idempotent: safe to run multiple times (update mode on 2nd+ run).
# Log: /run/wanptek/wanptek.log  (tmpfs RAM — zero SD card writes)
# =============================================================================

set -uo pipefail

REMOTE_USER="pico"
REMOTE_HOST="${1:-}"
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

[[ -z "$REMOTE_HOST" ]] && error "Usage: $0 <IP>   e.g.  $0 10.140.1.130"
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

SSH_OPTS="-o StrictHostKeyChecking=no -o ConnectTimeout=10"
ssh_run()  { sshpass -e ssh  $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" "$@"; }
scp_one()  { sshpass -e scp  $SSH_OPTS -r "$1" "${REMOTE_USER}@${REMOTE_HOST}:${REMOTE_DIR}/"; die_if_fail "scp '$1' failed"; info "  → $1"; }
scp_tmp()  { sshpass -e scp  $SSH_OPTS "$1" "${REMOTE_USER}@${REMOTE_HOST}:/tmp/"; die_if_fail "scp '$1' to /tmp failed"; }
# Run a script file from /tmp on the remote with sudo.
# Password is fed to stdin; -S reads it, -p '' suppresses the prompt.
run_sudo() { sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
                 "echo '${SSHPASS}' | sudo -S -p '' bash $1"; }
run_sudo_py() { sshpass -e ssh $SSH_OPTS "${REMOTE_USER}@${REMOTE_HOST}" \
                    "echo '${SSHPASS}' | sudo -S -p '' ${PYTHON} $1"; }

# --------------------------------------------------------------------------- #
step "Step 1 — Connectivity"
ssh_run "echo 'SSH OK'" || error "Cannot reach ${REMOTE_HOST}."
info "Connected."

# --------------------------------------------------------------------------- #
step "Step 2 — Install state"
FIRST_INSTALL=true
ssh_run "test -f ${REMOTE_DIR}/${WEBAPP_SCRIPT}" 2>/dev/null && FIRST_INSTALL=false || true
[[ "$FIRST_INSTALL" == "true" ]] && info "FRESH INSTALL mode." || info "UPDATE mode."

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
cat > /tmp/wanptek_rclocal.py << PYEOF
import os
rc    = '/etc/rc.local'
mark  = '${RC_MARKER}'
block = [mark+'\n', 'mkdir -p ${LOG_DIR}\n',
         '${AUTOSTART_CMD} >> ${LOG_FILE} 2>&1 &\n']
lines = open(rc).readlines() if os.path.exists(rc) else ['#!/bin/bash\n']
# strip old block
clean, skip = [], 0
for l in lines:
    if skip:   skip -= 1; continue
    if l.strip() == mark: skip = 2; continue
    clean.append(l)
# insert before 'exit 0' or append
out, done = [], False
for l in clean:
    if not done and l.strip() == 'exit 0':
        out.extend(block+['\n']); done = True
    out.append(l)
if not done:
    out.extend(['\n']+block)
open(rc,'w').writelines(out)
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
# Run as root — required for /dev/ttyS1 access on Buildroot at boot time
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

MODE="$([[ "$FIRST_INSTALL" == "true" ]] && echo "Fresh install" || echo "Update")"
echo
echo -e "${GREEN}============================================${NC}"
echo -e "${GREEN}  ${MODE} complete!${NC}"
echo -e "${GREEN}============================================${NC}"
echo "  Web interface : http://${REMOTE_HOST}"
echo "  SCPI server   : telnet ${REMOTE_HOST} 5050"
echo "  Live log      : ssh ${REMOTE_USER}@${REMOTE_HOST} tail -f ${LOG_FILE}"
echo "  Log resets on reboot (RAM) — autostart in /etc/rc.local"
echo

rm -f /tmp/wanptek_apt.sh /tmp/wanptek_perms.sh \
      /tmp/wanptek_rclocal.py /tmp/wanptek_start.sh 2>/dev/null || true
