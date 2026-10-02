from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import BINARY_SENSORS, DOMAIN


async def async_setup_entry(hass, entry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TraderBinarySensor(runtime.coordinator, entry, key) for key in BINARY_SENSORS])


class TraderBinarySensor(CoordinatorEntity, BinarySensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key

    @property
    def is_on(self):
        state = self.coordinator.data
        if self._key == "trading_enabled":
            return state.system_ready and not state.circuit_breaker
        return bool(getattr(state, self._key, False))
