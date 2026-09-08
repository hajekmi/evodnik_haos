# Validation and device handover

The automated suite uses synthetic loopback TCP peers and Home Assistant Core
2026.9.1 with Python 3.14.7. Version 0.3.0 passed **80 tests**, with
**96% statement coverage** across the integration; Ruff checks and formatting
also passed. The suite covers framing, CRC, escaping, exact forwarding, local and cloud
commands, response ownership, tag collisions, unconfirmed writes, connection
replacement, incomplete/corrupt replies, queue overflow, timeouts, discarded
stale messages, cloud recovery, UI configuration, reconfiguration, native
entities, MQTT discovery/control, outages/recovery, and listener/task cleanup.
An additional regression test verifies that the first counter snapshot is read
even when the host's uptime is shorter than the regular polling interval.

Config-flow HTTP tests cover opening setup and reconfiguration forms, returning
field validation errors, and saving normalized input through the actual API.
The initial-form test reproduced the HTTP 500 caused by unserializable custom
validators in version 0.2.1 before the form schema was fixed.

Meter tests cover zero and existing starting readings, calibration against a
fresh snapshot, duplicate messages, consumption during disconnects, persistence
across reloads, separate storage per entry, counter decreases, recalibration,
invalid/offline input, MQTT publication, and storage removal when an entry is deleted.

Home Assistant's test harness rejects lingering tasks and timers. The MQTT
fixture explicitly emits its simulated socket-close event because its mocked
Paho client does not emit that event at teardown. Live publication delivery
clears the RETAIN flag, matching broker behavior; separate replay tests send
retained messages explicitly.

MQTT tests create actual HA valve, sensor, and binary-sensor entities through
discovery. They exercise both valve actions against the loopback device,
calibrated counters, vendor commands without feedback, unconfirmed writes,
retained/invalid/offline commands, bursts, device replacement, cancellation of
an unsent command when the broker disconnects, custom discovery/birth/will
settings, stable IDs across data-prefix changes, and cleanup after disabling
or deleting an entry while MQTT is down. Pending cleanup survives registry
restart through HA's storage layer.

The local hassfest validator from the matching Core release passed with no
invalid integrations. The official HACS integration manifest and `hacs.json`
schemas and local brand-asset check also passed, using HACS source revision
`adb7d83e33d24325535fb43b8226572405143757`. The full HACS GitHub Action also checks remote
repository metadata and requires publication; a local schema check does not
replace that action.

The manifest links to `hajekmi/evodnik_haos` and names `@hajekmi` as code owner.
Before each publication, review the entire public tree and run the configured CI jobs. The private
research workspace must remain outside the repository and its Git history.

Before depending on local control, perform a controlled device trial:

1. Connect the device to the configured listener and verify forwarding, native
   reported state, and the raw liter counter against independent observations.
2. With the device connection established, make the vendor unavailable and
   verify that local polling and one close/open sequence still work. Separately
   test device startup while the vendor is already unavailable.
3. Verify a vendor-initiated valve command after recovery and confirm HA/MQTT
   follow the device report. Check vendor configuration and history operations.
4. Reconnect the device, reload the integration, and restart Core. Confirm that
   no previous command is replayed and unavailable state clears only after a
   new valid report.
5. Enable MQTT publication and check the **eVodnik MQTT** device under MQTT.
   Exercise one close/open sequence through its valve and verify the native
   entities agree. Disconnect/reconnect MQTT and verify no command is replayed,
   consumers reject expired retained state, and HA's configured will is honored.
6. Set a meter reading in liters, consume a known amount, and compare the
   adjusted reading with the physical meter. Verify it after restarting HA.

These are handover steps, not tests already performed on physical firmware.
Record live observations privately; publish only generalized protocol findings.
