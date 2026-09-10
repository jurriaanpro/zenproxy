from pathlib import Path

from zenproxy.config import MqttSettings, load_config

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.yaml"


def test_load_config_parses_example() -> None:
    config = load_config(EXAMPLE_CONFIG)

    assert config.virtual_sn == "ZENPROXY000001"
    assert len(config.devices) == 2
    assert config.devices[0].host == "192.168.1.101"
    assert config.server.port == 8080


def test_load_config_without_mqtt_section_leaves_mqtt_none() -> None:
    config = load_config(EXAMPLE_CONFIG)

    assert config.mqtt is None


def test_mqtt_settings_defaults() -> None:
    settings = MqttSettings(host="core-mosquitto")

    assert settings.port == 1883
    assert settings.discovery_prefix == "homeassistant"
    assert settings.username is None
    assert settings.password is None
