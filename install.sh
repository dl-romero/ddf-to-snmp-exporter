#!/usr/bin/env bash
# install.sh — installs the snmp-ddf-sync daily service
#
# Usage:
#   sudo bash install.sh [options]
#
# Options:
#   --downloader-dir DIR   Path to Schneider-Electric_SNMP-DDF-Downloader repo
#                          (cloned from GitHub if not provided)
#   --snmp-yml PATH        Where snmp_exporter reads its snmp.yml
#                          (default: /etc/snmp_exporter/snmp.yml)
#   --python PATH          Python interpreter to use (default: python3)
#   --run-now              Run the sync immediately after installing
#   --help

set -euo pipefail

# ── Defaults ──────────────────────────────────────────────────────────────────
CONVERTER_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DOWNLOADER_DIR=""
SNMP_YML="/etc/snmp_exporter/snmp.yml"
PYTHON="$(command -v python3 || echo python3)"
RUN_NOW=0
CONFIG_DIR="/etc/snmp-ddf-sync"
SERVICE_USER="snmp-ddf-sync"
INSTALL_BIN="/usr/local/bin/snmp-ddf-sync"
SYSTEMD_DIR="/etc/systemd/system"

# ── Argument parsing ───────────────────────────────────────────────────────────
while [[ $# -gt 0 ]]; do
    case "$1" in
        --downloader-dir) DOWNLOADER_DIR="$2"; shift 2 ;;
        --snmp-yml)       SNMP_YML="$2";       shift 2 ;;
        --python)         PYTHON="$2";          shift 2 ;;
        --run-now)        RUN_NOW=1;            shift ;;
        --help|-h)
            grep '^#' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

# ── Root check ────────────────────────────────────────────────────────────────
if [[ "$EUID" -ne 0 ]]; then
    echo "ERROR: This script must be run as root (use sudo)." >&2
    exit 1
fi

log() { echo "  [install] $*"; }
ok()  { echo "  ✓ $*"; }

echo ""
echo "=== snmp-ddf-sync installer ==="
echo ""

# ── 1. Python & dependencies ──────────────────────────────────────────────────
log "Checking Python..."
if ! "$PYTHON" -c "import sys; assert sys.version_info >= (3,10)" 2>/dev/null; then
    echo "ERROR: Python 3.10+ required. Found: $($PYTHON --version 2>&1)" >&2
    exit 1
fi
ok "Python: $($PYTHON --version)"

log "Installing Python dependencies..."
"$PYTHON" -m pip install -q -r "${CONVERTER_DIR}/requirements.txt"
ok "Dependencies installed"

# ── 2. DDF downloader repo ────────────────────────────────────────────────────
if [[ -z "$DOWNLOADER_DIR" ]]; then
    DOWNLOADER_DIR="/opt/Schneider-Electric_SNMP-DDF-Downloader"
fi

if [[ ! -d "$DOWNLOADER_DIR" ]]; then
    log "Cloning DDF downloader to $DOWNLOADER_DIR ..."
    git clone https://github.com/dl-romero/Schneider-Electric_SNMP-DDF-Downloader.git "$DOWNLOADER_DIR"
    # Install downloader dependencies
    if [[ -f "${DOWNLOADER_DIR}/requirements.txt" ]]; then
        "$PYTHON" -m pip install -q -r "${DOWNLOADER_DIR}/requirements.txt"
    fi
    ok "DDF downloader cloned"
else
    log "DDF downloader already at $DOWNLOADER_DIR — pulling latest..."
    git -C "$DOWNLOADER_DIR" pull --ff-only 2>/dev/null || log "git pull skipped (local changes?)"
    ok "DDF downloader up to date"
fi

DDF_DIR="${DOWNLOADER_DIR}/ddf_files"

# ── 3. snmp.yml output directory ──────────────────────────────────────────────
SNMP_YML_DIR="$(dirname "$SNMP_YML")"
if [[ ! -d "$SNMP_YML_DIR" ]]; then
    log "Creating snmp_exporter config directory: $SNMP_YML_DIR"
    mkdir -p "$SNMP_YML_DIR"
fi
ok "snmp.yml target: $SNMP_YML"

# ── 4. Service user ───────────────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    log "Creating system user: $SERVICE_USER"
    useradd --system --no-create-home --shell /usr/sbin/nologin "$SERVICE_USER"
    ok "User created: $SERVICE_USER"
else
    ok "User exists: $SERVICE_USER"
fi

# Give the service user write access to everything it needs
chown -R "$SERVICE_USER:$SERVICE_USER" "$DOWNLOADER_DIR"
chown -R "$SERVICE_USER:$SERVICE_USER" "$CONVERTER_DIR"
chown "$SERVICE_USER:$SERVICE_USER" "$SNMP_YML_DIR"
if [[ -f "$SNMP_YML" ]]; then
    chown "$SERVICE_USER:$SERVICE_USER" "$SNMP_YML"
fi

# Allow service user to send SIGHUP to snmp_exporter via systemctl
# (if snmp_exporter runs as its own user, this sudoers entry is needed)
SUDOERS_FILE="/etc/sudoers.d/snmp-ddf-sync"
if [[ ! -f "$SUDOERS_FILE" ]]; then
    log "Writing sudoers rule for snmp_exporter reload..."
    cat > "$SUDOERS_FILE" <<EOF
# Allow snmp-ddf-sync to reload snmp_exporter without a password
${SERVICE_USER} ALL=(root) NOPASSWD: /usr/bin/systemctl kill --signal=HUP snmp_exporter
${SERVICE_USER} ALL=(root) NOPASSWD: /bin/systemctl kill --signal=HUP snmp_exporter
EOF
    chmod 440 "$SUDOERS_FILE"
    ok "sudoers rule written: $SUDOERS_FILE"
fi

# ── 5. Config file ────────────────────────────────────────────────────────────
mkdir -p "$CONFIG_DIR"
CONFIG_FILE="${CONFIG_DIR}/config"

if [[ -f "$CONFIG_FILE" ]]; then
    log "Config already exists at $CONFIG_FILE — not overwriting"
    log "Edit it manually if paths have changed"
else
    log "Writing config: $CONFIG_FILE"
    cat > "$CONFIG_FILE" <<EOF
# snmp-ddf-sync configuration
# Generated by install.sh on $(date -Iseconds)

# Directory containing ddf_scrape.py (the downloader repo)
DOWNLOADER_DIR=${DOWNLOADER_DIR}

# Directory containing convert.py, build_lookup.py (this repo)
CONVERTER_DIR=${CONVERTER_DIR}

# Directory containing DDF .xml files (relative to DOWNLOADER_DIR)
DDF_DIR=${DDF_DIR}

# Full path where snmp_exporter reads its snmp.yml
SNMP_YML=${SNMP_YML}

# Python interpreter
PYTHON=${PYTHON}
EOF
    ok "Config written: $CONFIG_FILE"
fi
chown root:root "$CONFIG_FILE"
chmod 644 "$CONFIG_FILE"

# ── 6. Install sync script ────────────────────────────────────────────────────
log "Installing sync script to $INSTALL_BIN"
cp "${CONVERTER_DIR}/sync.sh" "$INSTALL_BIN"
chmod 755 "$INSTALL_BIN"
ok "Installed: $INSTALL_BIN"

# ── 7. Install systemd units ──────────────────────────────────────────────────
log "Installing systemd units..."
cp "${CONVERTER_DIR}/systemd/snmp-ddf-sync.service" "${SYSTEMD_DIR}/"
cp "${CONVERTER_DIR}/systemd/snmp-ddf-sync.timer"   "${SYSTEMD_DIR}/"

# Patch the service User= to match the service user (already correct if default)
sed -i "s/^User=.*/User=${SERVICE_USER}/" "${SYSTEMD_DIR}/snmp-ddf-sync.service"
sed -i "s/^Group=.*/Group=${SERVICE_USER}/" "${SYSTEMD_DIR}/snmp-ddf-sync.service"

systemctl daemon-reload
systemctl enable --now snmp-ddf-sync.timer
ok "Timer enabled and started: snmp-ddf-sync.timer"

# ── 8. Optional immediate run ────────────────────────────────────────────────
if [[ "$RUN_NOW" -eq 1 ]]; then
    echo ""
    log "Running initial sync now (this may take a few minutes)..."
    systemctl start snmp-ddf-sync.service
    systemctl status snmp-ddf-sync.service --no-pager -l || true
fi

# ── Done ──────────────────────────────────────────────────────────────────────
echo ""
echo "=== Installation complete ==="
echo ""
echo "  Config:          $CONFIG_FILE"
echo "  Sync script:     $INSTALL_BIN"
echo "  snmp.yml output: $SNMP_YML"
echo "  Schedule:        daily at 03:00 (with up to 10min random delay)"
echo ""
echo "  Useful commands:"
echo "    sudo systemctl start snmp-ddf-sync        # run sync now"
echo "    sudo journalctl -u snmp-ddf-sync -f       # follow logs"
echo "    sudo systemctl list-timers snmp-ddf-sync  # next scheduled run"
echo "    sudo systemctl status snmp-ddf-sync.timer # timer status"
echo ""
