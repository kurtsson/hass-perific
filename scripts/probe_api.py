#!/usr/bin/env python3
"""Probe the Enegic API and record what it actually returns.

Answers the four open questions in docs/api/enegic.md:

1. What is the real token validity window, and in what timestamp format?
2. What items does the account contain, and what are their category/type values?
3. Which `data` keys does each packet bucket carry, at which packet version?
4. Are `hwi` / `hwo` cumulative registers or interval deltas?

Question 4 is the one the sensor design is blocked on, so the same
`/getlatestpackets` call is made twice with a wait in between.

Standard library only -- no virtualenv, no install:

    python3 scripts/probe_api.py

Credentials come from `.env` in the repository root (PERIFIC_USERNAME,
PERIFIC_PASSWORD). They are read by this script and never printed.

Writes to scripts/probe_out/ (gitignored):

    raw/         verbatim responses -- contains a real token and MAC address
    redacted/    the same responses with secrets and identifiers replaced
    summary.txt  the findings, safe to paste anywhere

Exits non-zero if any call fails.
"""

from __future__ import annotations

import argparse
import json
import re
import ssl
import sys
import time
import urllib.error
import urllib.request
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BASE_URL = "https://api.enegic.com"
TIMEOUT = 15

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = REPO_ROOT / "scripts" / "probe_out"

BUCKETS = ("PhaseRealTime", "PhaseMinute", "PhaseHour", "PhaseDay")

# Keys whose value is a cumulative energy register, per docs/api/enegic.md.
# These are what the two-sample comparison is looking at.
ENERGY_KEYS = ("hwi", "hwo")


class ProbeError(Exception):
    """A probe step could not complete."""


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def load_env(path: Path) -> dict[str, str]:
    """Parse a dotenv file. Deliberately minimal: KEY=value, # comments."""
    if not path.is_file():
        raise ProbeError(f"no {path.name} found at {path.parent}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


def request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """Call the API and return the decoded JSON body."""
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if token:
        headers["X-Authorization"] = token

    # aiohttp sends Content-Length: 0 for a bodyless PUT; b"" matches that.
    # A bare None makes urllib omit the header entirely, which some
    # frameworks reject on PUT.
    data = json.dumps(body).encode() if body is not None else b""

    req = urllib.request.Request(
        f"{BASE_URL}{path}",
        data=data if method in {"PUT", "POST"} else None,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            req, timeout=TIMEOUT, context=ssl.create_default_context()
        ) as response:
            payload = response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        detail = err.read().decode("utf-8", "replace")[:500]
        raise ProbeError(f"{method} {path} -> HTTP {err.code}: {detail}") from err
    except urllib.error.URLError as err:
        raise ProbeError(f"{method} {path} -> unreachable: {err.reason}") from err

    try:
        return json.loads(payload)
    except json.JSONDecodeError as err:
        raise ProbeError(
            f"{method} {path} -> response is not JSON: {payload[:200]}"
        ) from err


# --------------------------------------------------------------------------
# Redaction
# --------------------------------------------------------------------------

SECRET_KEYS = {"Token", "token", "password", "Password", "username", "Username"}
NAME_KEYS = {"Name", "SystemName"}
MAC_KEYS = {"MacAddress", "Mac", "mac"}
OPAQUE_ID_KEYS = {
    "UserId",
    "MainUserId",
    "StripeId",
    "SerialNo",
    "SerialNumber",
    "InstallationId",
}
# Keys that hold a reference to another item. ReporterId holds it as a string,
# so the int-only ItemId rule does not reach it.
ITEM_REF_KEYS = {"ReporterId", "ClusterId", "GroupId"}
ORG_KEYS = {"Domain", "ParentDomain"}

PLACEHOLDER_USER_ID = 1000001


class Redactor:
    """Rewrites secrets and account identifiers out of API responses.

    Item IDs are remapped consistently (first seen -> 10001, then 10002, ...)
    so the redacted files still relate to each other and can become test
    fixtures unchanged.

    Device names are rewritten only inside objects carrying an `ItemId`.
    `Name` also occurs throughout the account's capability list, where the
    values are API vocabulary rather than anything personal, and redacting
    those would throw away the most informative part of the response.
    """

    def __init__(self) -> None:
        self._item_ids: dict[int, int] = {}
        self._names: dict[str, str] = {}

    def item_id(self, real: int) -> int:
        return self._item_ids.setdefault(real, 10001 + len(self._item_ids))

    def name(self, real: str, key: str) -> str:
        index = len(self._names) + 1
        placeholder = (
            f"perific-device-{index}"
            if key == "SystemName"
            else f"Perific Device {index}"
        )
        return self._names.setdefault(real, placeholder)

    def apply(
        self, value: Any, key: str | None = None, *, in_item: bool = False
    ) -> Any:
        if isinstance(value, dict):
            nested = in_item or "ItemId" in value
            return {k: self.apply(v, k, in_item=nested) for k, v in value.items()}
        if isinstance(value, list):
            return [self.apply(v, key, in_item=in_item) for v in value]
        if key in SECRET_KEYS:
            return "REDACTED"
        if key in MAC_KEYS:
            return "AA:BB:CC:DD:EE:FF"
        if key in {"ItemId", "iid"} and isinstance(value, int):
            return self.item_id(value)
        if key in ITEM_REF_KEYS and value is not None:
            try:
                mapped = self.item_id(int(value))
            except TypeError, ValueError:
                return "REDACTED"
            return str(mapped) if isinstance(value, str) else mapped
        if key in OPAQUE_ID_KEYS:
            if isinstance(value, str):
                return "REDACTED"
            # Negative values are sentinels rather than identifiers: MainUserId
            # is -1 on an account with no parent.
            if isinstance(value, int) and value > 0:
                return PLACEHOLDER_USER_ID
        if key in ORG_KEYS and isinstance(value, str) and value not in {"", "None"}:
            return "Example Org"
        if key in NAME_KEYS and in_item and isinstance(value, str) and value:
            return self.name(value, key)
        return value


LEAK_PATTERNS = {
    "MAC address": r"(?:[0-9a-fA-F]{2}:){5}[0-9a-fA-F]{2}",
    "email address": r"[\w.+-]+@[\w-]+\.[\w.]+",
    "Stripe customer": r"cus_\w+",
    "GUID": r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b",
}

PLACEHOLDER_MAC = "AA:BB:CC:DD:EE:FF"


def find_leaks(text: str) -> dict[str, list[str]]:
    """Identifier-shaped strings a redacted capture should not contain."""
    found: dict[str, list[str]] = {}
    for name, pattern in LEAK_PATTERNS.items():
        hits = sorted({h for h in re.findall(pattern, text) if h != PLACEHOLDER_MAC})
        if hits:
            found[name] = hits
    return found


def scrub(text: str, secrets: list[str]) -> str:
    """Last line of defence: remove known secret strings from free text."""
    for secret in secrets:
        if len(secret) >= 4:
            text = text.replace(secret, "REDACTED")
    return text


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


class Report:
    """Collects summary lines, printing as it goes."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, line: str = "") -> None:
        print(line)
        self.lines.append(line)

    def section(self, title: str) -> None:
        self()
        self(title)
        self("-" * len(title))


def describe(value: Any) -> str:
    """One-line description of a packet field's value."""
    if isinstance(value, list):
        return f"{value!r} (list of {len(value)})"
    return repr(value)


def ts_to_local(milliseconds: int) -> str:
    return (
        datetime.fromtimestamp(milliseconds / 1000, UTC)
        .astimezone()
        .strftime("%Y-%m-%d %H:%M:%S %Z")
    )


def parse_api_time(value: str) -> datetime | None:
    """Parse the API's ISO-8601 timestamps.

    The documented samples carry seven fractional digits, which
    `fromisoformat` rejects before 3.11 and which is more precision than
    anything needs -- truncate to six.
    """
    trimmed = re.sub(r"(\.\d{6})\d+", r"\1", value.replace("Z", "+00:00"))
    try:
        return datetime.fromisoformat(trimmed)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# Probe steps
# --------------------------------------------------------------------------


def probe_token(report: Report, username: str, password: str) -> tuple[str, Any]:
    report.section("1. PUT /createtoken")

    response = request(
        "PUT", "/createtoken", body={"username": username, "password": password}
    )
    info = (response or {}).get("TokenInfo")
    if not isinstance(info, dict) or not info.get("Token"):
        raise ProbeError(f"no TokenInfo.Token in response; keys: {list(response)}")

    token = str(info["Token"])
    report(f"Response keys      : {sorted(response)}")
    report(f"TokenInfo keys     : {sorted(info)}")
    report(f"Token length       : {len(token)} chars")
    report(f"Created            : {info.get('Created')}")
    report(f"ValidTo            : {info.get('ValidTo')}")

    created = parse_api_time(str(info.get("Created", "")))
    valid_to = parse_api_time(str(info.get("ValidTo", "")))
    if created and valid_to:
        report(f"Validity window    : {(valid_to - created).days} days")
        report(f"Fractional digits  : {count_fractional_digits(info['Created'])}")
    else:
        report("Validity window    : UNPARSEABLE -- check the format above")

    return token, response


def count_fractional_digits(timestamp: str) -> int:
    match = re.search(r"\.(\d+)", timestamp)
    return len(match.group(1)) if match else 0


def probe_overview(report: Report, token: str) -> Any:
    report.section("2. GET /getaccountoverview")

    response = request("GET", "/getaccountoverview", token=token)
    items = (response or {}).get("Items")
    if not isinstance(items, list):
        raise ProbeError(f"no Items list in response; keys: {list(response or {})}")

    report(f"Top-level keys     : {sorted(response)}")
    report(f"Items              : {len(items)}")

    for index, item in enumerate(items, start=1):
        report()
        report(f"  Item {index}")
        report(f"    ItemId       : {item.get('ItemId')}")
        report(f"    Name         : {item.get('Name')!r}")
        report(f"    SystemName   : {item.get('SystemName')!r}")
        report(f"    ItemCategory : {item.get('ItemCategory')!r}")
        report(f"    ItemType     : {item.get('ItemType')!r}")
        report(f"    ItemSubType  : {item.get('ItemSubType')!r}")
        report(f"    TimeZone     : {item.get('TimeZone')!r}")
        report(f"    CreationTime : {item.get('CreationTime')!r}")
        unexpected = sorted(
            set(item)
            - {
                "ItemId",
                "Name",
                "SystemName",
                "ItemCategory",
                "ItemType",
                "ItemSubType",
                "MacAddress",
                "TimeZone",
                "CreationTime",
            }
        )
        report(f"    Other keys   : {unexpected or 'none'}")

    return response


def probe_packets(report: Report, token: str, label: str) -> Any:
    report.section(f"3. PUT /getlatestpackets ({label})")

    response = request("PUT", "/getlatestpackets", token=token)
    if not isinstance(response, list):
        raise ProbeError(f"expected a list, got {type(response).__name__}")

    report(f"Entries            : {len(response)}")

    for entry in response:
        item_id = entry.get("ItemId")
        packets = entry.get("LatestPackets") or {}
        report()
        report(f"  ItemId {item_id} -- buckets present: {sorted(packets)}")

        unknown_buckets = sorted(set(packets) - set(BUCKETS))
        if unknown_buckets:
            report(f"  UNDOCUMENTED BUCKETS: {unknown_buckets}")

        for bucket in sorted(packets):
            packet = packets[bucket] or {}
            data = packet.get("data") or {}
            report()
            report(f"    {bucket}")
            report(
                f"      pv={packet.get('pv')} it={packet.get('it')!r} "
                f"fw={packet.get('fw')!r} rssi={packet.get('rssi')}"
            )
            ts = packet.get("ts")
            if isinstance(ts, int):
                report(f"      ts={ts} ({ts_to_local(ts)}) seqno={packet.get('seqno')}")
            report(f"      data keys: {sorted(data)}")
            for key in sorted(data):
                report(f"        {key:<8} = {describe(data[key])}")

    return response


def compare_samples(report: Report, first: Any, second: Any, elapsed: float) -> None:
    report.section("4. Cumulative or interval? (the blocking question)")
    report(f"Seconds between the two calls: {elapsed:.1f}")
    report()
    report("A cumulative register grows by a small amount or not at all.")
    report("An interval value changes arbitrarily and stays in the same order")
    report("of magnitude as the interval's own consumption.")

    second_by_id = {entry.get("ItemId"): entry for entry in second}

    for entry in first:
        item_id = entry.get("ItemId")
        other = second_by_id.get(item_id)
        if other is None:
            report(f"\n  ItemId {item_id}: missing from the second sample")
            continue

        packets_a = entry.get("LatestPackets") or {}
        packets_b = other.get("LatestPackets") or {}

        for bucket in sorted(set(packets_a) & set(packets_b)):
            packet_a = packets_a[bucket] or {}
            packet_b = packets_b[bucket] or {}
            data_a = packet_a.get("data") or {}
            data_b = packet_b.get("data") or {}

            report()
            report(f"  ItemId {item_id} / {bucket}")

            ts_a, ts_b = packet_a.get("ts"), packet_b.get("ts")
            if isinstance(ts_a, int) and isinstance(ts_b, int):
                report(
                    f"    ts delta    : {(ts_b - ts_a) / 1000:.1f}s"
                    f"{'  (same packet re-served)' if ts_a == ts_b else ''}"
                )
            seq_a, seq_b = packet_a.get("seqno"), packet_b.get("seqno")
            if isinstance(seq_a, int) and isinstance(seq_b, int):
                report(f"    seqno delta : {seq_b - seq_a}")

            for key in ENERGY_KEYS:
                value_a, value_b = data_a.get(key), data_b.get(key)
                if not isinstance(value_a, (int, float)) or not isinstance(
                    value_b, (int, float)
                ):
                    report(f"    {key:<11} : absent from this bucket")
                    continue
                delta = value_b - value_a
                verdict = (
                    "unchanged"
                    if delta == 0
                    else ("INCREASED" if delta > 0 else "DECREASED -- not monotonic")
                )
                report(
                    f"    {key:<11} : {value_a} -> {value_b} "
                    f"(delta {delta:+.6g}) {verdict}"
                )

    report()
    report("Reading it: values in the tens of thousands that grow by a fraction")
    report("of a kWh confirm cumulative registers, so TOTAL_INCREASING is correct.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def write_outputs(
    out_dir: Path, captures: list[tuple[str, Any]], redactor: Redactor
) -> None:
    raw_dir = out_dir / "raw"
    redacted_dir = out_dir / "redacted"
    raw_dir.mkdir(parents=True, exist_ok=True)
    redacted_dir.mkdir(parents=True, exist_ok=True)

    for name, payload in captures:
        (raw_dir / f"{name}.json").write_text(
            json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8"
        )
        redacted = json.dumps(redactor.apply(payload), indent=2, sort_keys=True)
        (redacted_dir / f"{name}.json").write_text(redacted, encoding="utf-8")

        leaks = find_leaks(redacted)
        if leaks:
            print(
                f"WARNING: redacted/{name}.json still contains identifier-shaped "
                f"values -- do not use as a fixture until the Redactor covers them:",
                file=sys.stderr,
            )
            for kind, hits in leaks.items():
                print(f"  {kind}: {len(hits)} occurrence(s)", file=sys.stderr)


def redact_only(out_dir: Path) -> int:
    """Rebuild redacted/ from raw/, without calling the API."""
    raw_dir = out_dir / "raw"
    captures = sorted(raw_dir.glob("*.json"))
    if not captures:
        print(f"no captures in {raw_dir}", file=sys.stderr)
        return 1

    redactor = Redactor()
    payloads = [
        (path.stem, json.loads(path.read_text(encoding="utf-8"))) for path in captures
    ]
    write_outputs(out_dir, payloads, redactor)
    print(f"Re-redacted {len(payloads)} capture(s) in {out_dir / 'redacted'}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--redact-only",
        action="store_true",
        help="rebuild redacted/ from the saved raw/ captures; makes no API calls",
    )
    parser.add_argument(
        "--wait",
        type=int,
        default=60,
        metavar="SECONDS",
        help="delay between the two /getlatestpackets calls (default: 60)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=DEFAULT_OUT,
        metavar="DIR",
        help=f"output directory (default: {DEFAULT_OUT.relative_to(REPO_ROOT)})",
    )
    args = parser.parse_args()

    if args.redact_only:
        return redact_only(args.out)

    report = Report()
    captures: list[tuple[str, Any]] = []
    redactor = Redactor()
    secrets: list[str] = []

    try:
        env = load_env(REPO_ROOT / ".env")
        username = env.get("PERIFIC_USERNAME", "")
        password = env.get("PERIFIC_PASSWORD", "")
        if not username or not password:
            raise ProbeError(".env must define PERIFIC_USERNAME and PERIFIC_PASSWORD")
        secrets += [password, username]

        report("Enegic API probe")
        report(f"Run at             : {datetime.now().astimezone().isoformat()}")
        report(f"Base URL           : {BASE_URL}")

        token, token_response = probe_token(report, username, password)
        secrets.append(token)
        captures.append(("01-createtoken", token_response))

        captures.append(("02-getaccountoverview", probe_overview(report, token)))

        first = probe_packets(report, token, "sample 1")
        captures.append(("03-getlatestpackets-t0", first))

        report()
        report(f"Waiting {args.wait}s before the second sample...")
        started = time.monotonic()
        time.sleep(args.wait)
        second = probe_packets(report, token, f"sample 2, +{args.wait}s")
        elapsed = time.monotonic() - started
        captures.append(("04-getlatestpackets-t1", second))

        compare_samples(report, first, second, elapsed)

    except ProbeError as err:
        print(f"\nPROBE FAILED: {scrub(str(err), secrets)}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    finally:
        if captures:
            write_outputs(args.out, captures, redactor)
            summary = scrub("\n".join(report.lines) + "\n", secrets)
            args.out.mkdir(parents=True, exist_ok=True)
            (args.out / "summary.txt").write_text(summary, encoding="utf-8")
            print(f"\nWrote {len(captures)} capture(s) and summary.txt to {args.out}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
