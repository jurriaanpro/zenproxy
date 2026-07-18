import itertools
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

import httpx
from fastapi import FastAPI
from pydantic import BaseModel

from zenproxy.aggregator import Aggregator
from zenproxy.config import AppConfig
from zenproxy.device_client import DeviceClient, Properties


class WriteRequest(BaseModel):
    sn: str
    properties: Properties


class WriteResponse(BaseModel):
    timestamp: int
    messageId: int
    success: bool
    code: int
    sn: str


class ReportResponse(BaseModel):
    sn: str
    properties: Properties
    packData: list[dict[str, Any]]


class DevicesResponse(BaseModel):
    sn: str
    devices: dict[str, Properties]


def create_app(config: AppConfig, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    client = http_client or httpx.AsyncClient()
    owns_client = http_client is None
    clients = [DeviceClient(device, client) for device in config.devices]
    aggregator = Aggregator(clients)
    message_ids = itertools.count(1)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_client:
            await client.aclose()

    app = FastAPI(title="zenproxy", lifespan=lifespan)

    @app.get("/properties/report")
    async def get_report() -> ReportResponse:
        properties, pack_data = await aggregator.get_aggregated_report()
        return ReportResponse(sn=config.virtual_sn, properties=properties, packData=pack_data)

    @app.get("/devices")
    async def get_devices() -> DevicesResponse:
        devices = await aggregator.get_report()
        return DevicesResponse(sn=config.virtual_sn, devices=devices)

    @app.post("/properties/write")
    async def write_properties(request: WriteRequest) -> WriteResponse:
        success = await aggregator.write_properties(request.properties)
        return WriteResponse(
            timestamp=int(time.time()),
            messageId=next(message_ids),
            success=success,
            code=200 if success else 500,
            sn=config.virtual_sn,
        )

    return app
