# MQTT Home Assistant Discovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Publish each real Zendure device's curated properties as read-only Home Assistant sensor entities over MQTT, using HA MQTT discovery, independent of zenproxy's existing HTTP API.

**Architecture:** A new `zenproxy/mqtt.py` module holds a curated property→HA-sensor mapping, pure functions to build discovery/state MQTT payloads, and an `MqttPublisher` class that owns a poll loop (reusing `Aggregator.get_report()`) with its own reconnect-with-backoff handling. `api.py`'s `lifespan` starts/stops it as a background asyncio task only when `config.mqtt` is set. The HA addon requests the `mqtt` Supervisor service and translates the injected environment variables into the `mqtt:` section of the rendered `config.yaml`.

**Tech Stack:** Python 3.14, `aiomqtt` (new dependency), existing FastAPI/httpx/pydantic/loguru stack, pytest/pytest-asyncio/respx for tests.

**Spec:** [docs/superpowers/specs/2026-09-10-mqtt-ha-discovery-design.md](../specs/2026-09-10-mqtt-ha-discovery-design.md)

## Global Constraints

- Python >=3.14, `mypy --strict` must pass on `src` and `tests`.
- `ruff check .` must pass (rules: E, F, I, UP, B; line-length 100).
- New dependency: `aiomqtt>=2.4.0` (asyncio-native MQTT client) in `pyproject.toml` `[project.dependencies]`.
- No writable/controllable MQTT entities this iteration — read-only sensors only.
- Only per-device entities are published over MQTT; the aggregated/virtual device is not (it's already fully visible via the HTTP API).
- Only the curated property set (below) is published — no generic "publish everything" mode.
- Curated property → HA sensor mapping (all scales confirmed against real hardware):

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

---

## Task 1: MQTT config model and dependency

**Files:**
- Modify: `pyproject.toml`
- Modify: `src/zenproxy/config.py`
- Test: `tests/test_config.py`

**Interfaces:**
- Produces: `zenproxy.config.MqttSettings` (fields: `host: str`, `port: int = 1883`, `username: str | None = None`, `password: str | None = None`, `discovery_prefix: str = "homeassistant"`); `zenproxy.config.AppConfig.mqtt: MqttSettings | None = None`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_config.py`:

```python
from zenproxy.config import MqttSettings, load_config

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.yaml"


def test_load_config_without_mqtt_section_leaves_mqtt_none() -> None:
    config = load_config(EXAMPLE_CONFIG)

    assert config.mqtt is None


def test_mqtt_settings_defaults() -> None:
    settings = MqttSettings(host="core-mosquitto")

    assert settings.port == 1883
    assert settings.discovery_prefix == "homeassistant"
    assert settings.username is None
    assert settings.password is None
```

(Keep the existing `test_load_config_parses_example` test and its `EXAMPLE_CONFIG` constant as-is — just add the `MqttSettings` import and the two new tests.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_config.py -v`
Expected: FAIL — `ImportError: cannot import name 'MqttSettings' from 'zenproxy.config'`

- [ ] **Step 3: Add `MqttSettings` and wire it into `AppConfig`**

In `src/zenproxy/config.py`, add after `ServerSettings`:

```python
class MqttSettings(BaseModel):
    host: str
    port: int = 1883
    username: str | None = None
    password: str | None = None
    discovery_prefix: str = "homeassistant"
```

Change `AppConfig` to add the field:

```python
class AppConfig(BaseModel):
    virtual_sn: str
    virtual_product: str
    devices: list[RealDevice]
    server: ServerSettings = ServerSettings()
    mqtt: MqttSettings | None = None
```

- [ ] **Step 4: Add the dependency**

In `pyproject.toml`, add `"aiomqtt>=2.4.0",` to `[project.dependencies]` (alphabetically, after `httpx`), then run:

Run: `uv sync`
Expected: `aiomqtt` and its transitive deps installed, `uv.lock` updated.

- [ ] **Step 5: Run tests to verify they pass**

Run: `uv run pytest tests/test_config.py -v`
Expected: PASS (all tests, including the pre-existing one)

- [ ] **Step 6: Type-check and lint**

Run: `uv run mypy . && uv run ruff check .`
Expected: no errors

- [ ] **Step 7: Commit**

```bash
git add pyproject.toml uv.lock src/zenproxy/config.py tests/test_config.py
git commit -m "Add MqttSettings config model and aiomqtt dependency"
```

---

## Task 2: Curated sensor table and pure MQTT payload builders

**Files:**
- Create: `src/zenproxy/mqtt.py`
- Test: `tests/test_mqtt.py`

**Interfaces:**
- Consumes: `zenproxy.device_client.Properties` (`dict[str, str | int | float | bool]`).
- Produces:
  - `zenproxy.mqtt.MQTT_SENSORS: dict[str, MqttSensorSpec]` (the curated table from Global Constraints)
  - `zenproxy.mqtt.BRIDGE_AVAILABILITY_TOPIC: str`, `zenproxy.mqtt.ONLINE: str`, `zenproxy.mqtt.OFFLINE: str`
  - `zenproxy.mqtt.state_topic(sn: str) -> str`
  - `zenproxy.mqtt.device_availability_topic(sn: str) -> str`
  - `zenproxy.mqtt.discovery_topic(discovery_prefix: str, sn: str, property_name: str) -> str`
  - `zenproxy.mqtt.discovery_payload(discovery_prefix: str, sn: str, property_name: str) -> dict[str, Any]`
  - `zenproxy.mqtt.state_payload(properties: Properties) -> dict[str, float]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_mqtt.py`:

```python
from zenproxy.mqtt import (
    BRIDGE_AVAILABILITY_TOPIC,
    MQTT_SENSORS,
    device_availability_topic,
    discovery_payload,
    discovery_topic,
    state_payload,
    state_topic,
)


def test_topics_are_namespaced_by_sn() -> None:
    assert state_topic("ABC123") == "zenproxy/ABC123/state"
    assert device_availability_topic("ABC123") == "zenproxy/ABC123/availability"
    assert discovery_topic("homeassistant", "ABC123", "electricLevel") == (
        "homeassistant/sensor/ABC123/electricLevel/config"
    )


def test_discovery_payload_includes_device_class_unit_and_availability() -> None:
    payload = discovery_payload("homeassistant", "ABC123", "electricLevel")

    assert payload["name"] == "Battery level"
    assert payload["unique_id"] == "zenproxy_ABC123_electricLevel"
    assert payload["state_topic"] == "zenproxy/ABC123/state"
    assert payload["value_template"] == "{{ value_json.electricLevel }}"
    assert payload["device_class"] == "battery"
    assert payload["unit_of_measurement"] == "%"
    assert payload["state_class"] == "measurement"
    assert payload["availability"] == [
        {"topic": BRIDGE_AVAILABILITY_TOPIC},
        {"topic": "zenproxy/ABC123/availability"},
    ]
    assert payload["availability_mode"] == "all"
    assert payload["device"] == {
        "identifiers": ["ABC123"],
        "name": "ABC123",
        "manufacturer": "Zendure",
    }
    assert "entity_category" not in payload


def test_discovery_payload_diagnostic_property_has_entity_category_and_no_device_class() -> None:
    payload = discovery_payload("homeassistant", "ABC123", "minSoc")

    assert payload["name"] == "Minimum SoC"
    assert payload["entity_category"] == "diagnostic"
    assert "device_class" not in payload
    assert payload["unit_of_measurement"] == "%"


def test_state_payload_scales_confirmed_hardware_values() -> None:
    properties = {"hyperTmp": 2991, "BatVolt": 5000, "minSoc": 100, "socSet": 800}

    payload = state_payload(properties)

    assert payload["hyperTmp"] == 29.91
    assert payload["BatVolt"] == 50.0
    assert payload["minSoc"] == 10.0
    assert payload["socSet"] == 80.0


def test_state_payload_omits_missing_or_non_numeric_properties() -> None:
    properties = {"electricLevel": 82, "acMode": "auto"}

    payload = state_payload(properties)

    assert payload == {"electricLevel": 82}


def test_state_payload_only_includes_curated_properties() -> None:
    properties = {name: 1 for name in MQTT_SENSORS} | {"someOtherFlag": 1}

    payload = state_payload(properties)

    assert set(payload) == set(MQTT_SENSORS)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mqtt.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'zenproxy.mqtt'`

- [ ] **Step 3: Implement `src/zenproxy/mqtt.py`**

```python
from dataclasses import dataclass
from typing import Any

from zenproxy.device_client import Properties

BRIDGE_AVAILABILITY_TOPIC = "zenproxy/bridge/availability"
ONLINE = "online"
OFFLINE = "offline"


@dataclass(frozen=True)
class MqttSensorSpec:
    name: str
    unit: str | None
    device_class: str | None = None
    scale: float = 1.0
    entity_category: str | None = None


# Curated property -> HA sensor mapping. All scales confirmed against real
# hardware: minSoc/socSet are tenths of a percent (see aggregator.py), and
# BatVolt/hyperTmp were confirmed in the same way -- e.g. a real device
# reporting hyperTmp=2991 is 29.91 degrees C.
MQTT_SENSORS: dict[str, MqttSensorSpec] = {
    "electricLevel": MqttSensorSpec(name="Battery level", unit="%", device_class="battery"),
    "outputHomePower": MqttSensorSpec(
        name="Output power", unit="W", device_class="power"
    ),
    "packInputPower": MqttSensorSpec(
        name="Pack input power", unit="W", device_class="power"
    ),
    "solarInputPower": MqttSensorSpec(
        name="Solar input power", unit="W", device_class="power"
    ),
    "solarPower1": MqttSensorSpec(
        name="Solar input power (MPPT 1)", unit="W", device_class="power"
    ),
    "solarPower2": MqttSensorSpec(
        name="Solar input power (MPPT 2)", unit="W", device_class="power"
    ),
    "BatVolt": MqttSensorSpec(
        name="Battery voltage", unit="V", device_class="voltage", scale=100
    ),
    "hyperTmp": MqttSensorSpec(
        name="Battery temperature", unit="°C", device_class="temperature", scale=100
    ),
    "rssi": MqttSensorSpec(
        name="Signal strength",
        unit="dBm",
        device_class="signal_strength",
        entity_category="diagnostic",
    ),
    "minSoc": MqttSensorSpec(name="Minimum SoC", unit="%", scale=10, entity_category="diagnostic"),
    "socSet": MqttSensorSpec(name="Target SoC", unit="%", scale=10, entity_category="diagnostic"),
}


def state_topic(sn: str) -> str:
    return f"zenproxy/{sn}/state"


def device_availability_topic(sn: str) -> str:
    return f"zenproxy/{sn}/availability"


def discovery_topic(discovery_prefix: str, sn: str, property_name: str) -> str:
    return f"{discovery_prefix}/sensor/{sn}/{property_name}/config"


def discovery_payload(discovery_prefix: str, sn: str, property_name: str) -> dict[str, Any]:
    spec = MQTT_SENSORS[property_name]
    payload: dict[str, Any] = {
        "name": spec.name,
        "unique_id": f"zenproxy_{sn}_{property_name}",
        "state_topic": state_topic(sn),
        "value_template": f"{{{{ value_json.{property_name} }}}}",
        "availability": [
            {"topic": BRIDGE_AVAILABILITY_TOPIC},
            {"topic": device_availability_topic(sn)},
        ],
        "availability_mode": "all",
        "device": {"identifiers": [sn], "name": sn, "manufacturer": "Zendure"},
    }
    if spec.unit is not None:
        payload["unit_of_measurement"] = spec.unit
    if spec.device_class is not None:
        payload["device_class"] = spec.device_class
        payload["state_class"] = "measurement"
    if spec.entity_category is not None:
        payload["entity_category"] = spec.entity_category
    return payload


def state_payload(properties: Properties) -> dict[str, float]:
    state: dict[str, float] = {}
    for name, spec in MQTT_SENSORS.items():
        value = properties.get(name)
        if isinstance(value, int | float):
            state[name] = value / spec.scale
    return state
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mqtt.py -v`
Expected: PASS

- [ ] **Step 5: Type-check and lint**

Run: `uv run mypy . && uv run ruff check .`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/zenproxy/mqtt.py tests/test_mqtt.py
git commit -m "Add curated MQTT sensor table and discovery/state payload builders"
```

---

## Task 3: `MqttPublisher` poll loop with reconnect

**Files:**
- Modify: `src/zenproxy/mqtt.py`
- Test: `tests/test_mqtt.py`

**Interfaces:**
- Consumes: `zenproxy.aggregator.Aggregator.get_report() -> dict[str, Properties]` (already exists); `zenproxy.mqtt.{state_topic, device_availability_topic, discovery_topic, discovery_payload, state_payload, MQTT_SENSORS, BRIDGE_AVAILABILITY_TOPIC, ONLINE, OFFLINE}` from Task 2.
- Produces: `zenproxy.mqtt.MqttPublishClient` (Protocol: `async def publish(self, topic: str, payload: str, *, qos: int = 0, retain: bool = False) -> None`); `zenproxy.mqtt.MqttClientContext` (Protocol: `async def __aenter__(self) -> MqttPublishClient`, `async def __aexit__(self, *args: object) -> bool | None`); `zenproxy.mqtt.ClientFactory = Callable[[], MqttClientContext]`; `zenproxy.mqtt.MqttPublisher` with constructor `(aggregator: Aggregator, discovery_prefix: str, poll_interval_seconds: float, client_factory: ClientFactory, reconnect_delay_seconds: float = 5.0)` and `async def run(self) -> None`.

Note: `MqttClientContext` is a local `Protocol`, not `contextlib.AbstractAsyncContextManager` — the latter is a real ABC, not structurally matched by mypy, which would make the test double below fail strict type-checking without inheriting from it. A local `Protocol` matches structurally, so `aiomqtt.Client` (Task 4) and the test's `FakeClientContext` both satisfy it without any inheritance relationship.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mqtt.py`:

```python
import asyncio
from dataclasses import dataclass, field
from typing import Any

import aiomqtt
import pytest

from zenproxy.aggregator import Aggregator
from zenproxy.config import RealDevice
from zenproxy.device_client import DeviceClient
from zenproxy.mqtt import MqttPublisher


class FakeDeviceClient(DeviceClient):
    def __init__(self, sn: str, report: dict[str, Any] | None = None, fail: bool = False) -> None:
        self.device = RealDevice(host="10.0.0.1")
        self.sn = sn
        self._report = report or {}
        self._fail = fail

    async def get_report(self) -> dict[str, Any]:
        if self._fail:
            raise ConnectionError("device unreachable")
        return self._report


@dataclass
class PublishCall:
    topic: str
    payload: str
    retain: bool


@dataclass
class FakeMqttClient:
    published: list[PublishCall] = field(default_factory=list)

    async def publish(
        self, topic: str, payload: str, *, qos: int = 0, retain: bool = False
    ) -> None:
        self.published.append(PublishCall(topic, payload, retain))


class FakeClientContext:
    """Async context manager wrapping a FakeMqttClient, or raising aiomqtt.MqttError."""

    def __init__(self, client: FakeMqttClient | None = None, raise_on_enter: bool = False) -> None:
        self._client = client
        self._raise_on_enter = raise_on_enter

    async def __aenter__(self) -> FakeMqttClient:
        if self._raise_on_enter:
            raise aiomqtt.MqttError("connection refused")
        assert self._client is not None
        return self._client

    async def __aexit__(self, *args: object) -> None:
        return None


@pytest.mark.asyncio
async def test_poll_once_publishes_discovery_once_then_state_and_availability() -> None:
    client = make_client_with_report("ABC123", {"electricLevel": 80})
    aggregator = Aggregator([client])
    fake_client = FakeMqttClient()
    publisher = MqttPublisher(
        aggregator=aggregator,
        discovery_prefix="homeassistant",
        poll_interval_seconds=0,
        client_factory=lambda: FakeClientContext(fake_client),
    )

    await publisher._poll_once(fake_client)
    await publisher._poll_once(fake_client)

    discovery_calls = [c for c in fake_client.published if "/config" in c.topic]
    state_calls = [c for c in fake_client.published if c.topic == "zenproxy/ABC123/state"]
    availability_calls = [
        c for c in fake_client.published if c.topic == "zenproxy/ABC123/availability"
    ]
    assert len(discovery_calls) == len(MQTT_SENSORS)  # only published once
    assert all(c.retain for c in discovery_calls)
    assert len(state_calls) == 2
    assert state_calls[0].payload == '{"electricLevel": 80}'
    assert not state_calls[0].retain
    assert len(availability_calls) == 2
    assert all(c.payload == "online" and c.retain for c in availability_calls)


@pytest.mark.asyncio
async def test_poll_once_marks_previously_discovered_device_offline_when_unreachable() -> None:
    client = make_client_with_report("ABC123", {"electricLevel": 80})
    aggregator = Aggregator([client])
    fake_client = FakeMqttClient()
    publisher = MqttPublisher(
        aggregator=aggregator,
        discovery_prefix="homeassistant",
        poll_interval_seconds=0,
        client_factory=lambda: FakeClientContext(fake_client),
    )
    await publisher._poll_once(fake_client)  # device discovered while reachable
    client._fail = True

    await publisher._poll_once(fake_client)

    availability_calls = [
        c for c in fake_client.published if c.topic == "zenproxy/ABC123/availability"
    ]
    assert availability_calls[-1].payload == "offline"


@pytest.mark.asyncio
async def test_run_reconnects_after_mqtt_error_and_publishes_offline_on_cancel() -> None:
    client = make_client_with_report("ABC123", {"electricLevel": 80})
    aggregator = Aggregator([client])
    fake_client = FakeMqttClient()
    contexts = iter([FakeClientContext(raise_on_enter=True), FakeClientContext(fake_client)])
    publisher = MqttPublisher(
        aggregator=aggregator,
        discovery_prefix="homeassistant",
        poll_interval_seconds=0,
        client_factory=lambda: next(contexts),
        reconnect_delay_seconds=0,
    )

    task = asyncio.create_task(publisher.run())
    for _ in range(200):
        if any(c.topic == BRIDGE_AVAILABILITY_TOPIC for c in fake_client.published):
            break
        await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    bridge_calls = [c for c in fake_client.published if c.topic == BRIDGE_AVAILABILITY_TOPIC]
    assert bridge_calls[0].payload == "online"
    assert bridge_calls[-1].payload == "offline"


def make_client_with_report(sn: str, report: dict[str, Any]) -> FakeDeviceClient:
    return FakeDeviceClient(sn, report=report)
```

(Add `import aiomqtt`, `import asyncio`, `import pytest`, `from dataclasses import dataclass, field`, `from typing import Any`, `from zenproxy.aggregator import Aggregator`, `from zenproxy.config import RealDevice`, `from zenproxy.device_client import DeviceClient` to the top of `tests/test_mqtt.py` alongside the existing imports from Task 2.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_mqtt.py -v`
Expected: FAIL — `ImportError: cannot import name 'MqttPublisher' from 'zenproxy.mqtt'`

- [ ] **Step 3: Implement `MqttPublisher` in `src/zenproxy/mqtt.py`**

Add to `src/zenproxy/mqtt.py` (alongside the existing imports, add `import asyncio`, `import json`, `from collections.abc import Callable`, `from typing import Protocol`, `import aiomqtt`, `from loguru import logger`, `from zenproxy.aggregator import Aggregator`):

```python
RECONNECT_DELAY_SECONDS = 5.0


class MqttPublishClient(Protocol):
    async def publish(
        self, topic: str, payload: str, *, qos: int = 0, retain: bool = False
    ) -> None: ...


class MqttClientContext(Protocol):
    async def __aenter__(self) -> MqttPublishClient: ...
    async def __aexit__(self, *args: object) -> bool | None: ...


ClientFactory = Callable[[], MqttClientContext]


class MqttPublisher:
    """Polls all configured devices and publishes their curated properties to
    MQTT with HA discovery, independent of zenproxy's HTTP API.
    """

    def __init__(
        self,
        aggregator: Aggregator,
        discovery_prefix: str,
        poll_interval_seconds: float,
        client_factory: ClientFactory,
        reconnect_delay_seconds: float = RECONNECT_DELAY_SECONDS,
    ) -> None:
        self._aggregator = aggregator
        self._discovery_prefix = discovery_prefix
        self._poll_interval_seconds = poll_interval_seconds
        self._client_factory = client_factory
        self._reconnect_delay_seconds = reconnect_delay_seconds
        self._discovered: set[str] = set()

    async def run(self) -> None:
        while True:
            try:
                async with self._client_factory() as client:
                    await client.publish(BRIDGE_AVAILABILITY_TOPIC, ONLINE, retain=True)
                    try:
                        while True:
                            await self._poll_once(client)
                            await asyncio.sleep(self._poll_interval_seconds)
                    except asyncio.CancelledError:
                        await client.publish(BRIDGE_AVAILABILITY_TOPIC, OFFLINE, retain=True)
                        raise
            except aiomqtt.MqttError as error:
                logger.warning(
                    "mqtt connection error: {}, reconnecting in {}s",
                    error,
                    self._reconnect_delay_seconds,
                )
                await asyncio.sleep(self._reconnect_delay_seconds)

    async def _poll_once(self, client: MqttPublishClient) -> None:
        reports = await self._aggregator.get_report()

        for sn, properties in reports.items():
            if sn not in self._discovered:
                await self._publish_discovery(client, sn)
                self._discovered.add(sn)
            await client.publish(state_topic(sn), json.dumps(state_payload(properties)))
            await client.publish(device_availability_topic(sn), ONLINE, retain=True)

        for sn in self._discovered - set(reports):
            await client.publish(device_availability_topic(sn), OFFLINE, retain=True)

    async def _publish_discovery(self, client: MqttPublishClient, sn: str) -> None:
        for property_name in MQTT_SENSORS:
            await client.publish(
                discovery_topic(self._discovery_prefix, sn, property_name),
                json.dumps(discovery_payload(self._discovery_prefix, sn, property_name)),
                retain=True,
            )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_mqtt.py -v`
Expected: PASS

- [ ] **Step 5: Type-check and lint**

Run: `uv run mypy . && uv run ruff check .`
Expected: no errors

- [ ] **Step 6: Commit**

```bash
git add src/zenproxy/mqtt.py tests/test_mqtt.py
git commit -m "Add MqttPublisher poll loop with reconnect and per-device availability"
```

---

## Task 4: Wire `MqttPublisher` into the FastAPI app lifespan

**Files:**
- Modify: `src/zenproxy/api.py`
- Test: `tests/test_api.py`

**Interfaces:**
- Consumes: `zenproxy.mqtt.{MqttPublisher, BRIDGE_AVAILABILITY_TOPIC, OFFLINE}`; `zenproxy.config.AppConfig.mqtt`.
- Produces: no new public API — `create_app` behavior only changes when `config.mqtt` is set.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_api.py`:

```python
import asyncio

from zenproxy.config import MqttSettings


class RecordingPublisher:
    instances: list["RecordingPublisher"] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        self.cancelled = False
        RecordingPublisher.instances.append(self)

    async def run(self) -> None:
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def test_mqtt_publisher_starts_and_stops_when_mqtt_configured(monkeypatch) -> None:
    RecordingPublisher.instances = []
    monkeypatch.setattr("zenproxy.api.MqttPublisher", RecordingPublisher)
    config = CONFIG.model_copy(update={"mqtt": MqttSettings(host="core-mosquitto")})

    with TestClient(create_app(config)):
        assert len(RecordingPublisher.instances) == 1
        assert RecordingPublisher.instances[0].cancelled is False

    assert RecordingPublisher.instances[0].cancelled is True


def test_mqtt_publisher_not_started_when_mqtt_not_configured(monkeypatch) -> None:
    RecordingPublisher.instances = []
    monkeypatch.setattr("zenproxy.api.MqttPublisher", RecordingPublisher)

    with TestClient(create_app(CONFIG)):
        pass

    assert RecordingPublisher.instances == []
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_api.py -v`
Expected: FAIL — `AttributeError: <module 'zenproxy.api'> does not have the attribute 'MqttPublisher'`

- [ ] **Step 3: Wire it into `create_app`**

In `src/zenproxy/api.py`, add imports:

```python
import contextlib

import aiomqtt

from zenproxy.mqtt import BRIDGE_AVAILABILITY_TOPIC, OFFLINE, MqttPublisher
```

Change `create_app` (the `lifespan` function and the body around it):

```python
def create_app(config: AppConfig, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    client = http_client or httpx.AsyncClient()
    owns_client = http_client is None
    clients = [DeviceClient(device, client) for device in config.devices]
    aggregator = Aggregator(clients)
    message_ids = itertools.count(1)

    def _mqtt_client_factory() -> aiomqtt.Client:
        assert config.mqtt is not None
        return aiomqtt.Client(
            hostname=config.mqtt.host,
            port=config.mqtt.port,
            username=config.mqtt.username,
            password=config.mqtt.password,
            will=aiomqtt.Will(topic=BRIDGE_AVAILABILITY_TOPIC, payload=OFFLINE, retain=True),
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        mqtt_task: asyncio.Task[None] | None = None
        if config.mqtt is not None:
            publisher = MqttPublisher(
                aggregator=aggregator,
                discovery_prefix=config.mqtt.discovery_prefix,
                poll_interval_seconds=config.server.poll_interval_seconds,
                client_factory=_mqtt_client_factory,
            )
            mqtt_task = asyncio.create_task(publisher.run())

        yield

        if mqtt_task is not None:
            mqtt_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await mqtt_task
        if owns_client:
            await client.aclose()
```

Add `import asyncio` to the top-level imports (alongside the existing `itertools`, `time` imports).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_api.py -v`
Expected: PASS (all tests, including pre-existing ones)

- [ ] **Step 5: Type-check and lint**

Run: `uv run mypy . && uv run ruff check .`
Expected: no errors

- [ ] **Step 6: Run the full test suite**

Run: `uv run pytest`
Expected: all tests PASS

- [ ] **Step 7: Commit**

```bash
git add src/zenproxy/api.py tests/test_api.py
git commit -m "Start MqttPublisher as a background task when mqtt is configured"
```

---

## Task 5: HA addon MQTT service wiring

**Files:**
- Modify: `ha_addon/zenproxy/config.yaml`
- Modify: `ha_addon/zenproxy/run.sh`

**Interfaces:**
- Consumes: Supervisor-injected environment variables `MQTT_HOST`, `MQTT_PORT`, `MQTT_USERNAME`, `MQTT_PASSWORD` (present only when the addon declares the `mqtt` service and a broker is available).
- Produces: an `mqtt:` block in the rendered `/data/zenproxy.yaml`, matching `zenproxy.config.MqttSettings`'s shape, present only when `MQTT_HOST` is set.

There is no existing shell-test infrastructure in this repo (`run.sh` has none today) — this task is verified manually as described in Step 3.

- [ ] **Step 1: Declare the MQTT service in the addon manifest**

In `ha_addon/zenproxy/config.yaml`, add after `ports_description`:

```yaml
services:
  - mqtt:want
```

Bump the version at the top of the same file from `"0.6.0"` to `"0.7.0"`.

- [ ] **Step 2: Render the `mqtt:` block in `run.sh`**

Change `ha_addon/zenproxy/run.sh` to:

```bash
#!/usr/bin/env bash
set -euo pipefail

OPTIONS_FILE=/data/options.json
CONFIG_FILE=/data/zenproxy.yaml

virtual_sn=$(jq -r '.virtual_sn' "$OPTIONS_FILE")
virtual_product=$(jq -r '.virtual_product' "$OPTIONS_FILE")
port=$(jq -r '.port' "$OPTIONS_FILE")

{
    echo "virtual_sn: ${virtual_sn}"
    echo "virtual_product: ${virtual_product}"
    echo
    echo "devices:"
    jq -r '.devices[] | "  - host: \(.host)\n    port: \(.port)"' "$OPTIONS_FILE"
    echo
    echo "server:"
    echo "  host: 0.0.0.0"
    echo "  port: ${port}"
    if [ -n "${MQTT_HOST:-}" ]; then
        echo
        echo "mqtt:"
        echo "  host: ${MQTT_HOST}"
        echo "  port: ${MQTT_PORT:-1883}"
        if [ -n "${MQTT_USERNAME:-}" ]; then
            echo "  username: ${MQTT_USERNAME}"
        fi
        if [ -n "${MQTT_PASSWORD:-}" ]; then
            echo "  password: ${MQTT_PASSWORD}"
        fi
    fi
} > "$CONFIG_FILE"

exec /app/.venv/bin/zenproxy --config "$CONFIG_FILE"
```

(Same interpolation style as the existing `virtual_sn`/`devices` fields — no new escaping introduced beyond what the file already does.)

- [ ] **Step 3: Manually verify the rendering logic**

`services: - mqtt:want` and the Supervisor env-var injection can't be exercised without a running Supervisor, so verify the shell logic in isolation:

Run:
```bash
cd /tmp && cat > options.json <<'EOF'
{"virtual_sn": "ZENPROXY000001", "virtual_product": "solarFlow800Plus", "port": 8080, "devices": [{"host": "192.168.1.101", "port": 80}]}
EOF
OPTIONS_FILE=/tmp/options.json CONFIG_FILE=/tmp/zenproxy.yaml MQTT_HOST=core-mosquitto MQTT_PORT=1883 MQTT_USERNAME=addons MQTT_PASSWORD=secret bash -c '
source <(sed -n "/^virtual_sn=/,/^exec/p" /Users/jurriaan/Documents/Personal/zenproxy/ha_addon/zenproxy/run.sh | sed "\$d")
'
cat /tmp/zenproxy.yaml
```
Expected output includes an `mqtt:` block with `host: core-mosquitto`, `port: 1883`, `username: addons`, `password: secret`. Then re-run with `MQTT_HOST` unset and confirm no `mqtt:` block appears. Clean up: `rm /tmp/options.json /tmp/zenproxy.yaml`.

- [ ] **Step 4: Commit**

```bash
git add ha_addon/zenproxy/config.yaml ha_addon/zenproxy/run.sh
git commit -m "Request MQTT Supervisor service and render mqtt config in addon; bump to 0.7.0"
```

---

## Task 6: Documentation

**Files:**
- Modify: `config.example.yaml`
- Modify: `README.md`

- [ ] **Step 1: Document the config option**

Add to `config.example.yaml`, after the `server:` block:

```yaml
# Optional: publish each real device's properties to Home Assistant over
# MQTT discovery (read-only sensors, independent of the HTTP API above).
# Omit this section entirely to leave MQTT publishing disabled.
mqtt:
  host: 192.168.1.10
  port: 1883
  username: mqtt-user
  password: mqtt-pass
  discovery_prefix: homeassistant
```

- [ ] **Step 2: Document the feature and design decisions in `README.md`**

Add a new subsection after "## Home Assistant addon" and before "## Design decisions":

```markdown
## MQTT (optional)

zenproxy can publish each *real* device's properties to Home Assistant over
MQTT discovery, as read-only sensor entities — separate from, and in
addition to, the HTTP API. This exists because the HTTP API deliberately
hides the individual devices behind the virtual one; MQTT is how you get
visibility into per-device state (e.g. each battery's own charge level)
without giving that up.

Configure it by adding an `mqtt:` section to `config.yaml` (see
[`config.example.yaml`](config.example.yaml)). In the Home Assistant addon,
this is automatic instead: the addon requests the `mqtt` Supervisor service,
and if a broker (e.g. the Mosquitto broker addon) is available, its
connection details are used with no manual configuration.

Only a curated set of properties is published (see `MQTT_SENSORS` in
[`src/zenproxy/mqtt.py`](src/zenproxy/mqtt.py)), not every raw device field —
this keeps each sensor mapped to the right Home Assistant `device_class` and
unit instead of showing up as an unlabeled number.
```

Add to the "## Design decisions" list, at the end:

```markdown
- **MQTT publishes per-device state, not the aggregated device.** The HTTP
  API already fully exposes the virtual/aggregated device; the only thing it
  doesn't expose is the individual real devices (that's the point of the
  proxy for automations). So MQTT is the other direction: per-device
  visibility only, read-only, for dashboards rather than automations. See
  `src/zenproxy/mqtt.py`.
```

- [ ] **Step 3: Commit**

```bash
git add config.example.yaml README.md
git commit -m "Document MQTT config option and design rationale"
```
