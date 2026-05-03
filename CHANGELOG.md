# Changelog

All notable changes to this project are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

---

## [Unreleased]

---

## [1.0.0] — 2026-05-03

### Added

#### Core converter (`scripts/convert.py`)
- Parses all Schneider Electric / APC SNMP DDF XML files and produces a single `snmp.yml` for [prometheus/snmp_exporter](https://github.com/prometheus/snmp_exporter)
- One module per DDF file, named from the `ddfid` attribute
- `gauge` metrics for all numeric sensors (`<numSensor>`) with `scale` derived from `<mult>` / `<div>` expressions
- `EnumAsStateSet` metrics for all discrete state sensors (`<stateSensor>`) with `enum_values` mapped from `<enumMap>` labels
- Table/index support for row-based sensors (per-outlet PDU readings, per-phase power, etc.)
- Unit hints appended to `help` strings (°C, V, A, Hz, kWh, etc.)
- Built-in auth profiles: `public_v2` (SNMPv2c, community `public`) and `public_v3` (SNMPv3 noAuthNoPriv)
- Atomic output write — generates to a temp file and moves into place
- Validated against snmp_exporter v0.30.1: 778 modules, 63,101 metrics from 791 DDF files, zero parse errors

#### Module lookup index (`scripts/build_lookup.py`)
- Extracts device identification rules from all DDF files into `output/module_lookup.json`
- Three index types: exact sysObjectID matches, sysObjectID prefix/wildcard matches, vendor MIB OID-existence checks
- Output: 106 sysObjectID mappings, 38 prefix mappings, 995 OID-prefix entries across 791 modules

#### SNMP device discovery (`scripts/discover.py`)
- Probes devices via SNMP and resolves the correct snmp_exporter module using a three-tier strategy:
  1. Exact sysObjectID match (`.1.3.6.1.2.1.1.2.0`)
  2. sysObjectID prefix/wildcard match
  3. Vendor MIB OID-existence probe (fallback for devices without sysObjectID rules)
- Outputs Prometheus [http_sd](https://prometheus.io/docs/prometheus/latest/http_sd/) JSON with `__param_module` and `__param_auth` labels pre-set
- Accepts individual IPs, hostnames, CIDR ranges, or a host file
- Up to 50 concurrent SNMP probes (configurable via `--concurrency`)
- Supports custom community strings, SNMP port, timeout, retries, and arbitrary extra labels

#### Daily sync service
- `scripts/sync.sh` — orchestrates download → convert → reload in sequence; validates generated YAML before atomically replacing the live file; sends SIGHUP to snmp_exporter (detects systemd service, Docker container, or bare process automatically)
- `systemd/snmp-ddf-sync.service` — oneshot service unit with 30-minute timeout and exit code 2 treated as partial success
- `systemd/snmp-ddf-sync.timer` — daily at 03:00, `Persistent=true` (catches up if system was offline), 10-minute randomised delay
- `install.sh` — one-command installer: clones DDF downloader repo if absent, installs Python dependencies, creates `snmp-ddf-sync` system user, writes `/etc/snmp-ddf-sync/config`, installs sudoers rule for snmp_exporter reload, enables the timer

#### Pre-generated output
- `output/snmp.yml` — ready-to-use snmp_exporter config (778 modules)
- `output/module_lookup.json` — ready-to-use device discovery index

#### Documentation
- Full `README.md` covering: repo structure, requirements, usage examples, snmp_exporter deployment (binary and Docker), Prometheus configuration (static, `file_sd`, `http_sd`), authentication (v2c and v3), module reference table, metric naming, PromQL query examples, troubleshooting, http_sd integration guide, and daily sync service management

[Unreleased]: https://github.com/dl-romero/ddf-to-snmp-exporter/compare/v1.0.0...HEAD
[1.0.0]: https://github.com/dl-romero/ddf-to-snmp-exporter/releases/tag/v1.0.0
