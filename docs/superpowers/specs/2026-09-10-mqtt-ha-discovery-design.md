# MQTT Home Assistant discovery for per-device entities

## Problem

zenproxy hides the individual real Zendure devices behind a single virtual
device on its HTTP API (`/properties/report`, `/properties/write`) — that's
the point of the proxy for automations. But it means Home Assistant has no
visibility into the individual real devices (e.g. per-battery state of
charge), which is still useful to see on a dashboard. This adds an MQTT
publisher that exposes each real device as its own set of read-only HA
sensor entities via [MQTT discovery][mqtt-discovery], independent of the
existing HTTP API.

[mqtt-discovery]: https://www.home-assistant.io/integrations/mqtt/#mqtt-discovery

## Non-goals

- Writable/controllable entities (e.g. commanding `outputLimit` via MQTT).
  Read-only visibility only, for this iteration.
- Publishing the aggregated/virtual device over MQTT — it's already fully
  visible via the HTTP API; MQTT is specifically for the per-device data the
  HTTP API doesn't expose.
- A generic "publish everything" mode. A curated, mapped set of properties
  keeps entities clean (right device_class/unit) and avoids depending on the
  full shape of whatever a given device firmware happens to report.

## Configuration

### `zenproxy` config schema

New model in [`src/zenproxy/config.py`](../../../src/zenproxy/config.py):

```python
class MqttSettings(BaseModel):
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    discovery_prefix: str = "homeassistant"
```

Added to `AppConfig` as `mqtt: MqttSettings | None = None`. When absent, the
MQTT publisher is disabled entirely — no behavior change for anyone not
using it, including the existing `config.example.yaml` / standalone usage.

### HA addon

The addon requests the MQTT service from Supervisor rather than asking the
user to enter broker details manually:

- [`ha_addon/zenproxy/config.yaml`](../../../ha_addon/zenproxy/config.yaml)
  gains `services: ["mqtt:want"]` (`want`, not `need` — the addon must keep
  working without a broker installed).
- [`ha_addon/zenproxy/run.sh`](../../../ha_addon/zenproxy/run.sh) checks
  `bashio::services.available "mqtt"`; if available, it reads
  `bashio::services mqtt "host"` / `"port"` / `"username"` / `"password"`
  and appends an `mqtt:` block to the rendered `config.yaml`. If not
  available, no `mqtt:` block is written and the feature stays off.

## Dependency

Add `aiomqtt` (asyncio-native MQTT client, actively maintained successor to
`asyncio-mqtt`) to `[project.dependencies]` in `pyproject.toml`.

## Lifecycle

No background task exists today — `create_app`'s endpoints poll devices
on-demand per HTTP request. This adds the first one: when `config.mqtt` is
set, `create_app`'s `lifespan` context manager starts an
`MqttPublisher.run()` asyncio task alongside serving HTTP, and cancels it
(awaiting a final "offline" publish) on shutdown. This task is independent
of request handling — an MQTT/broker problem never blocks or fails HTTP
requests, and vice versa.

The publisher loop:

1. Connect to the broker (via `aiomqtt.Client` as an async context manager).
2. Publish the bridge "online" availability.
3. Loop forever on `config.server.poll_interval_seconds`:
   - Call `Aggregator.get_report()` (already gathers all devices
     concurrently, tolerates unreachable ones, returns `dict[sn, Properties]`
     keyed by `client.label`, which is the sn once known).
   - For any device not seen before, publish its discovery configs
     (retained).
   - For every device: publish its state JSON (not retained) and its
     per-device availability ("online" if it answered this cycle, "offline"
     if not).
4. On any connection error, back off (5s) and reconnect from step 1 —
   rediscovery configs get republished (retained, so idempotent; HA dedupes
   identical retained payloads).

This mirrors the existing pattern in `Aggregator.get_report()` /
`_gather_states()`: gather concurrently, log and skip unreachable devices,
never let one bad device break the cycle for the others.

## MQTT topics

All topics are namespaced under `zenproxy/` for state/availability, and
under `{discovery_prefix}/` (default `homeassistant`) for discovery, per the
MQTT discovery convention:

- Discovery (retained): `{discovery_prefix}/sensor/{sn}/{property}/config`
- State (not retained), one JSON payload per device per poll:
  `zenproxy/{sn}/state`, e.g. `{"electricLevel": 82, "hyperTmp": 2991, ...}`
- Per-device availability (retained): `zenproxy/{sn}/availability`
  (`online`/`offline`)
- Bridge availability (retained, set as the MQTT client's Will):
  `zenproxy/bridge/availability` (`online`/`offline`)

Each discovery config uses `value_template` to pick its property out of the
shared state-topic JSON (one publish per device per cycle, not one per
sensor), and lists both availability topics with `availability_mode: "all"`
— a sensor reads as unavailable if either the whole proxy or that specific
device is down.

Discovery config shape (per property):

```json
{
  "name": "<friendly name, e.g. \"Battery level\">",
  "unique_id": "zenproxy_{sn}_{property}",
  "state_topic": "zenproxy/{sn}/state",
  "value_template": "{{ value_json.{property} }}",
  "availability": [
    {"topic": "zenproxy/bridge/availability"},
    {"topic": "zenproxy/{sn}/availability"}
  ],
  "availability_mode": "all",
  "device": {"identifiers": ["{sn}"], "name": "{sn}", "manufacturer": "Zendure"},
  "device_class": "...",
  "unit_of_measurement": "...",
  "state_class": "measurement",
  "entity_category": "diagnostic"
}
```

`device_class`/`unit_of_measurement`/`entity_category` are per-property, per
the table below; omitted when not applicable.

## Curated property set

Defined as a mapping in a new `src/zenproxy/mqtt.py`, each entry giving the
scale (raw value is divided by this before publishing), HA `device_class`,
and unit. All scales below are confirmed against real hardware (the
`minSoc`/`socSet` scale was already confirmed in `aggregator.py`; `BatVolt`
and `hyperTmp` were confirmed in this session — e.g. `hyperTmp: 2991` is
29.91°C).

| property | name | device_class | unit | scale | entity_category |
|---|---|---|---|---|---|
| `electricLevel` | Battery level | battery | % | 1 | — |
| `outputHomePower` | Output power | power | W | 1 | — |
| `packInputPower` | Pack input power | power | W | 1 | — |
| `solarInputPower` | Solar input power | power | W | 1 | — |
| `solarPower1` | Solar input power (MPPT 1) | power | W | 1 | — |
| `solarPower2` | Solar input power (MPPT 2) | power | W | 1 | — |
| `BatVolt` | Battery voltage | voltage | V | 100 | — |
| `hyperTmp` | Battery temperature | temperature | °C | 100 | — |
| `rssi` | Signal strength | signal_strength | dBm | 1 | diagnostic |
| `minSoc` | Minimum SoC | — | % | 10 | diagnostic |
| `socSet` | Target SoC | — | % | 10 | diagnostic |

`name` is the HA entity name; combined with the device grouping (all of one
device's sensors share `device.identifiers: [sn]`), the entity shows up in
HA as e.g. "ABC123 Battery level".

A device missing a given property in its report simply doesn't get that
sensor's state updated that cycle (same tolerance the aggregator already
has for missing/non-numeric values).

## Error handling

- Broker unreachable at startup or dropped mid-run: the publisher task
  retries with a fixed 5s backoff, logging via `loguru` like the rest of the
  app. It never raises out to the ASGI lifespan or affects the HTTP server.
- A single unreachable device: already handled by `Aggregator.get_report()`
  — it's just absent from that cycle's results, and its availability topic
  is set to `offline`.
- Malformed/missing broker credentials (`mqtt:` present but e.g. host
  unreachable/auth fails): same retry-with-backoff path; logged clearly so
  it's diagnosable from addon logs.

## Testing

New `tests/test_mqtt.py`:

- Pure functions for building discovery/state payloads (topic, JSON body,
  retain flag) tested directly against sample `Properties` dicts — no I/O,
  no broker needed.
- A fake MQTT client (same style as the `make_client` test doubles already
  used in `tests/test_aggregator.py`) to verify `MqttPublisher`'s poll loop
  calls the right sequence of publishes (discovery once per new device,
  state + availability every cycle) without a real broker or `aiomqtt`
  network code.
- Scale/unit table covered by a parametrized test asserting each property's
  raw→published value (e.g. `hyperTmp: 2991` → published `29.91`).

`aiomqtt`'s own connection handling (TCP, TLS, reconnect internals) is not
re-tested — only zenproxy's usage of it (what gets published, when, with
what payload).
