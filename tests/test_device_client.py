import json

import httpx
import pytest
import respx

from zenproxy.config import RealDevice
from zenproxy.device_client import DeviceClient

DEVICE = RealDevice(sn="ABC123", host="10.0.0.5", port=80)


@pytest.mark.asyncio
@respx.mock
async def test_get_report_returns_properties() -> None:
    respx.get("http://10.0.0.5:80/properties/report").mock(
        return_value=httpx.Response(200, json={"properties": {"outputHomePower": 100}})
    )
    async with httpx.AsyncClient() as http_client:
        client = DeviceClient(DEVICE, http_client)
        report = await client.get_report()

    assert report == {"outputHomePower": 100}


@pytest.mark.asyncio
@respx.mock
async def test_write_properties_sends_sn_and_body() -> None:
    route = respx.post("http://10.0.0.5:80/properties/write").mock(
        return_value=httpx.Response(200, json={})
    )
    async with httpx.AsyncClient() as http_client:
        client = DeviceClient(DEVICE, http_client)
        await client.write_properties({"outputLimit": 50})

    assert route.called
    request = route.calls.last.request
    body = json.loads(request.content)
    assert body == {"sn": "ABC123", "properties": {"outputLimit": 50}}
