from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import UnitOfCurrency
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NUMERIC_SENSORS, TEXT_SENSORS


async def async_setup_entry(hass, entry, async_add_entities: AddEntitiesCallback) -> None:
    runtime = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([TraderSensor(runtime.coordinator, entry, key) for key in (*NUMERIC_SENSORS, *TEXT_SENSORS)])


class TraderSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key: str) -> None:
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.entry_id}_{key}"
        self._attr_translation_key = key

    @property
    def native_value(self):
        state = self.coordinator.data
        if self._key == "portfolio_equity": return float(state.portfolio.equity) if state.portfolio else 0.0
        if self._key == "available_cash": return float(state.portfolio.cash) if state.portfolio else 0.0
        if self._key == "available_margin": return float(state.portfolio.available_margin) if state.portfolio else 0.0
        if self._key == "used_margin": return float(state.portfolio.used_margin) if state.portfolio else 0.0
        if self._key == "gross_exposure": return float(state.portfolio.gross_exposure) if state.portfolio else 0.0
        if self._key == "net_exposure": return float(state.portfolio.net_exposure) if state.portfolio else 0.0
        if self._key == "realized_pnl": return float(state.portfolio.realized_pnl) if state.portfolio else 0.0
        if self._key == "unrealized_pnl": return float(state.portfolio.unrealized_pnl) if state.portfolio else 0.0
        if self._key == "daily_pnl": return float(state.portfolio.daily_pnl) if state.portfolio else 0.0
        if self._key == "drawdown": return float(state.portfolio.drawdown) if state.portfolio else 0.0
        if self._key == "open_positions": return len(state.portfolio.positions) if state.portfolio else 0
        if self._key == "open_orders": return state.portfolio.open_orders if state.portfolio else 0
        if self._key == "orders_today": return state.portfolio.orders_today if state.portfolio else 0
        if self._key == "current_leverage": return float(state.portfolio.gross_exposure / state.portfolio.equity) if state.portfolio and state.portfolio.equity else 0.0
        if self._key == "expected_edge": return float(state.expected_edge)
        if self._key == "average_slippage": return float(state.average_slippage)
        if self._key == "average_latency": return float(state.average_latency)
        return getattr(state, self._key, "")

    @property
    def native_unit_of_measurement(self):
        if self._key in {"portfolio_equity", "available_cash", "realized_pnl", "unrealized_pnl", "daily_pnl"}:
            return UnitOfCurrency.EUR
        return None
