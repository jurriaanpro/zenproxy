import json

import httpx
import respx
from fastapi.testclient import TestClient

from zenproxy.api import create_app
from zenproxy.config import AppConfig, RealDevice

CONFIG = AppConfig(
    virtual_sn="VIRTUAL1",
    devices=[
        RealDevice(host="10.0.0.1", port=80),
        RealDevice(host="10.0.0.2", port=80),
    ],
)

PACK1 = {"sn": "PACK1", "packType": 70}
PACK2 = {"sn": "PACK2", "packType": 70}


@respx.mock
def test_get_properties_report_returns_aggregated_properties() -> None:
    respx.get("http://10.0.0.1:80/properties/report").mock(
        return_value=httpx.Response(
            200,
            json={
                "sn": "DEV1",
                "properties": {"outputHomePower": 100, "electricLevel": 80},
                "packData": [PACK1],
            },
        )
    )
    respx.get("http://10.0.0.2:80/properties/report").mock(
        return_value=httpx.Response(
            200,
            json={
                "sn": "DEV2",
                "properties": {"outputHomePower": 50, "electricLevel": 20},
                "packData": [PACK2],
            },
        )
    )

    with TestClient(create_app(CONFIG)) as test_client:
        response = test_client.get("/properties/report")

    assert response.status_code == 200
    assert response.json() == {
        "sn": "VIRTUAL1",
        "properties": {"outputHomePower": 150, "electricLevel": 50.0},
        "packData": [PACK1, PACK2],
    }


@respx.mock
def test_get_devices_returns_raw_per_device_breakdown() -> None:
    respx.get("http://10.0.0.1:80/properties/report").mock(
        return_value=httpx.Response(
            200, json={"sn": "DEV1", "properties": {"outputHomePower": 100}}
        )
    )
    respx.get("http://10.0.0.2:80/properties/report").mock(
        return_value=httpx.Response(
            200, json={"sn": "DEV2", "properties": {"outputHomePower": 200}}
        )
    )

    with TestClient(create_app(CONFIG)) as test_client:
        response = test_client.get("/devices")

    assert response.status_code == 200
    assert response.json() == {
        "sn": "VIRTUAL1",
        "devices": {
            "DEV1": {"outputHomePower": 100},
            "DEV2": {"outputHomePower": 200},
        },
    }


@respx.mock
def test_post_properties_write_splits_output_limit() -> None:
    respx.get("http://10.0.0.1:80/properties/report").mock(
        return_value=httpx.Response(200, json={"sn": "DEV1", "properties": {}})
    )
    respx.get("http://10.0.0.2:80/properties/report").mock(
        return_value=httpx.Response(200, json={"sn": "DEV2", "properties": {}})
    )
    dev1_route = respx.post("http://10.0.0.1:80/properties/write").mock(
        return_value=httpx.Response(200, json={})
    )
    dev2_route = respx.post("http://10.0.0.2:80/properties/write").mock(
        return_value=httpx.Response(200, json={})
    )

    with TestClient(create_app(CONFIG)) as test_client:
        response = test_client.post(
            "/properties/write",
            json={"sn": "VIRTUAL1", "properties": {"outputLimit": 200}},
        )

    assert response.status_code == 200
    assert response.json() == {"sn": "VIRTUAL1", "properties": {"outputLimit": 200}}
    assert json.loads(dev1_route.calls.last.request.content) == {
        "sn": "DEV1",
        "properties": {"outputLimit": 100.0},
    }
    assert json.loads(dev2_route.calls.last.request.content) == {
        "sn": "DEV2",
        "properties": {"outputLimit": 100.0},
    }
