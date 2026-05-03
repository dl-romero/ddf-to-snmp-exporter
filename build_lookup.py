#!/usr/bin/env python3
"""
Build module_lookup.json from DDF files.

This produces the index file that discover.py uses to map a device's
sysObjectID (and fallback OID probes) to the correct snmp_exporter module.
"""

import os
import re
import sys
import glob
import json
import argparse
import logging
from pathlib import Path
from xml.etree import ElementTree as ET
from collections import defaultdict

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

SYSOID_OID = ".1.3.6.1.2.1.1.2.0"


def norm_oid(oid: str) -> str:
    return oid.strip().lstrip(".")


def is_sysoid(oid: str) -> bool:
    return norm_oid(oid) == norm_oid(SYSOID_OID)


def module_id_from_ddfid(ddfid: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_]", "_", ddfid).strip("_") or "module"


def parse_ddf(filepath: str) -> dict | None:
    try:
        root = ET.parse(filepath).getroot()
    except ET.ParseError as e:
        log.warning("Parse error %s: %s", filepath, e)
        return None

    ddfid = root.get("ddfid", "")
    ddfname = root.get("ddfname", ddfid)
    if not ddfid:
        return None

    module_id = module_id_from_ddfid(ddfid)

    # Exact sysObjectID values this DDF matches on
    sysobjids: list[str] = []
    # sysObjectID prefix/wildcard patterns (e.g. "1.3.6.1.4.1.318.1.3.34.*")
    sysobjid_prefixes: list[str] = []
    # OIDs that must exist on the device (vendor MIB presence check)
    required_oids: list[str] = []

    for device in root.iter("device"):
        # sysObjectID match rules
        for match in device.findall("oidMustMatch"):
            if is_sysoid(match.get("oid", "")):
                for v in match.findall(".//value"):
                    raw = (v.text or "").strip()
                    if not raw:
                        continue
                    normed = norm_oid(raw)
                    if normed.endswith(".*"):
                        sysobjid_prefixes.append(normed[:-2])  # store prefix without .*
                    elif "*" not in normed:
                        sysobjids.append(normed)

        # Vendor MIB presence checks
        for e in device.findall("oidMustExist"):
            oid = norm_oid(e.get("oid", ""))
            if oid and oid not in required_oids:
                required_oids.append(oid)

    return {
        "module_id": module_id,
        "ddfid": ddfid,
        "name": ddfname,
        "sysobjids": list(dict.fromkeys(sysobjids)),        # dedupe, preserve order
        "sysobjid_prefixes": list(dict.fromkeys(sysobjid_prefixes)),
        "required_oids": required_oids[:5],                 # top 5 is enough for probing
    }


def build(input_dirs: list[str], output: str) -> None:
    xml_files = []
    for d in input_dirs:
        xml_files.extend(Path(d).rglob("*.xml"))
    xml_files = sorted(xml_files)
    log.info("Processing %d DDF files", len(xml_files))

    modules: dict[str, dict] = {}
    sysobjid_index: dict[str, list[str]] = defaultdict(list)   # exact OID → [module_id]
    sysobjid_prefix_index: dict[str, list[str]] = defaultdict(list)  # prefix → [module_id]
    reqoid_index: dict[str, list[str]] = defaultdict(list)      # req_oid → [module_id]

    for filepath in xml_files:
        info = parse_ddf(str(filepath))
        if not info:
            continue

        mid = info["module_id"]
        # Handle duplicate module IDs (same as convert.py)
        base = mid
        counter = 2
        while mid in modules:
            mid = f"{base}_{counter}"
            counter += 1
        info["module_id"] = mid

        modules[mid] = {
            "name": info["name"],
            "ddfid": info["ddfid"],
            "sysobjids": info["sysobjids"],
            "sysobjid_prefixes": info["sysobjid_prefixes"],
            "required_oids": info["required_oids"],
        }

        for oid in info["sysobjids"]:
            sysobjid_index[oid].append(mid)
        for prefix in info["sysobjid_prefixes"]:
            sysobjid_prefix_index[prefix].append(mid)
        for oid in info["required_oids"]:
            reqoid_index[oid].append(mid)

    # Dedupe index lists
    sysobjid_index = {k: list(dict.fromkeys(v)) for k, v in sysobjid_index.items()}
    sysobjid_prefix_index = {k: list(dict.fromkeys(v)) for k, v in sysobjid_prefix_index.items()}
    reqoid_index = {k: list(dict.fromkeys(v)) for k, v in reqoid_index.items()}

    lookup = {
        "modules": modules,
        "sysobjid_index": dict(sorted(sysobjid_index.items())),
        "sysobjid_prefix_index": dict(sorted(sysobjid_prefix_index.items())),
        "reqoid_index": dict(sorted(reqoid_index.items())),
    }

    with open(output, "w") as f:
        json.dump(lookup, f, indent=2)

    log.info(
        "Written %s: %d modules, %d sysObjectID mappings, %d OID-prefix mappings",
        output, len(modules), len(sysobjid_index), len(reqoid_index),
    )


def main():
    p = argparse.ArgumentParser(description="Build module lookup index from DDF files")
    p.add_argument("input_dirs", nargs="+")
    p.add_argument("-o", "--output", default="module_lookup.json")
    args = p.parse_args()
    build(args.input_dirs, args.output)


if __name__ == "__main__":
    main()
