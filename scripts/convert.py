#!/usr/bin/env python3
"""
Convert Schneider Electric / APC SNMP DDF files to Prometheus snmp_exporter snmp.yml format.

DDF files define SNMP OIDs, sensor types, scaling factors, and enumeration mappings
for network-managed data center equipment. This tool translates those definitions
into the snmp.yml module format consumed by prometheus/snmp_exporter.
"""

import os
import re
import sys
import glob
import logging
import argparse
from pathlib import Path
from xml.etree import ElementTree as ET
from collections import defaultdict, OrderedDict

try:
    import yaml
except ImportError:
    print("ERROR: pyyaml not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

# DDF sensor types that map to numeric gauge metrics
GAUGE_TYPES = {
    "num", "amperage", "temp", "voltage", "pctofcapacity", "powerW", "powerVA",
    "powerVA/powerVAR", "humidity", "pressure", "frequency", "runhours", "timeinhrs",
    "timeinsec", "timeinmin", "timeindays", "voltageAC", "voltageDC", "fanspeed",
    "volairflow", "num/kwatthr", "num/powerfactor", "num/powerKW", "num/powerKVA",
    "num/powerKVAR", "num/kVAhr", "num/kVARhr", "airflow", "powerFactor",
    "energy", "capacity", "percentage",
}

# DDF sensor types that map to enum/state metrics
ENUM_TYPES = {"state", "devstatus"}

# Prometheus-friendly unit hints by DDF type (appended to help text)
TYPE_UNITS = {
    "temp": "°C",
    "voltage": "V",
    "voltageAC": "V AC",
    "voltageDC": "V DC",
    "amperage": "A",
    "powerW": "W",
    "powerVA": "VA",
    "frequency": "Hz",
    "humidity": "%",
    "pctofcapacity": "%",
    "pressure": "Pa",
    "fanspeed": "RPM",
    "volairflow": "CFM",
    "runhours": "hours",
    "timeinhrs": "hours",
    "timeinsec": "seconds",
    "timeinmin": "minutes",
    "timeindays": "days",
    "num/kwatthr": "kWh",
    "num/powerKW": "kW",
    "num/powerKVA": "kVA",
    "num/powerfactor": "PF",
    "num/powerKVAR": "kVAR",
    "num/kVAhr": "kVAh",
    "num/kVARhr": "kVARh",
}


def normalize_oid(oid: str) -> str:
    """Strip leading dot from OID."""
    if not oid:
        return ""
    oid = oid.strip()
    return oid.lstrip(".")


def normalize_metric_name(s: str) -> str:
    """Convert arbitrary string to a valid Prometheus metric name component."""
    # Replace XML element placeholders
    s = re.sub(r"\{[^}]*\}", "", s)
    # Replace non-alphanumeric runs with underscore
    s = re.sub(r"[^a-zA-Z0-9]+", "_", s)
    s = re.sub(r"_+", "_", s).strip("_").lower()
    if s and s[0].isdigit():
        s = "sensor_" + s
    return s or "unknown"


def elem_text_content(elem) -> str:
    """Extract all text content from an element, ignoring child element tags."""
    if elem is None:
        return ""
    parts = []
    if elem.text:
        parts.append(elem.text.strip())
    for child in elem:
        if child.tail:
            parts.append(child.tail.strip())
    text = " ".join(p for p in parts if p)
    return re.sub(r"\s+", " ", text).strip()


def find_oid_in_element(elem) -> str:
    """Find the primary OID referenced inside a value element tree."""
    if elem is None:
        return ""
    # Prefer getOid (scalar) over getRowOid (table)
    for tag in ("getOid", "getRowOid"):
        node = elem.find(f".//{tag}")
        if node is not None and node.text:
            return normalize_oid(node.text.strip())
    return ""


def extract_scale(value_elem) -> float | None:
    """
    Extract a numeric scale factor from a <value> expression.

    Handles:
      <mult><op><getOid>...</getOid></op><op>0.1</op></mult>
      <div><op><getOid>...</getOid></op><op>10</op></div>
    """
    if value_elem is None:
        return None

    mult = value_elem.find(".//mult")
    if mult is not None:
        for op in mult.findall("op"):
            t = (op.text or "").strip()
            try:
                v = float(t)
                if v != 0 and v != 1.0:
                    return v
            except ValueError:
                pass

    div_elem = value_elem.find(".//div")
    if div_elem is not None:
        for op in div_elem.findall("op"):
            t = (op.text or "").strip()
            try:
                v = float(t)
                if v != 0 and v != 1.0:
                    return 1.0 / v
            except ValueError:
                pass

    return None


def oid_walk_prefix(oid: str) -> str:
    """
    Derive the OID subtree to walk.
    For scalar OIDs ending in .0, strip the .0.
    For table OIDs, return as-is (snmp_exporter will walk the subtree).
    """
    if oid.endswith(".0"):
        return oid[:-2]
    return oid


def build_enum_map_index(root: ET.Element) -> dict:
    """
    Build a dict mapping enumMap ruleid -> {int: str} from all enumMap elements in the tree.
    Labels in enumMap are 0-indexed.
    """
    enum_maps = {}
    for em in root.iter("enumMap"):
        ruleid = em.get("ruleid", "")
        if not ruleid:
            continue
        labels = {}
        for i, label in enumerate(em.findall("label")):
            text = elem_text_content(label)
            labels[i] = text if text else str(i)
        enum_maps[ruleid] = labels
    return enum_maps


def process_ddf(filepath: str) -> tuple[str, str, dict] | None:
    """
    Parse a single DDF XML file and return (module_id, module_name, module_dict)
    or None if the file has no extractable sensor metrics.
    """
    try:
        tree = ET.parse(filepath)
        root = tree.getroot()
    except ET.ParseError as exc:
        log.warning("XML parse error in %s: %s", filepath, exc)
        return None

    ddfid = root.get("ddfid") or Path(filepath).stem
    ddfname = root.get("ddfname") or ddfid

    # Sanitize module id: snmp_exporter module names must be valid identifiers
    module_id = re.sub(r"[^a-zA-Z0-9_]", "_", ddfid).strip("_") or "module"

    enum_maps = build_enum_map_index(root)

    walk_oids: set[str] = set()
    seen_oids: set[str] = set()
    metrics: list[dict] = []

    for device in root.iter("device"):
        device_id = device.get("deviceid", "")

        # ── Numeric sensors ──────────────────────────────────────────────────
        for sensor in device.findall(".//numSensor"):
            ruleid = sensor.get("ruleid", "")
            is_table = sensor.get("index") is not None

            value_elem = sensor.find("value")
            oid = find_oid_in_element(value_elem)
            if not oid:
                continue
            if oid in seen_oids:
                continue
            seen_oids.add(oid)

            label_elem = sensor.find("label")
            label = elem_text_content(label_elem) if label_elem is not None else ruleid or oid
            label = label or ruleid or oid

            type_elem = sensor.find("type")
            ddf_type = (type_elem.text or "").strip().lower() if type_elem is not None else "num"
            unit_hint = TYPE_UNITS.get(ddf_type, "")
            help_text = f"{ddfname} - {label}"
            if unit_hint:
                help_text += f" ({unit_hint})"

            scale = extract_scale(value_elem)

            metric: dict = {
                "name": normalize_metric_name(f"{module_id}_{label}"),
                "oid": oid,
                "type": "gauge",
                "help": help_text,
            }
            if scale is not None:
                metric["scale"] = round(scale, 10)

            if is_table:
                index_oid = normalize_oid(sensor.get("index", ""))
                metric["indexes"] = [{"labelname": "index", "type": "Integer"}]
                if index_oid:
                    walk_oids.add(oid_walk_prefix(index_oid))
                else:
                    walk_oids.add(oid_walk_prefix(oid))
            else:
                walk_oids.add(oid_walk_prefix(oid))

            metrics.append(metric)

        # ── State / enum sensors ─────────────────────────────────────────────
        for sensor in device.findall(".//stateSensor"):
            ruleid = sensor.get("ruleid", "")
            is_table = sensor.get("index") is not None

            value_elem = sensor.find("value")
            oid = find_oid_in_element(value_elem)
            if not oid:
                continue
            if oid in seen_oids:
                continue
            seen_oids.add(oid)

            label_elem = sensor.find("label")
            label = elem_text_content(label_elem) if label_elem is not None else ruleid or oid
            label = label or ruleid or oid

            enum_ref_elem = sensor.find("enum")
            enum_ref = (enum_ref_elem.text or "").strip() if enum_ref_elem is not None else ""
            enum_values = enum_maps.get(enum_ref, {})

            metric: dict = {
                "name": normalize_metric_name(f"{module_id}_{label}"),
                "oid": oid,
                "type": "EnumAsStateSet",
                "help": f"{ddfname} - {label}",
            }
            if enum_values:
                metric["enum_values"] = enum_values

            if is_table:
                index_oid = normalize_oid(sensor.get("index", ""))
                metric["indexes"] = [{"labelname": "index", "type": "Integer"}]
                if index_oid:
                    walk_oids.add(oid_walk_prefix(index_oid))
                else:
                    walk_oids.add(oid_walk_prefix(oid))
            else:
                walk_oids.add(oid_walk_prefix(oid))

            metrics.append(metric)

    if not metrics:
        return None

    module = {
        "walk": sorted(walk_oids),
        "metrics": metrics,
    }
    return module_id, ddfname, module


def dedupe_module_id(base_id: str, existing: dict) -> str:
    """Return a unique module id, appending a counter if needed."""
    if base_id not in existing:
        return base_id
    counter = 2
    while f"{base_id}_{counter}" in existing:
        counter += 1
    return f"{base_id}_{counter}"


# ── Custom YAML dumper ────────────────────────────────────────────────────────

class _LiteralStr(str):
    pass


def _literal_str_representer(dumper, data):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")


def _float_representer(dumper, data):
    # Avoid scientific notation for small floats
    s = f"{data:.10g}"
    return dumper.represent_scalar("tag:yaml.org,2002:float", s)


yaml.add_representer(_LiteralStr, _literal_str_representer)
yaml.add_representer(float, _float_representer)


def build_snmp_yml(modules: dict) -> dict:
    """Assemble the top-level snmp.yml structure."""
    return {
        "auths": {
            "public_v2": {
                "community": "public",
                "security_level": "noAuthNoPriv",
                "auth_protocol": "MD5",
                "priv_protocol": "DES",
                "version": 2,
            },
            "public_v3": {
                "username": "public",
                "security_level": "noAuthNoPriv",
                "version": 3,
            },
        },
        "modules": modules,
    }


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Convert Schneider Electric DDF XML files to Prometheus snmp_exporter snmp.yml"
    )
    parser.add_argument(
        "input_dirs",
        nargs="+",
        help="One or more directories to search recursively for DDF .xml files",
    )
    parser.add_argument(
        "-o", "--output",
        default="snmp.yml",
        help="Output snmp.yml path (default: snmp.yml)",
    )
    parser.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable verbose/debug logging",
    )
    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    # Collect all XML files
    xml_files: list[str] = []
    for d in args.input_dirs:
        d = os.path.expanduser(d)
        if not os.path.isdir(d):
            log.error("Not a directory: %s", d)
            sys.exit(1)
        found = sorted(Path(d).rglob("*.xml"))
        xml_files.extend(str(p) for p in found)

    if not xml_files:
        log.error("No .xml files found in: %s", args.input_dirs)
        sys.exit(1)

    log.info("Found %d DDF files to process", len(xml_files))

    modules: dict = {}
    stats = {"processed": 0, "skipped": 0, "errors": 0, "total_metrics": 0}

    for filepath in xml_files:
        try:
            result = process_ddf(filepath)
        except Exception as exc:
            log.error("Unexpected error processing %s: %s", filepath, exc)
            stats["errors"] += 1
            continue

        if result is None:
            log.debug("Skipped (no sensors): %s", filepath)
            stats["skipped"] += 1
            continue

        module_id, ddfname, module = result
        module_id = dedupe_module_id(module_id, modules)
        modules[module_id] = module
        stats["processed"] += 1
        stats["total_metrics"] += len(module["metrics"])
        log.debug("  %s: %d metrics, %d walk OIDs", module_id, len(module["metrics"]), len(module["walk"]))

    log.info(
        "Processed: %d modules | Skipped: %d | Errors: %d | Total metrics: %d",
        stats["processed"], stats["skipped"], stats["errors"], stats["total_metrics"],
    )

    if not modules:
        log.error("No modules generated — check input directories")
        sys.exit(1)

    snmp_config = build_snmp_yml(modules)

    output_path = args.output
    with open(output_path, "w", encoding="utf-8") as f:
        yaml.dump(snmp_config, f, default_flow_style=False, allow_unicode=True, sort_keys=False, width=120)

    log.info("Written: %s", output_path)


if __name__ == "__main__":
    main()
