"""Exercise form serialization through the HTTP API used by the HA frontend."""

from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.components.config.config_entries import (
    ConfigManagerFlowIndexView,
    ConfigManagerFlowResourceView,
)
from homeassistant.setup import async_setup_component
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.evodnik.const import DOMAIN


@pytest.fixture
def endpoint_data(unused_tcp_port_factory):
    return {
        "target_host": "127.0.0.1",
        "target_port": unused_tcp_port_factory(),
        "listen_host": "127.0.0.1",
        "listen_port": unused_tcp_port_factory(),
        "mqtt_enabled": False,
        "mqtt_prefix": "synthetic/evodnik",
    }


@pytest.fixture
async def config_client(hass, hass_client):
    assert await async_setup_component(hass, "http", {})
    hass.http.register_view(ConfigManagerFlowIndexView(hass.config_entries.flow))
    hass.http.register_view(ConfigManagerFlowResourceView(hass.config_entries.flow))
    return await hass_client()


async def test_initial_form_is_available_through_http(config_client):
    response = await config_client.post("/api/config/config_entries/flow", json={"handler": DOMAIN})
    assert response.status == 200
    result = await response.json()
    assert result["type"] == "form"
    assert result["step_id"] == "user"
    assert {field["name"] for field in result["data_schema"]} == {
        "target_host",
        "target_port",
        "listen_host",
        "listen_port",
        "mqtt_enabled",
        "mqtt_prefix",
    }


async def test_reconfigure_form_is_available_through_http(hass, config_client, endpoint_data):
    entry = MockConfigEntry(domain=DOMAIN, data=endpoint_data)
    entry.add_to_hass(hass)
    response = await config_client.post(
        "/api/config/config_entries/flow",
        json={"handler": DOMAIN, "entry_id": entry.entry_id},
    )
    assert response.status == 200
    result = await response.json()
    assert result["step_id"] == "reconfigure"
    fields = {field["name"]: field for field in result["data_schema"]}
    assert fields["target_host"]["description"]["suggested_value"] == endpoint_data["target_host"]
    assert fields["listen_port"]["description"]["suggested_value"] == endpoint_data["listen_port"]


@pytest.mark.parametrize(
    ("key", "value", "error"),
    [
        ("target_host", "https://vendor.example.test", "invalid_host"),
        ("listen_host", "hostname.test", "invalid_bind_address"),
        ("mqtt_prefix", "bad/#", "invalid_mqtt_prefix"),
        ("mqtt_prefix", " / ", "invalid_mqtt_prefix"),
    ],
)
async def test_invalid_fields_return_a_renderable_form(
    config_client, endpoint_data, key, value, error
):
    response = await config_client.post("/api/config/config_entries/flow", json={"handler": DOMAIN})
    result = await response.json()
    response = await config_client.post(
        f"/api/config/config_entries/flow/{result['flow_id']}",
        json={**endpoint_data, key: value},
    )
    assert response.status == 200
    result = await response.json()
    assert result["type"] == "form"
    assert result["errors"] == {key: error}
    assert result["data_schema"]


async def test_http_setup_normalizes_and_saves_valid_fields(hass, config_client, endpoint_data):
    response = await config_client.post("/api/config/config_entries/flow", json={"handler": DOMAIN})
    result = await response.json()
    with patch("custom_components.evodnik.async_setup_entry", new=AsyncMock(return_value=True)):
        response = await config_client.post(
            f"/api/config/config_entries/flow/{result['flow_id']}",
            json={
                **endpoint_data,
                "target_host": " 127.0.0.1 ",
                "listen_host": " 127.0.0.1 ",
                "mqtt_prefix": " /synthetic/evodnik/ ",
            },
        )
        assert response.status == 200
        result = await response.json()
        assert result["type"] == "create_entry"
        entry = hass.config_entries.async_get_entry(result["result"]["entry_id"])
        assert entry.data == endpoint_data
        await hass.async_block_till_done()
