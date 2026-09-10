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
