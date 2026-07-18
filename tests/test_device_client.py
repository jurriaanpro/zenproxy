import json

import httpx
import pytest
import respx

from zenproxy.config import RealDevice
from zenproxy.device_client import DeviceClient

DEVICE = RealDevice(host="10.0.0.5", port=80)


@pytest.mark.asyncio
@respx.mock
async def test_get_report_returns_properties_and_caches_sn() -> None:
    respx.get("http://10.0.0.5:80/properties/report").mock(
        return_value=httpx.Response(
            200, json={"sn": "ABC123", "properties": {"outputHomePower": 100}}
        )
    )
    async with httpx.AsyncClient() as http_client:
        client = DeviceClient(DEVICE, http_client)
        report = await client.get_report()

    assert report == {"outputHomePower": 100}
    assert client.sn == "ABC123"


@pytest.mark.asyncio
@respx.mock
async def test_get_report_caches_pack_data() -> None:
    respx.get("http://10.0.0.5:80/properties/report").mock(
        return_value=httpx.Response(
            200,
            json={
                "sn": "ABC123",
                "properties": {},
                "packData": [{"sn": "PACK1", "packType": 70}],
            },
        )
    )
    async with httpx.AsyncClient() as http_client:
        client = DeviceClient(DEVICE, http_client)
        await client.get_report()

    assert client.pack_data == [{"sn": "PACK1", "packType": 70}]


@pytest.mark.asyncio
@respx.mock
async def test_write_properties_resolves_sn_via_report_first() -> None:
    respx.get("http://10.0.0.5:80/properties/report").mock(
        return_value=httpx.Response(200, json={"sn": "ABC123", "properties": {}})
    )
    write_route = respx.post("http://10.0.0.5:80/properties/write").mock(
        return_value=httpx.Response(200, json={})
    )
    async with httpx.AsyncClient() as http_client:
        client = DeviceClient(DEVICE, http_client)
        await client.write_properties({"outputLimit": 50})

    assert write_route.called
    body = json.loads(write_route.calls.last.request.content)
    assert body == {"sn": "ABC123", "properties": {"outputLimit": 50}}


@pytest.mark.asyncio
@respx.mock
async def test_write_properties_raises_if_sn_never_resolves() -> None:
    respx.get("http://10.0.0.5:80/properties/report").mock(
        return_value=httpx.Response(200, json={"properties": {}})
    )
    async with httpx.AsyncClient() as http_client:
        client = DeviceClient(DEVICE, http_client)
        with pytest.raises(RuntimeError):
            await client.write_properties({"outputLimit": 50})
