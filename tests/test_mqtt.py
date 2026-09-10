import asyncio
from dataclasses import dataclass, field
from typing import Any

import aiomqtt
import pytest

from zenproxy.aggregator import Aggregator
from zenproxy.config import RealDevice
from zenproxy.device_client import DeviceClient
from zenproxy.mqtt import (
    BRIDGE_AVAILABILITY_TOPIC,
    MQTT_SENSORS,
    MqttPublisher,
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
    properties: dict[str, Any] = {"hyperTmp": 2991, "BatVolt": 5000, "minSoc": 100, "socSet": 800}

    payload = state_payload(properties)

    assert payload["hyperTmp"] == 29.91
    assert payload["BatVolt"] == 50.0
    assert payload["minSoc"] == 10.0
    assert payload["socSet"] == 80.0


def test_state_payload_omits_missing_or_non_numeric_properties() -> None:
    properties: dict[str, Any] = {"electricLevel": 82, "acMode": "auto"}

    payload = state_payload(properties)

    assert payload == {"electricLevel": 82}


def test_state_payload_only_includes_curated_properties() -> None:
    properties: dict[str, Any] = {name: 1 for name in MQTT_SENSORS} | {"someOtherFlag": 1}

    payload = state_payload(properties)

    assert set(payload) == set(MQTT_SENSORS)


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
