# eVodnik Local Proxy

![Local water valve](custom_components/evodnik/brand/icon.png)

A HACS custom integration that provides local eVodnik water valve control and
proxies vendor TCP traffic. Local controls and telemetry continue when the
vendor server is unavailable. Unrecognized valid transactions pass through
unchanged. This is an independent community project.

Requires **Home Assistant Core 2026.9 or newer**; development tests use 2026.9.1.
Firmware acceptance still needs a controlled device test; see
[protocol assumptions](docs/protocol.md#verification-boundary).

## Install

1. In HACS, open **Custom repositories**, add
   `https://github.com/hajekmi/evodnik_haos`,
   and select **Integration**. Download **eVodnik Local Proxy**.
2. Restart Home Assistant Core.
3. Open **Settings → Devices & services → Add integration → eVodnik**.
4. Enter the vendor target host, target TCP port, and local listener port.
   These fields have no deployment defaults. The listener address defaults to
   `0.0.0.0` (all IPv4 interfaces); an explicit local IPv4 or IPv6 address is
   supported. Supply your own network redirection separately.
5. Optionally enable MQTT publication and choose a unique topic prefix. MQTT
   must be configured separately in Home Assistant; its credentials are reused.

Saving configuration does not contact the vendor. A port conflict is reported
in the form. Each entry accepts one device; another connection replaces the
previous session. Each entry must use a different listener port and MQTT prefix.
Use the integration entry's **Reconfigure** action to change these settings.

HACS installs Python files into the existing **Home Assistant Core container**
on HAOS. There is no additional container or add-on. Restarting Core, reloading
the integration, or changing configuration interrupts both TCP sessions. The
device must reconnect; previous commands are never replayed.

## Controls and telemetry

| Entity | Meaning |
|---|---|
| Water valve | Native open/close actions and the device's reported state |
| Water total | Raw lower 32-bit counter in liters, assuming 1 pulse = 1 liter |
| Water meter | Your starting reading plus subsequent consumption, in liters |
| Device connected | The current local session has produced a valid response |
| Cloud connected | The separate vendor TCP connection is established |
| Last response | HA timestamp of the last valid device response |

Local commands and their replies stay between HA and the device. Vendor
commands also work and update the same state. One queue serializes both origins;
there is no persistent preference for a previous local command. Vendor-side
limits, drip settings, clock writes, and history requests remain vendor-managed.

After a valve write, the proxy reads status before confirming the requested
state. Until then, the valve is unknown. An acknowledgment alone is insufficient.
An unconfirmed local command returns an error; writes are never retried
automatically. HA disables the native control while the device is unavailable.
The reported state is **not proven to represent mechanically verified position**.

The proxy requests status about every 45 seconds and a counter snapshot about
every 5 minutes, using fresh forwarded responses to avoid redundant polling.
Valve reports expire after 120 seconds; counter readings expire after 600
seconds. The counter includes an `observed_at` attribute. Counter rollover,
resets, and upper bits remain unverified, so this version does not expose a
`total_increasing` statistics class or instantaneous flow rate.

## Set a starting reading

With the device connected, open **Settings → Devices & services → eVodnik →
Configure** and enter **Water meter reading (L)**. Enter whole liters, including
zero if you want to count usage from now. For a reading in cubic meters,
multiply by 1,000 before entering it.

The **Water meter** sensor (normally `sensor.evodnik_water_meter`) then shows the
entered reading plus the change in the device counter. For example, entering
12,000 L and consuming another 7 L produces 12,007 L. Repeated messages do not
increase the reading. The original **Water total** sensor keeps showing the
raw device counter.

Setting the reading obtains a fresh counter snapshot. It does not restart the
proxy or change anything on the device or vendor server. Offline calibration
is rejected; an earlier setting remains intact. The offset and last observed
counter are stored locally in HA and survive reloads and restarts. Consumption
while HA is disconnected is included when a fresh counter arrives, provided
the device counter has continued normally. Deleting the integration entry
also deletes its stored meter setting.

Before you set a reading, the sensor is unknown. Any observed counter decrease
(including a reset or rollover) makes it unknown again, with
`calibration_status: counter_reset`. Set the reading again through **Configure**;
the integration does not guess missing consumption. A reset that occurs and
catches up past the last observed count while HA is offline cannot be detected
from the counter alone. A disconnected or stale device makes the sensor unavailable.

## MQTT mirror

Publication is optional and uses HA's existing MQTT connection. Broker failure
does not stop the listener or native controls. No MQTT command topic or discovery
entity is created. There is no command feedback loop.

For prefix `evodnik`, both topics use QoS 1 and retained messages:

| Topic | Payload |
|---|---|
| `evodnik/availability` | `online` only with a current valve report; otherwise `offline` |
| `evodnik/state` | JSON report with availability, valve, timestamps, counter, and connection flags |

Synthetic example:

```json
{
  "available": true,
  "valve": "open",
  "observed_at": "2030-01-01T12:00:00+00:00",
  "valid_until": "2030-01-01T12:02:00+00:00",
  "water_total_liters": 42,
  "water_meter_liters": 12000,
  "meter_calibration_status": "ready",
  "counter_observed_at": "2030-01-01T12:00:00+00:00",
  "device_connected": true,
  "cloud_connected": false
}
```

On MQTT reconnect, the mirror first invalidates earlier retained state and
requests a fresh device snapshot. It becomes available only after a device
report observed since that connection. Normal unload publishes offline/unknown.

MQTT consumers must check `available`, `valid_until`, and the HA MQTT connection's
configured birth/will topic (normally `homeassistant/status`). A process crash or
power loss cannot publish a final offline message on the integration's topic;
retained `online` alone is insufficient. This integration does not alter HA's
broker credentials, will configuration, or other integrations' topics.

`water_meter_liters` is `null` until the meter is configured and while its
reading cannot be verified. `meter_calibration_status` is `not_configured`,
`ready`, or `counter_reset`; it describes the stored calibration, not freshness.

## Troubleshooting and privacy

If **Device connected** is off, verify that the device reconnects to your
configured listener. If only **Cloud connected** is off, local controls should
still work. Invalid frames, an unexpected response, or an ambiguous transaction
timeout retire the device session; the device must reconnect. Cloud connection
failures use background retries with exponential backoff, capped at 60 seconds.

Download integration diagnostics for connection flags, queue depth, dropped
message count, and a generic error code. Diagnostics omit endpoints, ports,
topic prefixes, counters, meter offsets, valve position, and timestamps. Integration logs do
not include payloads or endpoints. Configuration is stored locally in HA config
entries, and MQTT reports contain your operational data; keep HA backups and
broker exports private. The listener uses the device's existing unauthenticated
TCP protocol and should be reachable only by the intended device.

The public project contains synthetic fixtures only. Keep captures, real
deployment values, device identities, and private analysis outside this project.

## Development and publication

```bash
python3.14 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest -q
```

Tests run actual loopback device/cloud simulators and HA lifecycle tests, with
external socket connections blocked. No physical device or vendor endpoint is
used. CI adds official HACS and hassfest validation.

The public project is [hajekmi/evodnik_haos](https://github.com/hajekmi/evodnik_haos).
For a fork, update the project links and code owner locally:

```bash
python3 tools/set_repository.py https://github.com/OWNER/REPOSITORY --codeowner USER
```

This updates only local metadata. Keep **this directory** as the repository
root and review all proposed files for private data before committing. Do not
publish the surrounding research workspace. Keep GitHub issues enabled, set a
description and relevant repository topics, and run CI before a release.

See [validation and device handover](docs/validation.md) for the verification scope.

References: [HACS integration layout](https://www.hacs.xyz/docs/publish/integration/),
[custom repositories](https://www.hacs.xyz/docs/faq/custom_repositories/),
[HA valve entities](https://developers.home-assistant.io/docs/core/entity/valve/).
