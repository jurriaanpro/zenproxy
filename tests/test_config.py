from pathlib import Path

from zenproxy.config import load_config

EXAMPLE_CONFIG = Path(__file__).parent.parent / "config.example.yaml"


def test_load_config_parses_example() -> None:
    config = load_config(EXAMPLE_CONFIG)

    assert config.virtual_sn == "ZENPROXY000001"
    assert len(config.devices) == 2
    assert config.devices[0].sn == "WOB1NHMAMXXXXX1"
    assert config.devices[0].host == "192.168.1.101"
    assert config.server.port == 8080
