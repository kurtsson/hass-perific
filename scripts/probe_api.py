#!/usr/bin/env python3
"""Probe the Enegic API and record what it actually returns.

Answers the four open questions in docs/api/enegic.md:

1. What is the real token validity window, and in what timestamp format?
2. What items does the account contain, and what are their category/type values?
3. Which `data` keys does each packet bucket carry, at which packet version?
4. Are `hwi` / `hwo` cumulative registers or interval deltas?

Question 4 is the one the sensor design is blocked on, so the same
`/getlatestpackets` call is made twice with a wait in between.

`--phasedata` asks a different set, the ones the statistics backfill in
docs/plans/2026-09-18-statistics-backfill.md is blocked on: does
`POST /getphasedata` exist, what request shape does it accept, what bucket
granularity does it return, does it carry the cumulative registers, and how far
back does the account retain.

Standard library only -- no virtualenv, no install:

    python3 scripts/probe_api.py
    python3 scripts/probe_api.py --phasedata

Credentials come from `.env` in the repository root (PERIFIC_USERNAME,
PERIFIC_PASSWORD). They are read by this script and never printed.

Writes to scripts/probe_out/ (gitignored):

    raw/         verbatim responses -- contains a real token and MAC address
    redacted/    the same responses with secrets and identifiers replaced
    summary.txt  the findings, safe to paste anywhere

`--phasedata` writes the same three into scripts/probe_out/phasedata/ instead,
so it cannot overwrite the summary of the run that produced the fixtures.

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
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
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


@dataclass(frozen=True)
class Response:
    """One call's outcome, including the failures worth telling apart."""

    status: int | None
    payload: Any
    detail: str | None

    @property
    def ok(self) -> bool:
        return self.status == 200 and self.detail is None


def send(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
    form: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
) -> Response:
    """Call the API without raising, so a probe can read the status code.

    `form` sends `application/x-www-form-urlencoded` instead of JSON, which is
    what `toshi38`'s documentation claims `/getphasedata` wants. `params` puts
    them in the query string instead, for the verbs that carry no body.
    """
    headers = {"Accept": "application/json"}
    if token:
        headers["X-Authorization"] = token

    if form is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form).encode()
    else:
        headers["Content-Type"] = "application/json"
        # aiohttp sends Content-Length: 0 for a bodyless PUT; b"" matches that.
        # A bare None makes urllib omit the header entirely, which some
        # frameworks reject on PUT.
        data = json.dumps(body).encode() if body is not None else b""

    url = f"{BASE_URL}{path}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"

    req = urllib.request.Request(
        url,
        data=data if method in {"PUT", "POST"} else None,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(
            req, timeout=TIMEOUT, context=ssl.create_default_context()
        ) as response:
            status, payload = response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as err:
        return Response(err.code, None, err.read().decode("utf-8", "replace")[:500])
    except urllib.error.URLError as err:
        return Response(None, None, f"unreachable: {err.reason}")

    try:
        return Response(status, json.loads(payload), None)
    except json.JSONDecodeError:
        return Response(status, None, f"response is not JSON: {payload[:200]}")


def request(
    method: str,
    path: str,
    *,
    token: str | None = None,
    body: dict[str, Any] | None = None,
) -> Any:
    """Call the API and return the decoded JSON body."""
    response = send(method, path, token=token, body=body)
    if not response.ok:
        raise ProbeError(
            f"{method} {path} -> HTTP {response.status}: {response.detail}"
        )
    return response.payload


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
# /getphasedata -- what the statistics backfill would depend on
# --------------------------------------------------------------------------
#
# Answers the open questions in docs/plans/2026-09-18-statistics-backfill.md:
# does the endpoint exist, what does its request body look like, what bucket
# granularity does it return, does it carry the cumulative registers or
# per-period sums, and how far back does the free tier retain.
#
# The accepted shape below was found by trial against the real account, and
# every part of it contradicts toshi38's PERIFIC_API_DOCUMENTATION.md. The
# near misses are kept in the probe because each one fails differently, and
# the difference is the evidence that the accepted shape is the right one.

METER_CATEGORY = "LocalPhysical"
METER_TYPE = "Phase"

ISO_SECONDS = "%Y-%m-%dT%H:%M:%S"

PHASEDATA = "/getphasedata"

# Days back to start a one-day window, to find the retention edge. Clustered
# around a fortnight, which is where the edge sits on a free account.
RETENTION_PROBES = (1, 7, 10, 12, 14, 16, 21, 30, 90, 365)

type Window = tuple[datetime, datetime]


def find_meter(overview: Any) -> dict[str, Any] | None:
    """Pick the one item on the account that reports phases."""
    for item in (overview or {}).get("Items") or []:
        if (
            item.get("ItemCategory") == METER_CATEGORY
            and item.get("ItemType") == METER_TYPE
        ):
            return item
    return None


def phasedata_body(item_id: Any, window: Window) -> dict[str, Any]:
    """Build the request body `/getphasedata` actually accepts.

    `startTime` / `endTime` must be ISO strings interpreted as UTC. The same
    two keys carrying epoch milliseconds answer 500, which is how they were
    told apart from a dozen plausible namings that all answered 200 with `[]`.
    """
    start, end = window
    return {
        "itemId": item_id,
        "startTime": start.strftime(ISO_SECONDS),
        "endTime": end.strftime(ISO_SECONDS),
    }


def candidate_requests(
    item_id: Any, window: Window
) -> list[tuple[str, str, dict[str, Any]]]:
    """List the accepted shape first, then the near misses that pin it down."""
    start, end = window
    accepted = phasedata_body(item_id, window)
    documented = {
        "itemId": item_id,
        "fromDate": start.strftime(ISO_SECONDS),
        "toDate": end.strftime(ISO_SECONDS),
        "dataType": "Avg",
    }
    epoch = {
        **accepted,
        "startTime": int(start.timestamp() * 1000),
        "endTime": int(end.timestamp() * 1000),
    }
    return [
        ("PUT, startTime/endTime ISO", "PUT", {"body": accepted}),
        ("PUT, startTime/endTime epoch", "PUT", {"body": epoch}),
        ("PUT, fromDate/toDate (documented)", "PUT", {"body": documented}),
        (
            "PUT, startTime only",
            "PUT",
            {"body": {k: accepted[k] for k in ("itemId", "startTime")}},
        ),
        ("PUT, no body", "PUT", {}),
        ("POST, startTime/endTime ISO", "POST", {"body": accepted}),
        ("GET, startTime/endTime ISO", "GET", {"params": accepted}),
    ]


def outline(value: Any, indent: str = "", label: str = "", depth: int = 0) -> list[str]:
    """Describe a JSON shape nobody has seen before, in a few lines."""
    head = f"{indent}{label + ': ' if label else ''}"
    if depth >= 4:
        return [f"{head}..."]
    if isinstance(value, dict):
        keys = sorted(value)
        shown = keys[:12]
        lines = [f"{head}object {shown}{' ...' if len(keys) > 12 else ''}"]
        for key in shown[:3]:
            lines += outline(value[key], indent + "  ", key, depth + 1)
        return lines
    if isinstance(value, list):
        lines = [f"{head}array of {len(value)}"]
        if value:
            lines += outline(value[0], indent + "  ", "[0]", depth + 1)
        return lines
    return [f"{head}{value!r}"]


def collect_points(payload: Any) -> list[dict[str, Any]]:
    """Every `{ts, data}` object anywhere in a response.

    Written to walk rather than to index, because the nesting is one of the
    things being discovered.
    """
    found: list[dict[str, Any]] = []
    stack = [payload]
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            if "ts" in current and isinstance(current.get("data"), dict):
                found.append(current)
            stack.extend(current.values())
        elif isinstance(current, list):
            stack.extend(current)
    return found


def point_time(value: Any) -> datetime | None:
    """Parse a point's `ts`, which may be epoch ms or an ISO string."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, UTC)
    if isinstance(value, str):
        return parse_api_time(value)
    return None


def report_series(report: Report, payload: Any) -> None:
    """Report the granularity and field coverage of whatever came back."""
    points = collect_points(payload)
    report(f"    Points          : {len(points)}")
    if not points:
        report("    No {ts, data} objects found. Shape summary:")
        for line in outline(payload, "      "):
            report(line)
        return

    stamps = sorted(t for t in (point_time(p.get("ts")) for p in points) if t)
    if len(stamps) >= 2:
        spacings = sorted((b - a).total_seconds() for a, b in pairwise(stamps))
        median = spacings[len(spacings) // 2]
        report(
            f"    First / last    : {stamps[0].isoformat()} .. {stamps[-1].isoformat()}"
        )
        report(f"    Median spacing  : {median:.0f}s ({median / 60:.1f} min)")
        report(f"    Spacing range   : {spacings[0]:.0f}s .. {spacings[-1]:.0f}s")

    keys: set[str] = set()
    for point in points:
        keys |= set(point.get("data") or {})
    report(f"    data keys       : {sorted(keys)}")

    # The question the backfill turns on: async_import_statistics wants a `sum`
    # and a `state`, and which of those the endpoint can supply depends on
    # whether it returns the cumulative registers or only the averages.
    registers = sorted(set(ENERGY_KEYS) & keys)
    if registers:
        report(
            f"    Cumulative      : {registers} present -- maps onto `state` directly"
        )
        sample = next(
            (p for p in points if any(k in (p.get("data") or {}) for k in registers)),
            None,
        )
        if sample:
            values = {k: (sample.get("data") or {}).get(k) for k in registers}
            report(f"    Sample          : {values}")
    else:
        report(
            f"    Cumulative      : NEITHER {list(ENERGY_KEYS)} present -- "
            "energy would have to be derived"
        )


def probe_phasedata_shape(
    report: Report, token: str, item_id: Any, window: Window
) -> bool:
    """Confirm which verb and body the server accepts, and how the rest fail."""
    report.section("5. /getphasedata -- verb and request shape")
    report(f"Item queried       : {item_id}")
    report(f"Window             : {window[0].isoformat()} .. {window[1].isoformat()}")
    report()

    accepted = False
    for label, method, kwargs in candidate_requests(item_id, window):
        response = send(method, PHASEDATA, token=token, **kwargs)
        if not response.ok:
            detail = (response.detail or "").replace("\n", " ")[:70]
            report(f"  {label:<36} HTTP {response.status}  {detail}")
            continue
        points = len(collect_points(response.payload))
        report(f"  {label:<36} HTTP 200  {points} point(s)")
        if points and label.startswith("PUT, startTime"):
            accepted = True

    report()
    if accepted:
        report("Accepted: PUT with itemId and startTime, as ISO read in UTC.")
        report("endTime is optional and defaults to now. Note every other line")
        report("above: POST is 405, the documented fromDate/toDate bind to nothing")
        report("and return an empty array, and the same two keys carrying epoch")
        report("milliseconds answer 500.")
    else:
        report("The shape that worked before does not work now. Something changed")
        report("server-side; re-derive it before trusting anything downstream.")
    return accepted


def probe_phasedata_series(
    report: Report, token: str, item_id: Any, window: Window
) -> list[tuple[str, Any]]:
    """Report the granularity and fields of one window."""
    report.section("6. /getphasedata -- granularity and fields")
    response = send("PUT", PHASEDATA, token=token, body=phasedata_body(item_id, window))

    groups = response.payload if isinstance(response.payload, list) else []
    report(f"    Top level       : array of {len(groups)}")
    for group in groups[:3]:
        inner = group.get("data") if isinstance(group, dict) else None
        report(
            f"      dt={group.get('dt')!r} "
            f"holding {len(inner) if isinstance(inner, list) else '?'} point(s)"
        )
    report_series(report, response.payload)

    return [("05-getphasedata-recent", response.payload)]


def probe_phasedata_retention(
    report: Report, token: str, item_id: Any, now: datetime
) -> list[tuple[str, Any]]:
    """Find how far back the account still answers for."""
    report.section("7. /getphasedata -- retention")
    report("A one-day window ending N days ago. The free tier's retention limit is")
    report("the reason this project exists, so this bounds the backfill window.")
    report()

    captures: list[tuple[str, Any]] = []
    for days in RETENTION_PROBES:
        end = now - timedelta(days=days)
        window = (end - timedelta(days=1), end)
        response = send(
            "PUT", PHASEDATA, token=token, body=phasedata_body(item_id, window)
        )
        if not response.ok:
            report(f"  {days:>4}d ago : HTTP {response.status}")
            continue
        points = len(collect_points(response.payload))
        report(
            f"  {days:>4}d ago : {points} point(s){'  <- empty' if not points else ''}"
        )
        if points:
            # Overwritten each time round, so what survives is the oldest window
            # that still answered -- the backfill horizon itself.
            captures = [("06-getphasedata-oldest", response.payload)]

    report()
    report("The oldest window that still returns points is the backfill horizon.")
    return captures


def probe_phasedata(report: Report, token: str, overview: Any) -> list[tuple[str, Any]]:
    """Find out whether /getphasedata is real, and what it returns."""
    meter = find_meter(overview)
    if meter is None:
        raise ProbeError(
            f"no {METER_CATEGORY}/{METER_TYPE} item on the account to query"
        )
    item_id = meter.get("ItemId")

    now = datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
    window = (now - timedelta(hours=6), now)

    if not probe_phasedata_shape(report, token, item_id, window):
        return []

    return probe_phasedata_series(
        report, token, item_id, window
    ) + probe_phasedata_retention(report, token, item_id, now)


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
        "--phasedata",
        action="store_true",
        help="probe /getphasedata instead; answers the backfill plan's questions",
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

    # Kept apart so a phasedata run does not overwrite the summary of the probe
    # that produced the fixtures.
    if args.phasedata and args.out == DEFAULT_OUT:
        args.out = DEFAULT_OUT / "phasedata"

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

        overview = probe_overview(report, token)
        captures.append(("02-getaccountoverview", overview))

        if args.phasedata:
            captures += probe_phasedata(report, token, overview)
            return 0

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
