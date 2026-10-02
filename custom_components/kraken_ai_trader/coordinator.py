from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN


class TraderCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, runtime, interval: int = 60) -> None:
        self.runtime = runtime
        super().__init__(hass, logging.getLogger(__name__), name=DOMAIN, update_interval=timedelta(seconds=max(15, interval)))

    async def _async_update_data(self):
        try:
            return await self.runtime.authority.cycle()
        except Exception as exc:
            raise UpdateFailed(str(exc)) from exc
