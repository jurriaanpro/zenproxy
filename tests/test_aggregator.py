import pytest

from zenproxy.aggregator import Aggregator
from zenproxy.config import RealDevice
from zenproxy.device_client import DeviceClient, Properties


class FakeDeviceClient(DeviceClient):
    def __init__(self, sn: str, report: Properties | None = None, fail: bool = False) -> None:
        self.device = RealDevice(host="10.0.0.1")
        self.sn = sn
        self._report = report or {}
        self._fail = fail
        self.written: Properties | None = None

    async def get_report(self) -> Properties:
        if self._fail:
            raise ConnectionError("device unreachable")
        return self._report

    async def write_properties(self, properties: Properties) -> None:
        if self._fail:
            raise ConnectionError("device unreachable")
        self.written = properties


def make_client(sn: str, report: Properties | None = None, fail: bool = False) -> FakeDeviceClient:
    return FakeDeviceClient(sn, report=report, fail=fail)


@pytest.mark.asyncio
async def test_get_report_skips_unreachable_devices() -> None:
    ok = make_client("OK1", report={"outputHomePower": 100})
    broken = make_client("BROKEN1", fail=True)
    aggregator = Aggregator([ok, broken])

    reports = await aggregator.get_report()

    assert reports == {"OK1": {"outputHomePower": 100}}


@pytest.mark.asyncio
async def test_write_properties_splits_output_limit_evenly() -> None:
    a = make_client("A")
    b = make_client("B")
    c = make_client("C")
    aggregator = Aggregator([a, b, c])

    await aggregator.write_properties({"outputLimit": 300})

    assert a.written == {"outputLimit": 100}
    assert b.written == {"outputLimit": 100}
    assert c.written == {"outputLimit": 100}


@pytest.mark.asyncio
async def test_write_properties_passes_through_non_split_properties() -> None:
    a = make_client("A")
    b = make_client("B")
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 100, "acMode": 2})

    assert a.written == {"outputLimit": 50, "acMode": 2}
    assert b.written == {"outputLimit": 50, "acMode": 2}


@pytest.mark.asyncio
async def test_write_properties_skips_unreachable_devices() -> None:
    ok = make_client("OK1")
    broken = make_client("BROKEN1", fail=True)
    aggregator = Aggregator([ok, broken])

    await aggregator.write_properties({"outputLimit": 100})

    assert ok.written == {"outputLimit": 50}
