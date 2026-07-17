from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

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
    sn: str
    properties: Properties


class ReportResponse(BaseModel):
    sn: str
    devices: dict[str, Properties]


def create_app(config: AppConfig, http_client: httpx.AsyncClient | None = None) -> FastAPI:
    client = http_client or httpx.AsyncClient()
    owns_client = http_client is None
    clients = [DeviceClient(device, client) for device in config.devices]
    aggregator = Aggregator(clients)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_client:
            await client.aclose()

    app = FastAPI(title="zenproxy", lifespan=lifespan)

    @app.get("/properties/report")
    async def get_report() -> ReportResponse:
        devices = await aggregator.get_report()
        return ReportResponse(sn=config.virtual_device.sn, devices=devices)

    @app.post("/properties/write")
    async def write_properties(request: WriteRequest) -> WriteResponse:
        await aggregator.write_properties(request.properties)
        return WriteResponse(sn=config.virtual_device.sn, properties=request.properties)

    return app
