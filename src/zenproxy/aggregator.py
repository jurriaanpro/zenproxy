import asyncio
import logging

from zenproxy.device_client import DeviceClient, Properties

logger = logging.getLogger(__name__)

SPLIT_PROPERTY = "outputLimit"


class Aggregator:
    def __init__(self, clients: list[DeviceClient]) -> None:
        self.clients = clients

    async def get_report(self) -> dict[str, Properties]:
        results = await asyncio.gather(
            *(client.get_report() for client in self.clients),
            return_exceptions=True,
        )
        reports: dict[str, Properties] = {}
        for client, result in zip(self.clients, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("device %s unreachable: %s", client.device.sn, result)
                continue
            reports[client.device.sn] = result
        return reports

    async def write_properties(self, properties: Properties) -> None:
        per_device = dict(properties)
        if SPLIT_PROPERTY in properties and self.clients:
            total = properties[SPLIT_PROPERTY]
            assert isinstance(total, int | float)
            per_device[SPLIT_PROPERTY] = total / len(self.clients)

        results = await asyncio.gather(
            *(client.write_properties(per_device) for client in self.clients),
            return_exceptions=True,
        )
        for client, result in zip(self.clients, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("device %s write failed: %s", client.device.sn, result)
