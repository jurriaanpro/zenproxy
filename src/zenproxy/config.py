from pathlib import Path

import yaml
from pydantic import BaseModel


class RealDevice(BaseModel):
    host: str
    port: int = 80


class ServerSettings(BaseModel):
    host: str = "0.0.0.0"
    port: int = 8080
    poll_interval_seconds: float = 5.0


class AppConfig(BaseModel):
    virtual_sn: str
    devices: list[RealDevice]
    server: ServerSettings = ServerSettings()


def load_config(path: Path) -> AppConfig:
    raw = yaml.safe_load(path.read_text())
    return AppConfig.model_validate(raw)
