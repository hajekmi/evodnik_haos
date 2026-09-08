"""Configure endpoints locally, without probing the vendor."""

import asyncio
import ipaddress
import re

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import selector

from .const import (
    CONF_LISTEN_HOST,
    CONF_LISTEN_PORT,
    CONF_METER_READING,
    CONF_MQTT_ENABLED,
    CONF_MQTT_PREFIX,
    CONF_TARGET_HOST,
    CONF_TARGET_PORT,
    DOMAIN,
)
from .proxy import DeviceUnavailable, QueueFull


def host(value: str) -> str:
    value = cv.string(value).strip()
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if len(value) > 253 or not re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value
        ):
            raise vol.Invalid("Enter an IP address or host name") from None
    return value


def bind_address(value: str) -> str:
    try:
        return str(ipaddress.ip_address(cv.string(value).strip()))
    except ValueError:
        raise vol.Invalid("Enter a local IP address") from None


def topic_prefix(value: str) -> str:
    value = cv.string(value).strip().strip("/")
    if (
        not value
        or len(value.encode("utf-8")) > 200
        or any(char in value for char in ("+", "#", "\x00"))
    ):
        raise vol.Invalid("Enter a nonempty MQTT prefix without wildcards")
    return value


@callback
def schema(values: dict) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_TARGET_HOST): host,
            vol.Required(CONF_TARGET_PORT): cv.port,
            vol.Required(CONF_LISTEN_PORT): cv.port,
            vol.Required(
                CONF_LISTEN_HOST, default=values.get(CONF_LISTEN_HOST, "0.0.0.0")
            ): bind_address,
            vol.Required(
                CONF_MQTT_ENABLED, default=values.get(CONF_MQTT_ENABLED, False)
            ): cv.boolean,
            vol.Required(
                CONF_MQTT_PREFIX, default=values.get(CONF_MQTT_PREFIX, "evodnik")
            ): topic_prefix,
        }
    )


class EvodnikConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Create or reconfigure a proxy; each entry owns a distinct listener."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return MeterOptionsFlow()

    async def _validate(self, data: dict, current=None) -> str | None:
        for entry in self._async_current_entries():
            if current and entry.entry_id == current.entry_id:
                continue
            if entry.data[CONF_LISTEN_PORT] == data[CONF_LISTEN_PORT]:
                return "listener_in_use"
            if (
                data[CONF_MQTT_ENABLED]
                and entry.data[CONF_MQTT_ENABLED]
                and entry.data[CONF_MQTT_PREFIX] == data[CONF_MQTT_PREFIX]
            ):
                return "mqtt_prefix_in_use"
        if data[CONF_TARGET_PORT] == data[CONF_LISTEN_PORT] and data[CONF_TARGET_HOST] in (
            data[CONF_LISTEN_HOST],
            "localhost",
            "127.0.0.1",
            "::1",
            "0.0.0.0",
            "::",
        ):
            return "proxy_loop"
        if current and all(
            data[key] == current.data[key] for key in (CONF_LISTEN_HOST, CONF_LISTEN_PORT)
        ):
            return None
        try:
            # Probe only the local bind. Runtime setup also handles a later bind race.
            server = await asyncio.start_server(
                lambda _reader, writer: writer.close(),
                data[CONF_LISTEN_HOST],
                data[CONF_LISTEN_PORT],
            )
        except OSError:
            return "listener_in_use"
        server.close()
        await server.wait_closed()
        return None

    async def _step(self, step: str, user_input: dict | None):
        current = self._get_reconfigure_entry() if step == "reconfigure" else None
        values = dict(current.data) if current else {}
        errors = {}
        if user_input is not None:
            values = user_input
            if error := await self._validate(user_input, current):
                errors["base"] = error
            elif current:
                return self.async_update_reload_and_abort(current, data=user_input)
            else:
                return self.async_create_entry(title="eVodnik", data=user_input)
        return self.async_show_form(
            step_id=step,
            data_schema=self.add_suggested_values_to_schema(schema(values), values),
            errors=errors,
        )

    async def async_step_user(self, user_input=None):
        return await self._step("user", user_input)

    async def async_step_reconfigure(self, user_input=None):
        return await self._step("reconfigure", user_input)


class MeterOptionsFlow(config_entries.OptionsFlow):
    """Set one local meter reading without restarting either TCP session."""

    async def async_step_init(self, user_input=None):
        if self.config_entry.state is not config_entries.ConfigEntryState.LOADED:
            return self.async_abort(reason="integration_unavailable")
        errors = {}
        if user_input is not None:
            value = user_input[CONF_METER_READING]
            if not float(value).is_integer() or value < 0:
                errors[CONF_METER_READING] = "invalid_reading"
            else:
                try:
                    await self.config_entry.runtime_data.set_meter_reading(int(value))
                except DeviceUnavailable, QueueFull, TimeoutError:
                    errors["base"] = "counter_unavailable"
                else:
                    return self.async_create_entry(title="", data=dict(self.config_entry.options))
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_METER_READING): selector.NumberSelector(
                        selector.NumberSelectorConfig(
                            min=0,
                            max=1_000_000_000_000,
                            step=1,
                            mode=selector.NumberSelectorMode.BOX,
                            unit_of_measurement="L",
                        )
                    ),
                }
            ),
            errors=errors,
        )
