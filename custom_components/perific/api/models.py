"""Response models for the Enegic API.

Frozen dataclasses parsed with ``.get()`` and defaults. Field sets differ between
packet versions, between buckets and between device variants, so an absent field
becomes ``None`` and leaves the rest of the response usable. See
``docs/api/enegic.md`` and ``docs/device-notes.md``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from .exceptions import PerificResponseError

if TYPE_CHECKING:
    from collections.abc import Callable

_LOGGER = logging.getLogger(__name__)

BUCKET_REALTIME = "PhaseRealTime"
BUCKET_MINUTE = "PhaseMinute"
BUCKET_HOUR = "PhaseHour"
BUCKET_DAY = "PhaseDay"

ITEM_CATEGORY_METER = "LocalPhysical"
ITEM_TYPE_METER = "Phase"

# Seven fractional digits, which datetime.fromisoformat rejects.
_OVERLONG_FRACTION = re.compile(r"(\.\d{6})\d+")


def parse_api_datetime(value: Any) -> datetime | None:
    """Parse an ISO-8601 timestamp as the API writes them, or return None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(_OVERLONG_FRACTION.sub(r"\1", value))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def _parse_each[T](
    entries: list[Any], parse: Callable[[Any], T], label: str
) -> list[T]:
    """Parse what can be parsed, skipping entries that cannot.

    One malformed entry on an account must not cost every other device its update.
    """
    parsed: list[T] = []
    for entry in entries:
        try:
            parsed.append(parse(entry))
        except PerificResponseError as err:
            _LOGGER.warning("Skipping unparseable %s: %s", label, err)
    return parsed


def _is_numeric(value: Any) -> bool:
    # bool is an int subclass, and a flag field must not read as a measurement.
    return isinstance(value, int | float) and not isinstance(value, bool)


def _epoch_ms(value: Any) -> datetime | None:
    return datetime.fromtimestamp(value / 1000, tz=UTC) if _is_numeric(value) else None


def _enum(value: Any) -> str | None:
    """Read an enum field, where unset arrives as the string "None"."""
    if not isinstance(value, str) or value in ("", "None"):
        return None
    return value


def _text(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _number(value: Any) -> float | None:
    return float(value) if _is_numeric(value) else None


def _whole(value: Any) -> int | None:
    return int(value) if _is_numeric(value) else None


def _per_phase(value: Any) -> tuple[float | None, ...]:
    """Read a per-phase array, keeping the index of any unreadable entry."""
    if not isinstance(value, list):
        return ()
    return tuple(_number(entry) for entry in value)


@dataclass(frozen=True, slots=True)
class TokenInfo:
    """An access token and its validity window."""

    token: str
    created: datetime | None = None
    valid_to: datetime | None = None

    @classmethod
    def from_api(cls, payload: Any) -> TokenInfo:
        """Build from a ``/createtoken`` response body."""
        info = payload.get("TokenInfo") if isinstance(payload, dict) else None
        token = _text(info.get("Token")) if isinstance(info, dict) else None
        if info is None or token is None:
            raise PerificResponseError("Login response carried no token")
        return cls(
            token=token,
            created=parse_api_datetime(info.get("Created")),
            valid_to=parse_api_datetime(info.get("ValidTo")),
        )


@dataclass(frozen=True, slots=True)
class Item:
    """A device on the account."""

    item_id: int
    name: str | None = None
    system_name: str | None = None
    category: str | None = None
    item_type: str | None = None
    sub_type: str | None = None
    state: str | None = None
    mac_address: str | None = None
    firmware: str | None = None
    hardware: str | None = None
    time_zone: str | None = None

    @property
    def is_meter(self) -> bool:
        """Whether this item is a phase meter rather than a charger or a stub."""
        return (
            self.category == ITEM_CATEGORY_METER and self.item_type == ITEM_TYPE_METER
        )

    @classmethod
    def from_api(cls, payload: Any) -> Item:
        """Build from one entry of ``/getaccountoverview``'s ``Items``."""
        if not isinstance(payload, dict):
            raise PerificResponseError(f"Expected an item object, got {type(payload)}")
        item_id = _whole(payload.get("ItemId"))
        if item_id is None:
            raise PerificResponseError("Item carried no ItemId")
        parameters = payload.get("ActualItemUserParameters")
        if not isinstance(parameters, dict):
            parameters = {}
        return cls(
            item_id=item_id,
            name=_text(payload.get("Name")),
            system_name=_text(payload.get("SystemName")),
            category=_enum(payload.get("ItemCategory")),
            item_type=_enum(payload.get("ItemType")),
            sub_type=_enum(payload.get("ItemSubType")),
            state=_enum(payload.get("ItemState")),
            mac_address=_text(payload.get("MacAddress")),
            firmware=_text(parameters.get("FW")),
            hardware=_text(parameters.get("HW")),
            time_zone=_text(payload.get("TimeZone")),
        )


@dataclass(frozen=True, slots=True)
class PhaseData:
    """The measurements inside a packet.

    Which of these are present depends on the bucket: ``PhaseRealTime`` carries no
    energy registers at all.
    """

    energy_import: float | None = None
    energy_export: float | None = None
    current: tuple[float | None, ...] = ()
    voltage: tuple[float | None, ...] = ()
    current_min: tuple[float | None, ...] = ()
    current_max: tuple[float | None, ...] = ()

    @classmethod
    def from_api(cls, payload: Any) -> PhaseData:
        """Build from a packet's ``data`` object."""
        if not isinstance(payload, dict):
            return cls()
        return cls(
            energy_import=_number(payload.get("hwi")),
            energy_export=_number(payload.get("hwo")),
            current=_per_phase(payload.get("hiavg")),
            voltage=_per_phase(payload.get("huavg")),
            current_min=_per_phase(payload.get("himin")),
            current_max=_per_phase(payload.get("himax")),
        )


@dataclass(frozen=True, slots=True)
class Packet:
    """One bucket's reading for one item."""

    bucket: str
    timestamp: datetime | None = None
    seqno: int | None = None
    packet_version: int | None = None
    firmware: str | None = None
    rssi: int | None = None
    data: PhaseData = field(default_factory=PhaseData)

    @classmethod
    def from_api(cls, bucket: str, payload: Any) -> Packet:
        """Build from one entry of an item's ``LatestPackets``."""
        if not isinstance(payload, dict):
            return cls(bucket=bucket)
        return cls(
            bucket=bucket,
            timestamp=_epoch_ms(payload.get("ts")),
            seqno=_whole(payload.get("seqno")),
            packet_version=_whole(payload.get("pv")),
            firmware=_text(payload.get("fw")),
            rssi=_whole(payload.get("rssi")),
            data=PhaseData.from_api(payload.get("data")),
        )


@dataclass(frozen=True, slots=True)
class ItemPackets:
    """Every bucket the API returned for one item."""

    item_id: int
    packets: dict[str, Packet] = field(default_factory=dict)

    @property
    def minute(self) -> Packet | None:
        """The minute bucket — the freshest one carrying energy registers."""
        return self.packets.get(BUCKET_MINUTE)

    @property
    def realtime(self) -> Packet | None:
        """The ~10 s bucket, which carries current and voltage but no energy."""
        return self.packets.get(BUCKET_REALTIME)

    @classmethod
    def from_api(cls, payload: Any) -> ItemPackets:
        """Build from one entry of a ``/getlatestpackets`` response."""
        if not isinstance(payload, dict):
            raise PerificResponseError(f"Expected a packet object, got {type(payload)}")
        item_id = _whole(payload.get("ItemId"))
        if item_id is None:
            raise PerificResponseError("Packet entry carried no ItemId")
        buckets = payload.get("LatestPackets")
        if not isinstance(buckets, dict):
            buckets = {}
        return cls(
            item_id=item_id,
            packets={
                bucket: Packet.from_api(bucket, body)
                for bucket, body in buckets.items()
            },
        )


def parse_items(payload: Any) -> list[Item]:
    """Build the item list from a ``/getaccountoverview`` response body."""
    entries = payload.get("Items") if isinstance(payload, dict) else None
    if not isinstance(entries, list):
        raise PerificResponseError("Account overview carried no Items list")
    return _parse_each(entries, Item.from_api, "item")


def parse_latest_packets(payload: Any) -> dict[int, ItemPackets]:
    """Build the per-item readings from a ``/getlatestpackets`` response body.

    Items reporting nothing are absent rather than present and empty.
    """
    if not isinstance(payload, list):
        raise PerificResponseError("Latest packets was not a list")
    return {
        entry.item_id: entry
        for entry in _parse_each(payload, ItemPackets.from_api, "packet")
    }
