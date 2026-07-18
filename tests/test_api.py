import json

import httpx
import respx
from fastapi.testclient import TestClient

from zenproxy.api import create_app
from zenproxy.config import AppConfig, RealDevice

CONFIG = AppConfig(
    virtual_sn="VIRTUAL1",
    devices=[
        RealDevice(sn="DEV1", host="10.0.0.1", port=80),
        RealDevice(sn="DEV2", host="10.0.0.2", port=80),
    ],
)


@respx.mock
def test_get_properties_report_returns_per_device_breakdown() -> None:
    respx.get("http://10.0.0.1:80/properties/report").mock(
        return_value=httpx.Response(200, json={"properties": {"outputHomePower": 100}})
    )
    respx.get("http://10.0.0.2:80/properties/report").mock(
        return_value=httpx.Response(200, json={"properties": {"outputHomePower": 200}})
    )

    with TestClient(create_app(CONFIG)) as test_client:
        response = test_client.get("/properties/report")

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
