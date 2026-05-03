#!/usr/bin/env python3
"""
SNMP device discovery tool — resolves each device's correct snmp_exporter module
and outputs Prometheus http_sd JSON.

Resolution order per device:
  1. Exact sysObjectID match        (fastest, most specific)
  2. sysObjectID prefix match       (e.g. wildcard patterns like 318.1.3.34.*)
  3. Vendor MIB OID-existence probe (fallback for devices with no sysOID rules)

Usage:
    python3 discover.py 192.168.1.10 192.168.1.20 --community public
    python3 discover.py --file hosts.txt --community mystring --output sd.json
    python3 discover.py 10.0.0.0/24 --community public     # CIDR sweep
"""

import asyncio
import ipaddress
import json
import logging
import argparse
import sys
from pathlib import Path
from typing import Any

try:
    from pysnmp.hlapi.asyncio import (
        SnmpEngine, CommunityData, UdpTransportTarget,
        ContextData, ObjectType, ObjectIdentity, get_cmd,
    )
except ImportError:
    print("ERROR: pysnmp not installed. Run: pip install pysnmp", file=sys.stderr)
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger(__name__)

SYSOID_OID = "1.3.6.1.2.1.1.2.0"
SYSNAME_OID = "1.3.6.1.2.1.1.5.0"
SYSDESC_OID = "1.3.6.1.2.1.1.1.0"

DEFAULT_LOOKUP = Path(__file__).parent / "module_lookup.json"
DEFAULT_COMMUNITY = "public"
DEFAULT_PORT = 161
DEFAULT_TIMEOUT = 3    # seconds per device
DEFAULT_RETRIES = 1
DEFAULT_CONCURRENCY = 50


# ── SNMP helpers ──────────────────────────────────────────────────────────────

async def snmp_get(
    engine: SnmpEngine,
    host: str,
    oids: list[str],
    community: str = DEFAULT_COMMUNITY,
    port: int = DEFAULT_PORT,
    timeout: int = DEFAULT_TIMEOUT,
    retries: int = DEFAULT_RETRIES,
) -> dict[str, str]:
    """GET a set of OIDs from one host. Returns {oid: value_str}."""
    try:
        transport = await UdpTransportTarget.create(
            (host, port), timeout=timeout, retries=retries
        )
    except Exception:
        return {}

    var_binds = [ObjectType(ObjectIdentity(oid)) for oid in oids]
    try:
        err_ind, err_status, _err_idx, result = await get_cmd(
            engine,
            CommunityData(community, mpModel=1),   # mpModel=1 → SNMPv2c
            transport,
            ContextData(),
            *var_binds,
        )
    except Exception as e:
        log.debug("%s SNMP error: %s", host, e)
        return {}

    if err_ind or err_status:
        log.debug("%s: %s %s", host, err_ind or "", err_status or "")
        return {}

    out = {}
    for binding in result:
        oid_str, val = binding
        key = str(oid_str).lstrip(".")
        # Strip trailing instance index (.0) for matching
        out[key] = str(val)
        out[key.removesuffix(".0")] = str(val)
    return out


# ── Module resolution ─────────────────────────────────────────────────────────

def load_lookup(path: Path) -> dict:
    with open(path) as f:
        return json.load(f)


def norm_oid(oid: str) -> str:
    return oid.strip().lstrip(".")


def resolve_by_sysobjid(sysobjid: str, lookup: dict) -> list[str]:
    """Return candidate module IDs matching this sysObjectID."""
    normed = norm_oid(sysobjid)

    # 1. Exact match
    candidates = lookup["sysobjid_index"].get(normed, [])
    if candidates:
        return candidates

    # 2. Prefix match (sorted longest-prefix-first for specificity)
    prefix_index = lookup.get("sysobjid_prefix_index", {})
    matches = []
    for prefix, mods in prefix_index.items():
        if normed.startswith(prefix):
            matches.append((len(prefix), mods))
    if matches:
        matches.sort(key=lambda x: x[0], reverse=True)
        return matches[0][1]

    return []


async def resolve_device(
    engine: SnmpEngine,
    host: str,
    lookup: dict,
    community: str,
    port: int,
    timeout: int,
    retries: int,
) -> dict[str, Any] | None:
    """
    Probe one host. Returns a result dict or None if unreachable.
    """
    # Phase 1: fetch sysObjectID + sysName + sysDescr in one query
    base_oids = [SYSOID_OID, SYSNAME_OID, SYSDESC_OID]
    values = await snmp_get(engine, host, base_oids, community, port, timeout, retries)
    if not values:
        log.debug("%s: unreachable or no SNMP response", host)
        return None

    raw_sysobjid = values.get(norm_oid(SYSOID_OID), "")
    sysname = values.get(norm_oid(SYSNAME_OID), "")
    sysdesc = values.get(norm_oid(SYSDESC_OID), "")

    # Phase 2: sysObjectID lookup
    sysobjid = norm_oid(raw_sysobjid)
    candidates = resolve_by_sysobjid(sysobjid, lookup)

    # Phase 3: if no sysOID match, probe vendor MIBs
    if not candidates:
        candidates = await resolve_by_oid_probe(
            engine, host, lookup, community, port, timeout, retries
        )

    module = candidates[0] if len(candidates) == 1 else (
        _pick_best(candidates, lookup) if candidates else None
    )

    return {
        "host": host,
        "sysobjid": sysobjid,
        "sysname": sysname,
        "sysdesc": sysdesc[:80],
        "module": module,
        "candidates": candidates,
    }


async def resolve_by_oid_probe(
    engine: SnmpEngine,
    host: str,
    lookup: dict,
    community: str,
    port: int,
    timeout: int,
    retries: int,
) -> list[str]:
    """
    Probe a set of discriminating OIDs to identify the device when
    sysObjectID lookup fails. Returns matching module IDs.
    """
    reqoid_index = lookup.get("reqoid_index", {})
    if not reqoid_index:
        return []

    # Pick the top N most-discriminating OIDs (fewest module candidates)
    probe_oids = sorted(
        reqoid_index.keys(),
        key=lambda o: len(reqoid_index[o])
    )[:30]

    values = await snmp_get(engine, host, probe_oids, community, port, timeout, retries)
    if not values:
        return []

    # Find modules whose required_oid appears in the response
    matches: dict[str, int] = {}
    for oid in probe_oids:
        normed = norm_oid(oid)
        # OID exists if it appears in any form in the values
        exists = any(v.startswith(normed) or normed.startswith(v) for v in values)
        if exists:
            for mid in reqoid_index.get(oid, []):
                matches[mid] = matches.get(mid, 0) + 1

    if not matches:
        return []

    # Return modules sorted by match score (highest first)
    return [m for m, _ in sorted(matches.items(), key=lambda x: x[1], reverse=True)]


def _pick_best(candidates: list[str], lookup: dict) -> str | None:
    """Pick the module with the most identification rules (most specific)."""
    if not candidates:
        return None
    modules = lookup["modules"]
    def specificity(mid):
        m = modules.get(mid, {})
        return len(m.get("sysobjids", [])) + len(m.get("required_oids", []))
    return max(candidates, key=specificity)


# ── http_sd output ────────────────────────────────────────────────────────────

def results_to_http_sd(
    results: list[dict],
    auth: str,
    port: int,
    extra_labels: dict[str, str],
) -> list[dict]:
    """Convert discovery results to Prometheus http_sd targets format."""
    targets = []
    for r in results:
        if not r or not r.get("module"):
            continue
        labels = {
            "__param_module": r["module"],
            "__param_auth": auth,
            "sysobjid": r["sysobjid"],
        }
        if r.get("sysname"):
            labels["sysname"] = r["sysname"]
        labels.update(extra_labels)

        targets.append({
            "targets": [f"{r['host']}:{port}"],
            "labels": labels,
        })
    return targets


# ── IP expansion ──────────────────────────────────────────────────────────────

def expand_targets(raw: list[str]) -> list[str]:
    """Expand CIDR ranges and plain IPs/hostnames."""
    hosts = []
    for item in raw:
        try:
            network = ipaddress.ip_network(item, strict=False)
            if network.num_addresses == 1:
                hosts.append(str(network.network_address))
            else:
                hosts.extend(str(h) for h in network.hosts())
        except ValueError:
            hosts.append(item)  # treat as hostname
    return hosts


# ── Main ──────────────────────────────────────────────────────────────────────

async def run(args) -> None:
    if not Path(args.lookup).exists():
        log.error(
            "Lookup file not found: %s\n"
            "Generate it with: python3 build_lookup.py <ddf_dir>",
            args.lookup,
        )
        sys.exit(1)

    lookup = load_lookup(args.lookup)
    log.info("Loaded lookup: %d modules", len(lookup["modules"]))

    # Collect target IPs
    raw_targets = list(args.targets)
    if args.file:
        with open(args.file) as f:
            raw_targets.extend(line.strip() for line in f if line.strip() and not line.startswith("#"))

    if not raw_targets:
        log.error("No targets specified. Use positional args or --file.")
        sys.exit(1)

    targets = expand_targets(raw_targets)
    log.info("Probing %d hosts (concurrency=%d)", len(targets), args.concurrency)

    engine = SnmpEngine()
    semaphore = asyncio.Semaphore(args.concurrency)

    async def bounded_resolve(host):
        async with semaphore:
            return await resolve_device(
                engine, host, lookup,
                args.community, args.port,
                args.timeout, args.retries,
            )

    results = await asyncio.gather(*[bounded_resolve(t) for t in targets])

    reachable = [r for r in results if r is not None]
    matched = [r for r in reachable if r.get("module")]
    unmatched = [r for r in reachable if not r.get("module")]

    log.info(
        "Results: %d reachable, %d matched, %d unmatched, %d unreachable",
        len(reachable), len(matched), len(unmatched),
        len(targets) - len(reachable),
    )

    if unmatched and not args.quiet:
        log.warning("Could not identify module for:")
        for r in unmatched:
            log.warning("  %s  sysOID=%s  sysName=%s", r["host"], r["sysobjid"], r["sysname"])

    extra_labels = {}
    for label in args.label:
        if "=" in label:
            k, v = label.split("=", 1)
            extra_labels[k] = v

    sd_output = results_to_http_sd(matched, args.auth, args.snmp_port, extra_labels)

    output_json = json.dumps(sd_output, indent=2)

    if args.output:
        with open(args.output, "w") as f:
            f.write(output_json)
        log.info("Written: %s (%d targets)", args.output, len(sd_output))
    else:
        print(output_json)


def main():
    p = argparse.ArgumentParser(
        description="Probe SNMP devices and output Prometheus http_sd JSON with correct module labels",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Probe specific hosts
  python3 discover.py 192.168.1.10 192.168.1.20 --community public

  # Probe a /24 subnet
  python3 discover.py 10.0.1.0/24 --community mystring --output /etc/prometheus/sd/snmp.json

  # Read hosts from a file, add extra labels
  python3 discover.py --file hosts.txt --label datacenter=dc1 --label env=prod

  # Specify output SNMP auth profile for prometheus params
  python3 discover.py 10.0.0.10 --auth public_v2
        """,
    )
    p.add_argument("targets", nargs="*", help="IP addresses, hostnames, or CIDR ranges to probe")
    p.add_argument("--file", "-f", help="File with one IP/hostname/CIDR per line")
    p.add_argument("--lookup", default=str(DEFAULT_LOOKUP), help="Path to module_lookup.json")
    p.add_argument("--community", "-c", default=DEFAULT_COMMUNITY, help="SNMP community string")
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="SNMP UDP port (default 161)")
    p.add_argument("--snmp-port", type=int, default=161,
                   help="SNMP port to set in http_sd target labels (default 161)")
    p.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT, help="SNMP timeout per host (seconds)")
    p.add_argument("--retries", type=int, default=DEFAULT_RETRIES, help="SNMP retries per host")
    p.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY,
                   help="Max concurrent SNMP probes (default 50)")
    p.add_argument("--auth", default="public_v2",
                   help="snmp_exporter auth profile name to set in labels (default: public_v2)")
    p.add_argument("--label", action="append", default=[],
                   metavar="KEY=VALUE", help="Extra labels to add to all targets (repeatable)")
    p.add_argument("--output", "-o", help="Write http_sd JSON to file (default: stdout)")
    p.add_argument("--quiet", "-q", action="store_true", help="Suppress unmatched device warnings")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
