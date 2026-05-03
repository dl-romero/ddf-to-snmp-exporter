#!/usr/bin/env bash
# snmp-ddf-sync — daily DDF download + snmp.yml regeneration
#
# Called by the snmp-ddf-sync.service systemd unit.
# All output goes to journald via the service's stdout.

set -euo pipefail

CONFIG_FILE="${SNMP_DDF_CONFIG:-/etc/snmp-ddf-sync/config}"

# ── Load config ───────────────────────────────────────────────────────────────
if [[ ! -f "$CONFIG_FILE" ]]; then
    echo "ERROR: config file not found: $CONFIG_FILE" >&2
    echo "Run install.sh first, or set SNMP_DDF_CONFIG to point at your config." >&2
    exit 1
fi
# shellcheck source=/dev/null
source "$CONFIG_FILE"

# Required variables (install.sh sets all of these)
: "${DOWNLOADER_DIR:?DOWNLOADER_DIR not set in $CONFIG_FILE}"
: "${CONVERTER_DIR:?CONVERTER_DIR not set in $CONFIG_FILE}"
: "${DDF_DIR:?DDF_DIR not set in $CONFIG_FILE}"
: "${SNMP_YML:?SNMP_YML not set in $CONFIG_FILE}"
: "${PYTHON:?PYTHON not set in $CONFIG_FILE}"

LOOKUP_JSON="${CONVERTER_DIR}/output/module_lookup.json"
SNMP_YML_TMP="${SNMP_YML}.tmp"

log() { echo "[$(date -Iseconds)] $*"; }
die() { log "ERROR: $*" >&2; exit 1; }

# ── Step 1: Download new / updated DDF files ──────────────────────────────────
log "=== Step 1: Downloading DDFs ==="
DOWNLOAD_FAILED=0
if [[ -f "${DOWNLOADER_DIR}/ddf_scrape.py" ]]; then
    if ! "$PYTHON" "${DOWNLOADER_DIR}/ddf_scrape.py" 2>&1; then
        log "WARNING: DDF download encountered errors — continuing with existing files"
        DOWNLOAD_FAILED=1
    fi
else
    log "WARNING: ddf_scrape.py not found at ${DOWNLOADER_DIR}/ddf_scrape.py — skipping download"
    DOWNLOAD_FAILED=1
fi

# Abort if no DDF directory exists at all
if [[ ! -d "$DDF_DIR" ]]; then
    die "DDF directory does not exist: $DDF_DIR"
fi

DDF_COUNT=$(find "$DDF_DIR" -name "*.xml" | wc -l)
log "DDF files available: $DDF_COUNT"
if [[ "$DDF_COUNT" -eq 0 ]]; then
    die "No DDF .xml files found in $DDF_DIR — aborting"
fi

# ── Step 2: Regenerate snmp.yml ───────────────────────────────────────────────
log "=== Step 2: Regenerating snmp.yml ==="
if ! "$PYTHON" "${CONVERTER_DIR}/scripts/convert.py" "$DDF_DIR" -o "$SNMP_YML_TMP" 2>&1; then
    rm -f "$SNMP_YML_TMP"
    die "convert.py failed — keeping existing snmp.yml unchanged"
fi

# Validate the generated YAML has the expected structure
if ! "$PYTHON" - <<'EOF' 2>&1
import sys, yaml
with open(sys.argv[1]) as f:
    data = yaml.safe_load(f)
assert "modules" in data, "missing 'modules' key"
assert "auths" in data,   "missing 'auths' key"
assert len(data["modules"]) > 0, "no modules generated"
print(f"Validated: {len(data['modules'])} modules, {len(data['auths'])} auth profiles")
EOF
"$SNMP_YML_TMP"; then
    rm -f "$SNMP_YML_TMP"
    die "Generated snmp.yml failed validation — keeping existing file"
fi

# Atomic replace
mv "$SNMP_YML_TMP" "$SNMP_YML"
log "snmp.yml updated: $SNMP_YML"

# ── Step 3: Regenerate module_lookup.json ────────────────────────────────────
log "=== Step 3: Regenerating module_lookup.json ==="
if ! "$PYTHON" "${CONVERTER_DIR}/scripts/build_lookup.py" "$DDF_DIR" -o "$LOOKUP_JSON" 2>&1; then
    log "WARNING: build_lookup.py failed — module_lookup.json not updated"
fi

# ── Step 4: Reload snmp_exporter ─────────────────────────────────────────────
log "=== Step 4: Reloading snmp_exporter ==="

RELOADED=0

# systemd service
if systemctl is-active --quiet snmp_exporter 2>/dev/null; then
    systemctl kill --signal=HUP snmp_exporter
    log "Sent SIGHUP to snmp_exporter (systemd)"
    RELOADED=1
fi

# Docker container named "snmp_exporter" or "snmp-exporter"
for CONTAINER in snmp_exporter snmp-exporter; do
    if command -v docker &>/dev/null && docker inspect "$CONTAINER" &>/dev/null 2>&1; then
        docker kill --signal=HUP "$CONTAINER"
        log "Sent SIGHUP to Docker container: $CONTAINER"
        RELOADED=1
    fi
done

# Bare process fallback
if [[ "$RELOADED" -eq 0 ]]; then
    if pgrep -x snmp_exporter &>/dev/null; then
        pkill -HUP -x snmp_exporter
        log "Sent SIGHUP to snmp_exporter process"
        RELOADED=1
    fi
fi

if [[ "$RELOADED" -eq 0 ]]; then
    log "WARNING: snmp_exporter not found running — snmp.yml updated on disk but not reloaded"
fi

# ── Done ──────────────────────────────────────────────────────────────────────
log "=== Sync complete ==="
[[ "$DOWNLOAD_FAILED" -eq 1 ]] && exit 2   # partial success — timer will retry tomorrow
exit 0
