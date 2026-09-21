"""Sensor entities.

The typing in ``SENSORS`` is load-bearing: Home Assistant decides whether to record
long-term statistics from ``state_class`` and the unit, and declines with nothing
louder than a log warning. See ``docs/specs/perific-integration.md``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
)
from homeassistant.core import callback
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import (
    DOMAIN,
    KEY_LAST_PACKET,
    KEY_STATUS,
    MANUFACTURER,
    STALE_AFTER,
    STATUS_NO_DATA,
    STATUS_OFFLINE,
    STATUS_OK,
    STATUS_OPTIONS,
    STATUS_STALE,
)

# PerificCoordinator parameterises CoordinatorEntity, so it is needed at runtime.
from .coordinator import PerificCoordinator

if TYPE_CHECKING:
    from collections.abc import Callable
    from datetime import datetime

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import Item, ItemPackets, PhaseData
    from .coordinator import PerificConfigEntry

_LOGGER = logging.getLogger(__name__)

PHASES = (1, 2, 3)

type PacketValue = float | datetime | None


@dataclass(frozen=True, kw_only=True)
class PerificSensorEntityDescription(SensorEntityDescription):
    """Describes a sensor, including where in a packet its value comes from."""

    value_fn: Callable[[ItemPackets], PacketValue]


def _phase_powers(packets: ItemPackets) -> list[float] | None:
    """Power per phase, positive importing and negative exporting.

    The API carries no power field. This product is apparent power, but the vendor's
    own app displays exactly it, and against the energy registers it reconciles to
    within a few percent — see ``docs/device-notes.md``.
    """
    packet = packets.realtime
    if packet is None:
        return None
    currents, voltages = packet.data.current, packet.data.voltage
    if not currents or len(currents) != len(voltages):
        return None
    powers = []
    for current, voltage in zip(currents, voltages, strict=True):
        if current is None or voltage is None:
            return None
        powers.append(current * voltage)
    return powers


def _power_import(packets: ItemPackets) -> float | None:
    powers = _phase_powers(packets)
    if powers is None:
        return None
    return round(sum(power for power in powers if power > 0), 1)


def _power_export(packets: ItemPackets) -> float | None:
    # Reported unsigned, which is what the Energy dashboard's two-sensor mode expects.
    powers = _phase_powers(packets)
    if powers is None:
        return None
    return round(-sum(power for power in powers if power < 0), 1)


def _last_packet(packets: ItemPackets) -> datetime | None:
    """Take the newest timestamp across the buckets.

    The hour and day buckets stamp the start of their period, so the maximum is the
    real-time bucket whenever one arrived.
    """
    stamps = [
        packet.timestamp
        for packet in packets.packets.values()
        if packet.timestamp is not None
    ]
    return max(stamps) if stamps else None


def _currents(data: PhaseData) -> tuple[float | None, ...]:
    return data.current


def _voltages(data: PhaseData) -> tuple[float | None, ...]:
    return data.voltage


def _phase_reading(
    read: Callable[[PhaseData], tuple[float | None, ...]], phase: int
) -> Callable[[ItemPackets], float | None]:
    """Pick one phase out of a per-phase array in the real-time packet."""

    def value_fn(packets: ItemPackets) -> float | None:
        packet = packets.realtime
        if packet is None:
            return None
        values = read(packet.data)
        return values[phase - 1] if phase <= len(values) else None

    return value_fn


_PHASE_SENSORS: tuple[PerificSensorEntityDescription, ...] = tuple(
    description
    for phase in PHASES
    for description in (
        PerificSensorEntityDescription(
            key=f"current_l{phase}",
            translation_key=f"current_l{phase}",
            device_class=SensorDeviceClass.CURRENT,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
            suggested_display_precision=2,
            value_fn=_phase_reading(_currents, phase),
        ),
        PerificSensorEntityDescription(
            key=f"voltage_l{phase}",
            translation_key=f"voltage_l{phase}",
            device_class=SensorDeviceClass.VOLTAGE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfElectricPotential.VOLT,
            suggested_display_precision=1,
            entity_category=EntityCategory.DIAGNOSTIC,
            value_fn=_phase_reading(_voltages, phase),
        ),
    )
)


# No cumulative-energy sensors. The hourly energy series is imported straight
# into long-term statistics from the vendor's own record — see history.py — so
# an entity accumulating the same registers from polling would only add a second,
# gappier copy of it under a confusingly similar name.
SENSORS: tuple[PerificSensorEntityDescription, ...] = (
    PerificSensorEntityDescription(
        key="power_import",
        translation_key="power_import",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=_power_import,
    ),
    PerificSensorEntityDescription(
        key="power_export",
        translation_key="power_export",
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT,
        native_unit_of_measurement=UnitOfPower.WATT,
        suggested_display_precision=0,
        value_fn=_power_export,
    ),
    PerificSensorEntityDescription(
        key=KEY_LAST_PACKET,
        translation_key=KEY_LAST_PACKET,
        device_class=SensorDeviceClass.TIMESTAMP,
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=_last_packet,
    ),
    *_PHASE_SENSORS,
)

STATUS_SENSOR = SensorEntityDescription(
    key=KEY_STATUS,
    translation_key=KEY_STATUS,
    device_class=SensorDeviceClass.ENUM,
    options=STATUS_OPTIONS,
    entity_category=EntityCategory.DIAGNOSTIC,
)


async def async_setup_entry(
    _hass: HomeAssistant,
    entry: PerificConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one set of sensors per meter on the account."""
    coordinator = entry.runtime_data
    async_add_entities(
        [
            *(
                PerificPacketSensor(coordinator, meter, description)
                for meter in coordinator.meters
                for description in SENSORS
            ),
            *(
                PerificStatusSensor(coordinator, meter, STATUS_SENSOR)
                for meter in coordinator.meters
            ),
        ]
    )


class PerificSensor(CoordinatorEntity[PerificCoordinator], SensorEntity):
    """A reading belonging to one meter."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PerificCoordinator,
        meter: Item,
        description: SensorEntityDescription,
    ) -> None:
        """Bind the entity to one meter and one reading."""
        super().__init__(coordinator)
        self.entity_description = description
        self._item_id = meter.item_id
        self._attr_unique_id = f"{meter.item_id}_{description.key}"

        connections = set()
        if meter.mac_address:
            connections.add((CONNECTION_NETWORK_MAC, meter.mac_address))
        self._attr_device_info = DeviceInfo(
            # ItemId is the only stable identifier the API offers; Name is editable.
            identifiers={(DOMAIN, str(meter.item_id))},
            connections=connections,
            manufacturer=MANUFACTURER,
            model=meter.sub_type,
            name=meter.name or meter.system_name or "Perific",
            sw_version=meter.firmware,
            hw_version=meter.hardware,
        )

    def _read(self) -> float | datetime | str | None:
        """Pull this sensor's reading out of the coordinator."""
        raise NotImplementedError

    def _next_value(self) -> float | datetime | str | None:
        """Compute the value to publish this poll."""
        return self._read()

    async def async_added_to_hass(self) -> None:
        """Publish a first value once any restored state is in place."""
        await super().async_added_to_hass()
        self._attr_native_value = self._next_value()

    @callback
    def _handle_coordinator_update(self) -> None:
        """Recompute once per poll — the guard must not run inside a property."""
        self._attr_native_value = self._next_value()
        super()._handle_coordinator_update()

    @property
    def available(self) -> bool:
        """Report unavailable rather than publishing a gap as a value."""
        return super().available and self.native_value is not None


class PerificPacketSensor(PerificSensor):
    """A reading taken straight out of one meter's latest packets."""

    entity_description: PerificSensorEntityDescription

    def _read(self) -> PacketValue:
        packets = self.coordinator.data.get(self._item_id)
        if packets is None:
            return None
        return self.entity_description.value_fn(packets)


class PerificStatusSensor(PerificSensor):
    """Why the other entities look the way they do.

    Every other sensor answers a failure by going unavailable, which says nothing
    about the cause. This one stays up through a failed poll on purpose — it is the
    entity that has to be readable when nothing else is.
    """

    def _read(self) -> str:
        if not self.coordinator.last_update_success:
            return STATUS_OFFLINE
        packets = self.coordinator.data.get(self._item_id)
        last = _last_packet(packets) if packets is not None else None
        if last is None:
            return STATUS_NO_DATA
        if dt_util.utcnow() - last > STALE_AFTER:
            return STATUS_STALE
        return STATUS_OK

    @property
    def available(self) -> bool:
        """Always available; the state is the report."""
        return True
