from pathlib import Path

from zenproxy.config import MqttSettings, load_config

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.yaml"


def test_load_config_parses_example() -> None:
    config = load_config(EXAMPLE_CONFIG)

    assert config.virtual_sn == "ZENPROXY000001"
    assert len(config.devices) == 2
    assert config.devices[0].host == "192.168.1.101"
    assert config.server.port == 8080


def test_load_config_defaults_leader_rotation_when_omitted(tmp_path: Path) -> None:
    config_file = tmp_path / "zenproxy.yaml"
    config_file.write_text(
        "virtual_sn: ZENPROXY000001\n"
        "virtual_product: solarFlow800Plus\n"
        "devices:\n"
        "  - host: 192.168.1.101\n"
    )

    config = load_config(config_file)

    assert config.leader_rotation.enabled is True
    assert config.leader_rotation.soc_delta_percent == 10.0


def test_load_config_parses_leader_rotation_overrides(tmp_path: Path) -> None:
    config_file = tmp_path / "zenproxy.yaml"
    config_file.write_text(
        "virtual_sn: ZENPROXY000001\n"
        "virtual_product: solarFlow800Plus\n"
        "devices:\n"
        "  - host: 192.168.1.101\n"
        "leader_rotation:\n"
        "  enabled: false\n"
        "  soc_delta_percent: 15.0\n"
    )

    config = load_config(config_file)

    assert config.leader_rotation.enabled is False
    assert config.leader_rotation.soc_delta_percent == 15.0


def test_load_config_without_mqtt_section_leaves_mqtt_none(tmp_path: Path) -> None:
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "virtual_sn: TEST\n"
        "virtual_product: solarFlow800Plus\n"
        "devices:\n"
        "  - host: 10.0.0.1\n"
    )

    config = load_config(config_path)

    assert config.mqtt is None


def test_mqtt_settings_defaults() -> None:
    settings = MqttSettings(host="core-mosquitto")

    assert settings.port == 1883
    assert settings.discovery_prefix == "homeassistant"
    assert settings.username is None
    assert settings.password is None
