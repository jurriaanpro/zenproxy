from typing import Any

import httpx

from zenproxy.config import RealDevice

PropertyValue = str | int | float | bool
Properties = dict[str, PropertyValue]


class DeviceClient:
    def __init__(self, device: RealDevice, client: httpx.AsyncClient) -> None:
        self.device = device
        self._client = client
        self.sn: str | None = None
        self.pack_data: list[dict[str, Any]] = []

    def _base_url(self) -> str:
        return f"http://{self.device.host}:{self.device.port}"

    @property
    def label(self) -> str:
        return self.sn or f"{self.device.host}:{self.device.port}"

    async def get_report(self) -> Properties:
        response = await self._client.get(f"{self._base_url()}/properties/report")
        response.raise_for_status()
        body: dict[str, Any] = response.json()
        if "sn" in body:
            self.sn = body["sn"]
        self.pack_data = body.get("packData", [])
        properties: Properties = body.get("properties", body)
        return properties

    async def write_properties(self, properties: Properties) -> None:
        if self.sn is None:
            await self.get_report()
        if self.sn is None:
            raise RuntimeError(f"device at {self._base_url()} did not report a serial number")

        response = await self._client.post(
            f"{self._base_url()}/properties/write",
            json={"sn": self.sn, "properties": properties},
        )
        response.raise_for_status()
