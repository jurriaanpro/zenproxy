from typing import Any

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
