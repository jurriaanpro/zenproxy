from typing import Any

import httpx

from zenproxy.config import RealDevice

PropertyValue = str | int | float | bool
Properties = dict[str, PropertyValue]


class DeviceClient:
    def __init__(self, device: RealDevice, client: httpx.AsyncClient) -> None:
        self.device = device
        self._client = client

    def _base_url(self) -> str:
        return f"http://{self.device.host}:{self.device.port}"

    async def get_report(self) -> Properties:
        response = await self._client.get(f"{self._base_url()}/properties/report")
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        properties: Properties = body.get("properties", body)
        return properties

    async def write_properties(self, properties: Properties) -> None:
        response = await self._client.post(
            f"{self._base_url()}/properties/write",
            json={"sn": self.device.sn, "properties": properties},
        )
        response.raise_for_status()
