# DDF to snmp_exporter Converter

Converts [Schneider Electric / APC SNMP Device Definition Files (DDFs)](https://github.com/dl-romero/Schneider-Electric_SNMP-DDF-Downloader) into a `snmp.yml` configuration file ready for use with [prometheus/snmp_exporter](https://github.com/prometheus/snmp_exporter).

## What it does

Each DDF file describes the SNMP OIDs, sensor types, scaling factors, and enumeration mappings for a specific device (UPS, PDU, cooling unit, etc.). This tool parses those XML definitions and generates a `snmp.yml` with:

- One **module** per DDF file (e.g., `apc_acrd2g`, `schneiderelectric_ledxg2`)
- **Gauge metrics** for numeric sensors (temperature, voltage, power, current, etc.) with correct `scale` factors applied
- **EnumAsStateSet metrics** for discrete state sensors (unit status, power supply state, alarm states, etc.) with `enum_values` mappings
- **Walk OIDs** covering every sensor in the module
- **Table/index support** for sensors that span SNMP table rows (per-outlet PDU readings, per-phase power, etc.)
- Two built-in auth profiles: `public_v2` (SNMP v2c) and `public_v3` (SNMPv3 noAuthNoPriv)

Validated against snmp_exporter **v0.30.1** — the binary loads the generated `snmp.yml` with no errors across all 778 modules and 63,000+ metrics derived from 791 DDF files.

---

## Requirements

- Python 3.11+
- `pyyaml`

```bash
pip install -r requirements.txt
```

---

## Usage

```bash
python3 convert.py <ddf_directory> [<ddf_directory2> ...] [-o snmp.yml]
```

### Examples

```bash
# Convert all DDF files from the downloader repo
python3 convert.py /path/to/Schneider-Electric_SNMP-DDF-Downloader/ddf_files

# Specify output path
python3 convert.py ./ddf_files -o /etc/snmp_exporter/snmp.yml

# Multiple source directories
python3 convert.py ./ddf_files/provided ./ddf_files/verified ./ddf_files/unverified

# Verbose output (shows per-file processing)
python3 convert.py ./ddf_files -v
```

---

## Deploying snmp_exporter

### 1. Install snmp_exporter

**Docker (recommended):**
```bash
docker pull prom/snmp-exporter:latest
```

**Binary:**
Download the [latest release](https://github.com/prometheus/snmp_exporter/releases) for your platform.

**Homebrew** (macOS, if available):
```bash
brew install snmp_exporter
```

### 2. Place your snmp.yml

```bash
# Generate the config
python3 convert.py ./ddf_files -o /etc/snmp_exporter/snmp.yml

# Run snmp_exporter pointing at the config
snmp_exporter --config.file=/etc/snmp_exporter/snmp.yml
```

**Docker:**
```bash
docker run -d \
  --name snmp_exporter \
  -p 9116:9116 \
  -v /etc/snmp_exporter/snmp.yml:/etc/snmp_exporter/snmp.yml:ro \
  prom/snmp-exporter \
  --config.file=/etc/snmp_exporter/snmp.yml
```

snmp_exporter listens on port **9116** by default.

---

## Configuring Prometheus

### Core scrape job structure

snmp_exporter works as a **proxy**: Prometheus sends HTTP requests to snmp_exporter with target device IP and module name as query parameters. snmp_exporter performs the actual SNMP walk and returns metrics.

Add this to your `prometheus.yml`:

```yaml
scrape_configs:

  # ── SNMP Exporter proxy endpoint ──────────────────────────────────────────
  - job_name: snmp
    static_configs:
      - targets:
          # List every SNMP-managed device IP or hostname here
          - 192.168.1.10   # APC UPS
          - 192.168.1.20   # PDU
          - 192.168.1.30   # Schneider cooling unit
    metrics_path: /snmp
    params:
      # Choose the module matching the device type (see Module Reference below)
      module: [apc_acrd2g]
      # Choose the auth profile matching the device SNMP version
      auth: [public_v2]
    relabel_configs:
      # Pass the device address as the SNMP target parameter
      - source_labels: [__address__]
        target_label: __param_target
      # Replace the scrape address with the snmp_exporter address
      - target_label: __address__
        replacement: 127.0.0.1:9116   # snmp_exporter host:port
      # Preserve the original device IP as an instance label
      - source_labels: [__param_target]
        target_label: instance
```

### Scraping multiple device types

Each device type needs its own job (or use `file_sd_configs` for scale):

```yaml
scrape_configs:

  # APC / Schneider cooling units (ACRD2G)
  - job_name: snmp_apc_cooling
    static_configs:
      - targets:
          - 10.0.0.11
          - 10.0.0.12
    metrics_path: /snmp
    params:
      module: [apc_acrd2g]
      auth: [public_v2]
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - target_label: __address__
        replacement: snmp-exporter:9116
      - source_labels: [__param_target]
        target_label: instance

  # APC Smart-UPS
  - job_name: snmp_apc_ups
    static_configs:
      - targets:
          - 10.0.0.20
          - 10.0.0.21
    metrics_path: /snmp
    params:
      module: [apcsmartups]
      auth: [public_v2]
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - target_label: __address__
        replacement: snmp-exporter:9116
      - source_labels: [__param_target]
        target_label: instance

  # APC Rack PDU Advanced (NetShelter)
  - job_name: snmp_apc_pdu
    static_configs:
      - targets:
          - 10.0.0.30
    metrics_path: /snmp
    params:
      module: [apc_cpdu]
      auth: [public_v2]
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - target_label: __address__
        replacement: snmp-exporter:9116
      - source_labels: [__param_target]
        target_label: instance

  # Schneider Uniflair LE G2 cooling
  - job_name: snmp_uniflair
    static_configs:
      - targets:
          - 10.0.0.40
    metrics_path: /snmp
    params:
      module: [schneiderelectric_ledxg2]
      auth: [public_v2]
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - target_label: __address__
        replacement: snmp-exporter:9116
      - source_labels: [__param_target]
        target_label: instance
```

### File-based service discovery (recommended for large deployments)

Instead of hardcoding targets per job, use `file_sd_configs` and tag each target with the right module:

**`prometheus.yml`:**
```yaml
scrape_configs:
  - job_name: snmp_devices
    file_sd_configs:
      - files:
          - /etc/prometheus/snmp_targets/*.yml
        refresh_interval: 30s
    metrics_path: /snmp
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [module]
        target_label: __param_module
      - source_labels: [auth]
        target_label: __param_auth
      - target_label: __address__
        replacement: snmp-exporter:9116
      - source_labels: [__param_target]
        target_label: instance
```

**`/etc/prometheus/snmp_targets/datacenter.yml`:**
```yaml
- targets:
    - 10.0.0.11
  labels:
    module: apc_acrd2g
    auth: public_v2
    location: dc1
    device_type: cooling

- targets:
    - 10.0.0.20
    - 10.0.0.21
  labels:
    module: apcsmartups
    auth: public_v2
    location: dc1
    device_type: ups

- targets:
    - 10.0.0.30
  labels:
    module: apc_cpdu
    auth: public_v2
    location: dc1
    device_type: pdu

- targets:
    - 10.0.0.50
  labels:
    module: schneiderelectric_ledxg2
    auth: public_v2
    location: dc1
    device_type: cooling
```

### Authentication

**SNMP v2c (most APC/Schneider devices):**
```yaml
params:
  module: [your_module]
  auth: [public_v2]   # uses community string "public"
```

To use a custom community string, add an auth entry to `snmp.yml`:
```yaml
auths:
  my_community:
    community: mysecretcommunity
    security_level: noAuthNoPriv
    auth_protocol: MD5
    priv_protocol: DES
    version: 2
```

**SNMP v3:**
```yaml
auths:
  v3_authpriv:
    username: monitoruser
    security_level: authPriv
    auth_protocol: SHA
    auth_password: authpassword
    priv_protocol: AES
    priv_password: privpassword
    version: 3
```

Then reference it with `auth: [v3_authpriv]` in your `params`.

---

## Module Reference

Each module corresponds to a DDF file. Use the module name matching your device. Below is a selection of common devices; the full `snmp.yml` contains 778 modules.

| Module Name | Device | Vendor |
|---|---|---|
| `apcsmartups` | Smart-UPS (all models) | APC |
| `apc_acrd2g` | InRow Cooling ACRD2G | APC/Schneider |
| `apc_cpdu` | NetShelter Rack PDU Advanced | APC |
| `apc_pmm` | Power Management Module | APC |
| `apc_air_economizer` | Air Economizer | APC |
| `schneiderelectric_ledxg2` | Uniflair LE G2 Cooling | Schneider Electric |
| `uniflair_ledxg2` | Uniflair LE G2 | Uniflair/Schneider |
| `eaton_ats2` | Automatic Transfer Switch | Eaton |
| `eatonups` | Eaton UPS | Eaton |
| `socomec_ups` | Socomec UPS | Socomec |
| `alcatel` | Network Switches | Alcatel |
| `cisco` | Network Infrastructure | Cisco |
| `riello_netman_204` | Riello UPS | Riello |

To find the exact module name for your device, search the `snmp.yml` file:

```bash
grep "^[a-z]" snmp.yml | grep -i "ups\|pdu\|cooling"
```

Or list all modules:
```bash
python3 -c "
import yaml
with open('snmp.yml') as f:
    data = yaml.safe_load(f)
for name, mod in data['modules'].items():
    n = len(mod['metrics'])
    print(f'{name:40s}  {n} metrics')
" | sort
```

---

## Metric Naming

Metrics are named using the pattern:

```
<module_id>_<sensor_label>
```

All characters are lowercased and non-alphanumeric runs are replaced with underscores. Examples:

| Module | Sensor Label | Metric Name |
|---|---|---|
| `apc_acrd2g` | Unit Maximum Rack Inlet Temperature | `apc_acrd2g_unit_maximum_rack_inlet_temperature` |
| `apc_pmm` | Nominal Input Voltage | `apc_pmm_nominal_input_voltage` |
| `alcatel` | Power Supply 1 | `alcatel_power_supply_1` |

Numeric sensors have a `help` string that includes the unit (e.g., `°C`, `V`, `A`, `Hz`, `kWh`).

---

## Querying in Prometheus / Grafana

### Example queries

**Current temperature from APC cooling unit:**
```promql
apc_acrd2g_unit_maximum_rack_inlet_temperature{instance="10.0.0.11"}
```

**UPS output load (with scaling already applied by snmp_exporter):**
```promql
apcsmartups_ups_output_load{instance="10.0.0.20"}
```

**Power supply status (EnumAsStateSet — one series per state, value 0 or 1):**
```promql
# Alert when power supply is not OK
alcatel_power_supply_1{state!="OK"} == 1
```

**All sensors from a PDU outlet table:**
```promql
apc_cpdu_pdu_unit_status_active_power{instance="10.0.0.30"}
```

**Device-level energy consumption:**
```promql
sum by (instance) (schneiderelectric_ledxg2_energy_kwh)
```

---

## Troubleshooting

### snmp_exporter returns no metrics for a target

1. Confirm the device is reachable: `snmpwalk -v2c -c public <device-ip> 1.3.6.1`
2. Confirm the module name is correct for your device model
3. Check snmp_exporter logs: look for `"msg":"SNMP error"` entries
4. Try querying snmp_exporter directly:
   ```bash
   curl "http://localhost:9116/snmp?target=10.0.0.11&module=apc_acrd2g&auth=public_v2"
   ```

### Scrape timeout

Large modules with many walk OIDs can take >10 seconds. Increase the scrape timeout:

```yaml
scrape_configs:
  - job_name: snmp_cooling
    scrape_timeout: 30s   # default is 10s
    ...
```

### Wrong module for device

If metrics are empty but SNMP works, you may have the wrong module. Each DDF includes OID existence checks; if the device doesn't have those OIDs, snmp_exporter returns 0 metrics. Cross-reference the device's sysObjectID against the DDF discovery rules.

### Custom community string

The default `public_v2` auth uses community `public`. To use a different community string, add a custom auth block to `snmp.yml` and reference it in your Prometheus params.

---

## Automatic module discovery for http_sd

When building a Prometheus [HTTP service discovery](https://prometheus.io/docs/prometheus/latest/http_sd/) endpoint you need each target labeled with the correct `__param_module`. This repo provides two tools to automate that.

### How module identification works

Every DDF file contains device identification rules alongside its sensor definitions. The `discover.py` tool reads those rules and probes devices to determine their module automatically.

**Resolution order per device:**

| Step | Method | OID queried | Covers |
|---|---|---|---|
| 1 | Exact sysObjectID match | `.1.3.6.1.2.1.1.2.0` | 106 unique device fingerprints |
| 2 | sysObjectID prefix match | `.1.3.6.1.2.1.1.2.0` | Wildcard patterns (e.g. `318.1.3.34.*`) |
| 3 | Vendor MIB OID-existence probe | Various vendor OIDs | Remaining 733+ modules identified by proprietary MIB presence |

The sysObjectID (`.1.3.6.1.2.1.1.2.0`) is the SNMP equivalent of a USB vendor/product ID — every SNMP agent returns it and it uniquely identifies the device family. For devices not covered by sysObjectID rules, `discover.py` probes for the presence of vendor-specific MIB subtrees (e.g. APC's `.1.3.6.1.4.1.318.1.1.27` tree for InRow cooling units).

### Step 1: Build the lookup index

```bash
python3 build_lookup.py /path/to/ddf_files -o module_lookup.json
```

This generates `module_lookup.json` containing:
- `sysobjid_index` — sysObjectID → module name (exact matches)
- `sysobjid_prefix_index` — sysObjectID prefix → module name (wildcard matches)
- `reqoid_index` — vendor OID → candidate modules (fallback probe)

### Step 2: Run discovery against your devices

```bash
# Probe specific hosts
python3 discover.py 192.168.1.10 192.168.1.20 --community public

# Probe a subnet
python3 discover.py 10.0.1.0/24 --community public --output /etc/prometheus/sd/snmp.json

# Read from a file, add environment labels
python3 discover.py --file hosts.txt --label datacenter=dc1 --label env=prod --output snmp_sd.json

# Custom community string + SNMPv3 auth profile name
python3 discover.py 10.0.0.0/24 --community secretstring --auth my_v2_auth
```

`discover.py` runs up to 50 concurrent SNMP probes (configurable with `--concurrency`).

### Output: http_sd JSON

```json
[
  {
    "targets": ["10.0.0.11:161"],
    "labels": {
      "__param_module": "apc_acrd2g",
      "__param_auth": "public_v2",
      "sysobjid": "1.3.6.1.4.1.318.1.3.14.15",
      "sysname": "ACRD2G-Row01",
      "datacenter": "dc1"
    }
  },
  {
    "targets": ["10.0.0.20:161"],
    "labels": {
      "__param_module": "apcsmartups",
      "__param_auth": "public_v2",
      "sysobjid": "1.3.6.1.4.1.318.1.3.2.6",
      "sysname": "UPS-Rack-A01",
      "datacenter": "dc1"
    }
  }
]
```

### Step 3: Point Prometheus at the http_sd file

**Option A — file_sd (static file, refresh on re-run):**

Run `discover.py` on a schedule (cron, systemd timer) and have Prometheus watch the output file:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: snmp_devices
    file_sd_configs:
      - files:
          - /etc/prometheus/sd/snmp.json
        refresh_interval: 60s
    metrics_path: /snmp
    relabel_configs:
      # Pass device IP as SNMP target
      - source_labels: [__address__]
        target_label: __param_target
      # Use the module label discover.py set
      - source_labels: [__param_module]
        target_label: __param_module
      # Route through snmp_exporter
      - target_label: __address__
        replacement: snmp-exporter:9116
      # Keep original IP as instance label
      - source_labels: [__param_target]
        target_label: instance
```

**Option B — http_sd (live endpoint, Prometheus polls your server):**

Serve the `discover.py` output from a small HTTP endpoint that Prometheus can poll directly:

```yaml
# prometheus.yml
scrape_configs:
  - job_name: snmp_devices
    http_sd_configs:
      - url: http://your-discovery-service:8080/snmp-targets
        refresh_interval: 5m
    metrics_path: /snmp
    relabel_configs:
      - source_labels: [__address__]
        target_label: __param_target
      - source_labels: [__param_module]
        target_label: __param_module
      - target_label: __address__
        replacement: snmp-exporter:9116
      - source_labels: [__param_target]
        target_label: instance
```

A minimal HTTP SD server using `discover.py`:

```python
#!/usr/bin/env python3
"""Minimal HTTP SD server wrapping discover.py output."""
import subprocess, json
from http.server import HTTPServer, BaseHTTPRequestHandler

HOSTS_FILE = "/etc/snmp-discovery/hosts.txt"
DISCOVER_CMD = ["python3", "/opt/ddf-to-snmp-exporter/discover.py",
                "--file", HOSTS_FILE, "--community", "public"]

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/snmp-targets":
            result = subprocess.run(DISCOVER_CMD, capture_output=True, text=True)
            body = result.stdout.encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

HTTPServer(("0.0.0.0", 8080), Handler).serve_forever()
```

### Keeping discovery fresh

Run `discover.py` periodically so new devices get picked up automatically:

```bash
# crontab — re-discover every 15 minutes
*/15 * * * * python3 /opt/ddf-to-snmp-exporter/discover.py \
    --file /etc/snmp-discovery/hosts.txt \
    --community public \
    --label datacenter=dc1 \
    --output /etc/prometheus/sd/snmp.json
```

---

## How it works (technical)

DDF XML elements are mapped as follows:

| DDF Element | snmp.yml Output |
|---|---|
| `<numSensor>` with `<getOid>` | `gauge` metric at that OID |
| `<numSensor>` with `<mult><op>N</op>` | `gauge` metric with `scale: N` |
| `<numSensor>` with `<div><op>N</op>` | `gauge` metric with `scale: 1/N` |
| `<stateSensor>` with `<enumMap>` | `EnumAsStateSet` metric with `enum_values` |
| Sensor with `index` attribute | Metric with `indexes: [{labelname: index}]` and table walk |
| `<sensorSet>` / `<label>` | `help` string |
| DDF `ddfid` attribute | Module name |

Sensors from `core.xml` (discovery MIB, firmware, location) are omitted — they contain no walkable sensor OIDs.
