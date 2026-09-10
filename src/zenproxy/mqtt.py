import asyncio
import json
from collections.abc import Callable
from dataclasses import dataclass
from types import TracebackType
from typing import Any, Protocol

import aiomqtt
from loguru import logger

from zenproxy.aggregator import Aggregator
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
            # Skip the division for an unscaled property so an int input stays
            # an int (e.g. electricLevel=80 serializes as 80, not 80.0).
            state[name] = value if spec.scale == 1 else value / spec.scale
    return state


RECONNECT_DELAY_SECONDS = 5.0


class MqttPublishClient(Protocol):
    async def publish(
        self, topic: str, payload: str, *, qos: int = 0, retain: bool = False
    ) -> None: ...


class MqttClientContext(Protocol):
    async def __aenter__(self) -> MqttPublishClient: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> bool | None: ...


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
