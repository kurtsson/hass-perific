#!/usr/bin/env python3
"""Measure the holes in the long-term statistics series.

`docs/plans/2026-09-18-statistics-backfill.md` gates the backfill work on this
one question: are the real polling gaps long enough to be worth repairing?

A gap shorter than an hour costs nothing. `hwi` / `hwo` are cumulative
registers, so when polling resumes the counter has already advanced by the
whole gap's energy and Home Assistant folds that delta into the hour the
reading landed in -- which, for a sub-hour gap, is the hour it belonged to
anyway. Only a gap that spans an hour boundary loses anything, and that is
exactly an hour with no row in the `statistics` table.

So the measurement is: which hours are missing, how long are the runs, and how
big is the spike in the hour collection resumed.

Hourly statistics are read over Home Assistant's websocket API. The REST API
does not expose them at all, and `/api/history` reaches back only as far as the
recorder's purge window -- the `statistics` table is never purged, which is the
whole reason this project exists.

    uv run python scripts/measure_gaps.py

Configuration comes from `.env` in the repository root: HA_URL and HA_TOKEN,
the same two keys `scripts/deploy.sh` reads. This script loads them itself and
never prints them.

Read-only. It sends no command that changes anything on the instance.
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

import aiohttp

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_FILE = REPO_ROOT / ".env"

PLATFORM = "perific"
HOUR = timedelta(hours=1)

# Home Assistant has emitted hourly statistics for far longer than this project
# has existed; starting here is simply "everything".
EPOCH = datetime(2020, 1, 1, tzinfo=UTC)


class MeasureError(Exception):
    """The measurement could not complete."""


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def load_env(path: Path) -> dict[str, str]:
    """Parse a dotenv file. Deliberately minimal: KEY=value, # comments."""
    if not path.is_file():
        raise MeasureError(f"no {path.name} found at {path.parent}")

    values: dict[str, str] = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip().strip("\"'")
    return values


# --------------------------------------------------------------------------
# Websocket
# --------------------------------------------------------------------------


class Connection:
    """A thin request/response wrapper over Home Assistant's websocket API."""

    def __init__(self, socket: aiohttp.ClientWebSocketResponse) -> None:
        self._socket = socket
        self._next_id = 1

    @staticmethod
    async def _expect(socket: aiohttp.ClientWebSocketResponse) -> dict[str, Any]:
        message = await socket.receive_json()
        if not isinstance(message, dict):
            raise MeasureError(f"unexpected websocket frame: {message!r}")
        return message

    @classmethod
    async def authenticate(
        cls, socket: aiohttp.ClientWebSocketResponse, token: str
    ) -> Connection:
        """Complete the auth handshake, which precedes any numbered command."""
        hello = await cls._expect(socket)
        if hello.get("type") != "auth_required":
            raise MeasureError(f"expected auth_required, got {hello.get('type')!r}")

        await socket.send_json({"type": "auth", "access_token": token})
        result = await cls._expect(socket)
        if result.get("type") != "auth_ok":
            raise MeasureError(
                f"authentication rejected: {result.get('message', result)}. "
                "Check HA_TOKEN in .env."
            )
        return cls(socket)

    async def command(self, **payload: Any) -> Any:
        """Send one command and return its result."""
        message_id = self._next_id
        self._next_id += 1
        await self._socket.send_json({"id": message_id, **payload})

        # Subscriptions and other in-flight traffic can interleave; match on id.
        while True:
            message = await self._expect(self._socket)
            if message.get("id") != message_id or message.get("type") != "result":
                continue
            if not message.get("success"):
                error = message.get("error") or {}
                raise MeasureError(
                    f"{payload.get('type')} failed: "
                    f"{error.get('code')} {error.get('message')}"
                )
            return message.get("result")


# --------------------------------------------------------------------------
# Statistics
# --------------------------------------------------------------------------


async def find_energy_statistic_ids(connection: Connection) -> list[str]:
    """Find the integration's cumulative-energy entities, by statistic id.

    Discovered through the entity registry rather than by matching on the name:
    entity IDs are built from translated names, so they differ with the
    instance's language.
    """
    entities = await connection.command(type="config/entity_registry/list")
    ours = {
        entry["entity_id"]
        for entry in entities
        if entry.get("platform") == PLATFORM and entry.get("entity_id")
    }
    if not ours:
        raise MeasureError(
            f"no entities registered by the {PLATFORM} integration on this instance"
        )

    # has_sum is what distinguishes the TOTAL_INCREASING energy registers from
    # the MEASUREMENT sensors, which carry no sum and cannot show a gap this way.
    listed = await connection.command(
        type="recorder/list_statistic_ids", statistic_type="sum"
    )
    return sorted(
        entry["statistic_id"] for entry in listed if entry.get("statistic_id") in ours
    )


def to_datetime(value: Any) -> datetime | None:
    """Parse a statistics row's `start`, which HA has typed both ways."""
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, UTC)
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value)
        except ValueError:
            return None
    return None


async def fetch_hours(
    connection: Connection, statistic_id: str, start: datetime
) -> list[tuple[datetime, float | None]]:
    """Every hourly row for one statistic, oldest first."""
    result = await connection.command(
        type="recorder/statistics_during_period",
        start_time=start.isoformat(),
        statistic_ids=[statistic_id],
        period="hour",
        types=["sum"],
    )
    rows = (result or {}).get(statistic_id) or []

    hours: list[tuple[datetime, float | None]] = []
    for row in rows:
        when = to_datetime(row.get("start"))
        if when is None:
            continue
        total = row.get("sum")
        hours.append(
            (
                when.replace(minute=0, second=0, microsecond=0),
                float(total) if isinstance(total, (int, float)) else None,
            )
        )
    hours.sort(key=lambda pair: pair[0])
    return hours


@dataclass(frozen=True)
class Gap:
    """A run of consecutive hours with no statistics row.

    Compared by value, because every energy sensor on a meter stops together:
    the same outage surfaces once per statistic and must be counted once.
    """

    first: datetime
    last: datetime

    @property
    def hours(self) -> int:
        return int((self.last - self.first) / HOUR) + 1

    @property
    def resumed(self) -> datetime:
        return self.last + HOUR


def find_gaps(hours: list[tuple[datetime, float | None]]) -> list[Gap]:
    """Missing hours between the first and last row.

    Only the interior counts. Nothing before the series starts is a gap, and a
    series that ends an hour ago has not lost that hour -- it has not written it
    yet.
    """
    return [
        Gap(previous + HOUR, current - HOUR)
        for (previous, _), (current, _) in pairwise(hours)
        if current - previous > HOUR
    ]


def hourly_deltas(hours: list[tuple[datetime, float | None]]) -> dict[datetime, float]:
    """Energy booked into each hour: the rise in `sum` since the row before it."""
    return {
        when: after - before
        for (_, before), (when, after) in pairwise(hours)
        if before is not None and after is not None
    }


# --------------------------------------------------------------------------
# Reporting
# --------------------------------------------------------------------------


def local(when: datetime) -> str:
    return when.astimezone().strftime("%Y-%m-%d %H:%M %Z")


def describe_duration(hours: int) -> str:
    if hours < 24:
        return f"{hours}h"
    return f"{hours // 24}d {hours % 24}h"


def report_statistic(
    statistic_id: str, hours: list[tuple[datetime, float | None]]
) -> list[Gap]:
    """Print one statistic's findings and hand back its gaps."""
    print()
    print(statistic_id)
    print("-" * len(statistic_id))

    if not hours:
        print("  No hourly statistics at all. Either the integration has never run")
        print("  here, or its typing was rejected -- check the log before reading")
        print("  anything else into this.")
        return []

    first, last = hours[0][0], hours[-1][0]
    span = int((last - first) / HOUR) + 1
    print(f"  First hour      : {local(first)}")
    print(f"  Last hour       : {local(last)}")
    print(f"  Span            : {span} hours")
    print(f"  Hours recorded  : {len(hours)}")

    gaps = find_gaps(hours)
    missing = sum(gap.hours for gap in gaps)
    print(f"  Hours missing   : {missing}")

    if not gaps:
        print()
        print("  No missing hours. Every gap so far was shorter than an hour, which")
        print("  costs nothing -- the delta landed in the hour it belonged to.")
        return []

    deltas = hourly_deltas(hours)
    typical = statistics.median(deltas.values()) if deltas else 0.0
    print(f"  Median hour     : {typical:.3f} kWh")
    print()
    print(f"  {len(gaps)} gap(s), newest last:")

    for gap in gaps:
        spike = deltas.get(gap.resumed)
        print()
        print(f"    {local(gap.first)} -> {local(gap.last)}")
        print(f"      Length      : {describe_duration(gap.hours)} of lost hours")
        print(f"      Resumed     : {local(gap.resumed)}")
        if spike is None:
            print("      Spike       : unknown (no sum on one side)")
        else:
            ratio = f", {spike / typical:.0f}x the median" if typical else ""
            print(f"      Spike       : {spike:.3f} kWh into that one hour{ratio}")

    return gaps


def report_verdict(all_gaps: list[Gap]) -> None:
    """Answer the gate the plan actually asks about."""
    print()
    print("Verdict")
    print("-------")

    # One outage stops every sensor, so the same gap arrives once per statistic.
    all_gaps = sorted(set(all_gaps), key=lambda gap: gap.first)

    if not all_gaps:
        print("No gap has ever spanned an hour boundary.")
        print()
        print("On this evidence the backfill has no value: every outage so far cost")
        print("nothing but the shape of one hour, and the lifetime total and the")
        print("year-over-year comparison -- the project's actual goal -- are intact.")
        print("Re-run this after the next outage rather than building anything.")
        return

    longest = max(gap.hours for gap in all_gaps)
    total = sum(gap.hours for gap in all_gaps)
    print(f"Gaps spanning an hour boundary : {len(all_gaps)}")
    print(f"Longest                        : {describe_duration(longest)}")
    print(f"Total hours lost               : {total}")
    print()
    print("These are the hours a backfill would repair, and the spikes above are")
    print("what re-importing the resumption hour would have to correct rather than")
    print("add to. Whether that is worth building still depends on /getphasedata")
    print("existing and reaching back far enough -- probe_api.py --phasedata.")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


async def measure(url: str, token: str, start: datetime, wanted: list[str]) -> int:
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        try:
            socket = await session.ws_connect(f"{url}/api/websocket")
        except aiohttp.ClientError as err:
            raise MeasureError(f"cannot reach {url}: {err}") from err

        async with socket:
            connection = await Connection.authenticate(socket, token)

            statistic_ids = wanted or await find_energy_statistic_ids(connection)
            if not statistic_ids:
                raise MeasureError(
                    "no cumulative-energy statistics found for the "
                    f"{PLATFORM} integration. A missing statistics graph means the "
                    "sensor typing was rejected; read the log."
                )

            print(f"Instance   : {url}")
            print(f"Since      : {local(start)}")
            print(f"Statistics : {', '.join(statistic_ids)}")

            all_gaps: list[Gap] = []
            for statistic_id in statistic_ids:
                hours = await fetch_hours(connection, statistic_id, start)
                all_gaps += report_statistic(statistic_id, hours)

            report_verdict(all_gaps)

    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--days",
        type=int,
        metavar="N",
        help="only look at the last N days (default: the whole series)",
    )
    parser.add_argument(
        "--statistic-id",
        action="append",
        default=[],
        metavar="ID",
        help="measure this statistic instead of discovering the integration's",
    )
    args = parser.parse_args()

    start = (
        datetime.now(UTC) - timedelta(days=args.days) if args.days else EPOCH
    ).replace(minute=0, second=0, microsecond=0)

    try:
        env = load_env(ENV_FILE)
        url = env.get("HA_URL", "").rstrip("/")
        token = env.get("HA_TOKEN", "")
        if not url or not token:
            raise MeasureError(".env must define HA_URL and HA_TOKEN")

        return asyncio.run(measure(url, token, start, args.statistic_id))
    except MeasureError as err:
        print(f"\nFAILED: {err}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
