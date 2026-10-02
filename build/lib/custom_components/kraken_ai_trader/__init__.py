from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from .const import DOMAIN, PLATFORMS


@dataclass
class Runtime:
    authority: object
    store: object
    coordinator: object
    websocket: object | None = None


async def async_setup(hass, config) -> bool:
    """Register HA control services; services never submit orders directly."""
    import voluptuous as vol

    async def start(call) -> None:
        runtimes = hass.data.get(DOMAIN, {}).values()
        for runtime in list(runtimes):
            await runtime.authority.recover()

    async def stop(call) -> None:
        for runtime in list(hass.data.get(DOMAIN, {}).values()):
            runtime.authority.state.circuit_breaker = True
            runtime.authority.state.system_ready = False
            runtime.authority.state.system_state = "SAFE_STOP"
            runtime.authority.state.last_blocker = "SAFE_STOP"
            runtime.authority._event("SAFE_STOP", source="home_assistant_service")

    async def reconcile(call) -> None:
        for runtime in list(hass.data.get(DOMAIN, {}).values()):
            await runtime.authority.reconcile()

    async def recover(call) -> None:
        for runtime in list(hass.data.get(DOMAIN, {}).values()):
            await runtime.authority.recover()

    hass.services.async_register(DOMAIN, "start", start, schema=vol.Schema({}))
    hass.services.async_register(DOMAIN, "stop", stop, schema=vol.Schema({}))
    hass.services.async_register(DOMAIN, "reconcile", reconcile, schema=vol.Schema({}))
    hass.services.async_register(DOMAIN, "recover", recover, schema=vol.Schema({}))
    return True


async def async_setup_entry(hass, entry) -> bool:
    from .const import CONF_POLL_INTERVAL
    from .core.authority import CentralTradingAuthority
    from .core.gemini import GeminiClient
    from .core.kraken import KrakenGateway
    from .core.models import ProductType, SafetyLimits
    from .core.news import NewsEngine
    from .core.storage import Store
    from .core.websocket import WebsocketSupervisor
    from .coordinator import TraderCoordinator

    data = {**entry.data, **entry.options}
    limits = SafetyLimits(
        max_position_risk=Decimal(str(data["max_position_risk"])),
        max_gross_exposure=Decimal(str(data["max_gross_exposure"])),
        max_net_exposure=Decimal(str(data["max_net_exposure"])),
        max_margin=Decimal(str(data["max_margin"])),
        max_leverage=Decimal(str(data["max_leverage"])),
        max_positions=int(data["max_positions"]),
        daily_loss_limit=Decimal(str(data["daily_loss_limit"])),
        max_drawdown=Decimal(str(data["max_drawdown"])),
        cash_reserve=Decimal(str(data["cash_reserve"])),
        max_slippage=Decimal(str(data["max_slippage"])),
        max_orders_per_day=int(data["max_orders_per_day"]),
    )
    store = Store(hass.config.path("kraken_ai_trader", "trader.db"))
    gateway = KrakenGateway(
        data["api_key"],
        data["api_secret"],
        data.get("futures_api_key", ""),
        data.get("futures_api_secret", ""),
        account_currency=str(data.get("account_currency", "EUR")),
    )
    news = NewsEngine([x.strip() for x in str(data.get("news_urls", "")).splitlines() if x.strip()])
    gemini = GeminiClient(data.get("gemini_api_key"), data.get("gemini_model", "gemini-3.8-flash"))
    authority = CentralTradingAuthority(gateway, news, gemini, store, limits, data)
    await authority.startup()
    runtime = Runtime(authority, store, None, None)
    coordinator = TraderCoordinator(hass, runtime, int(data.get(CONF_POLL_INTERVAL, 60)))
    runtime.coordinator = coordinator

    async def ws_event(message):
        if message.get("channel") == "executions":
            try:
                await authority.handle_execution_event(message)
            except Exception as exc:  # noqa: BLE001 - malformed private events are isolated from WS health
                authority.state.metadata["last_ws_execution_error"] = type(exc).__name__
        code = str(message.get("event_code", message.get("channel", "message")))
        authority.state.metadata["last_ws_event"] = code
        if code == "PUBLIC_WS_DISCONNECTED":
            authority.state.market_data_healthy = False
            authority.state.circuit_breaker = True
            authority.state.last_blocker = "BLOCKED_MARKET_DATA"
        elif code == "FUTURES_WS_DISCONNECTED":
            authority.state.market_data_healthy = False
            authority.state.circuit_breaker = True
            authority.state.last_blocker = "BLOCKED_MARKET_DATA"
        elif code == "PRIVATE_WS_DISCONNECTED":
            authority.state.private_data_healthy = False
            authority.state.circuit_breaker = True
            authority.state.last_blocker = "BLOCKED_PRIVATE_DATA"
        elif code in {"PUBLIC_WS_CONNECTED", "PRIVATE_WS_CONNECTED", "FUTURES_WS_CONNECTED"}:
            if code.startswith("PUBLIC"):
                authority.state.market_data_healthy = True
            elif code == "PRIVATE_WS_CONNECTED":
                authority.state.private_data_healthy = True
            else:
                authority.state.market_data_healthy = True

    async def ws_gap():
        authority.state.circuit_breaker = True
        authority.state.last_blocker = "BLOCKED_RECONCILIATION"
        await authority.recover()

    websocket = WebsocketSupervisor(gateway, ws_event, ws_gap)
    spot_symbols = [
        instrument.symbol
        for instrument in gateway.instruments.values()
        if instrument.product_type != ProductType.FUTURES
    ]
    futures_symbols = [
        instrument.symbol
        for instrument in gateway.instruments.values()
        if instrument.product_type == ProductType.FUTURES
    ]
    runtime.websocket = websocket
    if authority.state.system_ready:
        await websocket.start(spot_symbols, futures_symbols)

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = runtime
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry) -> bool:
    runtime: Runtime = hass.data[DOMAIN].pop(entry.entry_id)
    if runtime.websocket:
        await runtime.websocket.stop()
    await runtime.authority.close()
    runtime.store.close()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
