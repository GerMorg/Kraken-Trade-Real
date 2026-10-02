from __future__ import annotations

import voluptuous as vol
from homeassistant import config_entries

from .const import (
    CONF_ACCOUNT_CURRENCY,
    CONF_API_KEY,
    CONF_API_SECRET,
    CONF_CASH_RESERVE,
    CONF_DAILY_LOSS,
    CONF_ENABLED,
    CONF_FUTURES_API_KEY,
    CONF_FUTURES_API_SECRET,
    CONF_GEMINI_API_KEY,
    CONF_GEMINI_MODEL,
    CONF_HISTORY_BACKFILL,
    CONF_LIVE_ENABLED,
    CONF_MAX_DRAWDOWN,
    CONF_MAX_GROSS_EXPOSURE,
    CONF_MAX_LEVERAGE,
    CONF_MAX_MARGIN,
    CONF_MAX_NET_EXPOSURE,
    CONF_MAX_ORDERS_DAY,
    CONF_MAX_POSITION_RISK,
    CONF_MAX_SLIPPAGE,
    CONF_MAX_POSITIONS,
    CONF_MIN_CONFIDENCE,
    CONF_MIN_EDGE,
    CONF_MIN_LIQUIDITY,
    CONF_MAX_SPREAD,
    CONF_NEWS_ENABLED,
    CONF_NEWS_URLS,
    CONF_POLL_INTERVAL,
    DEFAULTS,
    DOMAIN,
    NAME,
)


class KrakenAITraderConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        if user_input is not None:
            self._account = user_input
            return await self.async_step_ai()
        return self.async_show_form(step_id="user", data_schema=vol.Schema({
            vol.Required(CONF_API_KEY): str,
            vol.Required(CONF_ACCOUNT_CURRENCY, default=DEFAULTS[CONF_ACCOUNT_CURRENCY]): str,
            vol.Required(CONF_API_SECRET): str,
            vol.Optional(CONF_FUTURES_API_KEY, default=""): str,
            vol.Optional(CONF_FUTURES_API_SECRET, default=""): str,
            vol.Required(CONF_ENABLED, default=False): bool,
            vol.Required(CONF_LIVE_ENABLED, default=False): bool,
        }))

    async def async_step_ai(self, user_input=None):
        if user_input is not None:
            self._ai = user_input
            return await self.async_step_risk()
        return self.async_show_form(step_id="ai", data_schema=vol.Schema({
            vol.Optional(CONF_GEMINI_API_KEY, default=""): str,
            vol.Required(CONF_GEMINI_MODEL, default=DEFAULTS[CONF_GEMINI_MODEL]): str,
            vol.Required(CONF_NEWS_ENABLED, default=True): bool,
            vol.Required(CONF_NEWS_URLS, default=DEFAULTS[CONF_NEWS_URLS]): str,
        }))

    async def async_step_risk(self, user_input=None):
        if user_input is not None:
            data = {**self._account, **self._ai, **user_input}
            return self.async_create_entry(title=NAME, data=data)
        schema = vol.Schema({
            vol.Required(CONF_MAX_POSITION_RISK, default=DEFAULTS[CONF_MAX_POSITION_RISK]): vol.All(vol.Coerce(float), vol.Range(min=0.001, max=0.2)),
            vol.Required(CONF_MAX_GROSS_EXPOSURE, default=DEFAULTS[CONF_MAX_GROSS_EXPOSURE]): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=5.0)),
            vol.Required(CONF_MAX_NET_EXPOSURE, default=DEFAULTS[CONF_MAX_NET_EXPOSURE]): vol.All(vol.Coerce(float), vol.Range(min=0.1, max=5.0)),
            vol.Required(CONF_MAX_MARGIN, default=DEFAULTS[CONF_MAX_MARGIN]): vol.All(vol.Coerce(float), vol.Range(min=0.05, max=0.9)),
            vol.Required(CONF_MAX_LEVERAGE, default=DEFAULTS[CONF_MAX_LEVERAGE]): vol.All(vol.Coerce(float), vol.Range(min=1.0, max=10.0)),
            vol.Required(CONF_MAX_POSITIONS, default=DEFAULTS[CONF_MAX_POSITIONS]): vol.All(vol.Coerce(int), vol.Range(min=1, max=100)),
            vol.Required(CONF_DAILY_LOSS, default=DEFAULTS[CONF_DAILY_LOSS]): vol.All(vol.Coerce(float), vol.Range(min=0.01, max=0.5)),
            vol.Required(CONF_MAX_DRAWDOWN, default=DEFAULTS[CONF_MAX_DRAWDOWN]): vol.All(vol.Coerce(float), vol.Range(min=0.02, max=0.5)),
            vol.Required(CONF_CASH_RESERVE, default=DEFAULTS[CONF_CASH_RESERVE]): vol.Coerce(float),
            vol.Required(CONF_MAX_SLIPPAGE, default=DEFAULTS[CONF_MAX_SLIPPAGE]): vol.All(vol.Coerce(float), vol.Range(min=0.0001, max=0.02)),
            vol.Required(CONF_MAX_ORDERS_DAY, default=DEFAULTS[CONF_MAX_ORDERS_DAY]): vol.All(vol.Coerce(int), vol.Range(min=1, max=1000)),
            vol.Required(CONF_MIN_LIQUIDITY, default=DEFAULTS[CONF_MIN_LIQUIDITY]): vol.Coerce(float),
            vol.Required(CONF_MAX_SPREAD, default=DEFAULTS[CONF_MAX_SPREAD]): vol.Coerce(float),
            vol.Required(CONF_MIN_EDGE, default=DEFAULTS[CONF_MIN_EDGE]): vol.Coerce(float),
            vol.Required(CONF_MIN_CONFIDENCE, default=DEFAULTS[CONF_MIN_CONFIDENCE]): vol.Coerce(float),
            vol.Required(CONF_POLL_INTERVAL, default=DEFAULTS[CONF_POLL_INTERVAL]): vol.All(vol.Coerce(int), vol.Range(min=15, max=3600)),
            vol.Required(CONF_HISTORY_BACKFILL, default=DEFAULTS[CONF_HISTORY_BACKFILL]): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        })
        return self.async_show_form(step_id="risk", data_schema=schema)

    @staticmethod
    def async_get_options_flow(config_entry):
        return KrakenAITraderOptionsFlow(config_entry)


class KrakenAITraderOptionsFlow(config_entries.OptionsFlow):
    def __init__(self, config_entry) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input=None):
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)
        current = {**self.config_entry.options}
        schema = vol.Schema({
            vol.Required(CONF_ENABLED, default=current.get(CONF_ENABLED, DEFAULTS[CONF_ENABLED])): bool,
            vol.Required(CONF_LIVE_ENABLED, default=current.get(CONF_LIVE_ENABLED, DEFAULTS[CONF_LIVE_ENABLED])): bool,
            vol.Required(CONF_MIN_LIQUIDITY, default=current.get(CONF_MIN_LIQUIDITY, DEFAULTS[CONF_MIN_LIQUIDITY])): vol.Coerce(float),
            vol.Required(CONF_MAX_SPREAD, default=current.get(CONF_MAX_SPREAD, DEFAULTS[CONF_MAX_SPREAD])): vol.Coerce(float),
            vol.Required(CONF_MIN_EDGE, default=current.get(CONF_MIN_EDGE, DEFAULTS[CONF_MIN_EDGE])): vol.Coerce(float),
            vol.Required(CONF_MIN_CONFIDENCE, default=current.get(CONF_MIN_CONFIDENCE, DEFAULTS[CONF_MIN_CONFIDENCE])): vol.Coerce(float),
            vol.Required(CONF_POLL_INTERVAL, default=current.get(CONF_POLL_INTERVAL, DEFAULTS[CONF_POLL_INTERVAL])): vol.Coerce(int),
        })
        return self.async_show_form(step_id="init", data_schema=schema)
