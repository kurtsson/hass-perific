"""Sensor entities.

The typing in ``SENSORS`` is load-bearing: Home Assistant decides whether to record
long-term statistics from ``state_class`` and the unit, and declines with nothing
louder than a log warning. See ``docs/specs/perific-integration.md``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import UnitOfEnergy
from homeassistant.helpers.device_registry import CONNECTION_NETWORK_MAC, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, MANUFACTURER

# PerificCoordinator parameterises CoordinatorEntity, so it is needed at runtime.
from .coordinator import PerificCoordinator

if TYPE_CHECKING:
    from collections.abc import Callable

    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

    from .api import Item, ItemPackets
    from .coordinator import PerificConfigEntry


@dataclass(frozen=True, kw_only=True)
class PerificSensorEntityDescription(SensorEntityDescription):
    """Describes a sensor, including where in a packet its value comes from."""

    value_fn: Callable[[ItemPackets], float | None]


def _energy_import(packets: ItemPackets) -> float | None:
    # PhaseMinute, not PhaseRealTime: the real-time bucket carries no energy
    # registers at all.
    return packets.minute.data.energy_import if packets.minute else None


SENSORS: tuple[PerificSensorEntityDescription, ...] = (
    PerificSensorEntityDescription(
        key="energy_import",
        translation_key="energy_import",
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING,
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        value_fn=_energy_import,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: PerificConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up one set of sensors per meter on the account."""
    coordinator = entry.runtime_data
    async_add_entities(
        PerificSensor(coordinator, meter, description)
        for meter in coordinator.meters
        for description in SENSORS
    )


class PerificSensor(CoordinatorEntity[PerificCoordinator], SensorEntity):
    """A reading taken from one meter's latest packets."""

    entity_description: PerificSensorEntityDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PerificCoordinator,
        meter: Item,
        description: PerificSensorEntityDescription,
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

    @property
    def native_value(self) -> float | None:
        """The reading, or None when this packet doesn't carry it."""
        packets = self.coordinator.data.get(self._item_id)
        if packets is None:
            return None
        return self.entity_description.value_fn(packets)

    @property
    def available(self) -> bool:
        """Report unavailable rather than publishing a gap as a value."""
        return super().available and self.native_value is not None
