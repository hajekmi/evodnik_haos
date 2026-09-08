"""Allowlisted diagnostics omit all user configuration and operational values."""


async def async_get_config_entry_diagnostics(hass, entry) -> dict:
    runtime = entry.runtime_data
    state = runtime.proxy.state
    return {
        "device_connected": state.device_connected,
        "cloud_connected": state.cloud_connected,
        "valve_report_available": runtime.valve is not None,
        "counter_report_available": runtime.total_liters is not None,
        "meter_calibration_status": runtime.meter.status,
        "queue_size": runtime.proxy.queue_size,
        "dropped_messages": runtime.proxy.dropped_messages,
        "error": state.error,
    }
