from typing import Any

import pytest

from zenproxy.aggregator import Aggregator
from zenproxy.config import RealDevice
from zenproxy.device_client import DeviceClient, Properties

PACK_1920WH = [{"packType": 70}]


class FakeDeviceClient(DeviceClient):
    def __init__(
        self,
        sn: str,
        report: Properties | None = None,
        fail: bool = False,
        pack_data: list[dict[str, Any]] | None = None,
    ) -> None:
        self.device = RealDevice(host="10.0.0.1")
        self.sn = sn
        self._report = report or {}
        self._fail = fail
        self.pack_data = pack_data or []
        self.written: Properties | None = None

    async def get_report(self) -> Properties:
        if self._fail:
            raise ConnectionError("device unreachable")
        return self._report

    async def write_properties(self, properties: Properties) -> None:
        if self._fail:
            raise ConnectionError("device unreachable")
        self.written = properties


def make_client(
    sn: str,
    report: Properties | None = None,
    fail: bool = False,
    pack_data: list[dict[str, Any]] | None = None,
) -> FakeDeviceClient:
    return FakeDeviceClient(sn, report=report, fail=fail, pack_data=pack_data)


@pytest.mark.asyncio
async def test_get_report_skips_unreachable_devices() -> None:
    ok = make_client("OK1", report={"outputHomePower": 100})
    broken = make_client("BROKEN1", fail=True)
    aggregator = Aggregator([ok, broken])

    reports = await aggregator.get_report()

    assert reports == {"OK1": {"outputHomePower": 100}}


@pytest.mark.asyncio
async def test_get_aggregated_report_sums_power_and_weight_averages_soc() -> None:
    pack_a = {"sn": "PACKA", "packType": 70}
    pack_b = {"sn": "PACKB", "packType": 70}
    a = make_client(
        "A", report={"outputHomePower": 100, "electricLevel": 80}, pack_data=[pack_a]
    )
    b = make_client(
        "B", report={"outputHomePower": 50, "electricLevel": 20}, pack_data=[pack_b]
    )
    aggregator = Aggregator([a, b])

    properties, pack_data = await aggregator.get_aggregated_report()

    assert properties == {"outputHomePower": 150, "electricLevel": 50.0}
    assert pack_data == [pack_a, pack_b]


@pytest.mark.asyncio
async def test_get_aggregated_report_skips_unreachable_devices() -> None:
    ok = make_client("OK", report={"outputHomePower": 100}, pack_data=PACK_1920WH)
    broken = make_client("BROKEN", fail=True)
    aggregator = Aggregator([ok, broken])

    properties, pack_data = await aggregator.get_aggregated_report()

    assert properties == {"outputHomePower": 100}
    assert pack_data == PACK_1920WH


@pytest.mark.asyncio
async def test_write_properties_splits_output_limit_by_soc_and_capacity() -> None:
    a = make_client("A", report={"electricLevel": 80}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 20}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 100})

    assert a.written == {"outputLimit": 80.0}
    assert b.written == {"outputLimit": 20.0}


@pytest.mark.asyncio
async def test_write_properties_splits_evenly_when_state_is_equal() -> None:
    a = make_client("A", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    c = make_client("C", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b, c])

    await aggregator.write_properties({"outputLimit": 300})

    assert a.written == {"outputLimit": 100.0}
    assert b.written == {"outputLimit": 100.0}
    assert c.written == {"outputLimit": 100.0}


@pytest.mark.asyncio
async def test_write_properties_passes_through_non_split_properties() -> None:
    a = make_client("A", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 100, "acMode": 2})

    assert a.written == {"outputLimit": 50.0, "acMode": 2}
    assert b.written == {"outputLimit": 50.0, "acMode": 2}


@pytest.mark.asyncio
async def test_write_properties_excludes_device_at_or_below_min_soc() -> None:
    low = make_client("LOW", report={"electricLevel": 5, "minSoc": 100}, pack_data=PACK_1920WH)
    ok = make_client("OK", report={"electricLevel": 80}, pack_data=PACK_1920WH)
    aggregator = Aggregator([low, ok])

    await aggregator.write_properties({"outputLimit": 100})

    assert low.written == {"outputLimit": 0.0}
    assert ok.written == {"outputLimit": 100.0}


@pytest.mark.asyncio
async def test_write_properties_excludes_device_at_or_above_soc_set_when_charging() -> None:
    full = make_client("FULL", report={"electricLevel": 95, "socSet": 900}, pack_data=PACK_1920WH)
    low = make_client("LOW", report={"electricLevel": 20}, pack_data=PACK_1920WH)
    aggregator = Aggregator([full, low])

    await aggregator.write_properties({"inputLimit": 100})

    assert full.written == {"inputLimit": 0.0}
    assert low.written == {"inputLimit": 100.0}


@pytest.mark.asyncio
async def test_write_properties_reads_min_soc_and_soc_set_as_tenths_of_a_percent() -> None:
    # Real device values: electricLevel=65, minSoc=100 (10%), socSet=800 (80%).
    # A naive direct comparison against electricLevel would wrongly treat
    # minSoc/socSet as already being on a 0-100 percent scale.
    device = make_client(
        "REAL", report={"electricLevel": 65, "minSoc": 100, "socSet": 800}, pack_data=PACK_1920WH
    )
    aggregator = Aggregator([device])

    await aggregator.write_properties({"outputLimit": 100})
    assert device.written == {"outputLimit": 100.0}

    await aggregator.write_properties({"inputLimit": 100})
    assert device.written == {"inputLimit": 100.0}


@pytest.mark.asyncio
async def test_write_properties_caps_output_at_inverse_max_power_and_redistributes() -> None:
    capped = make_client(
        "CAPPED", report={"electricLevel": 50, "inverseMaxPower": 30}, pack_data=PACK_1920WH
    )
    open_ended = make_client("OPEN", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([capped, open_ended])

    await aggregator.write_properties({"outputLimit": 100})

    assert capped.written == {"outputLimit": 30.0}
    assert open_ended.written == {"outputLimit": 70.0}


@pytest.mark.asyncio
async def test_write_properties_caps_input_at_charge_max_limit_and_redistributes() -> None:
    capped = make_client(
        "CAPPED", report={"electricLevel": 20, "chargeMaxLimit": 40}, pack_data=PACK_1920WH
    )
    open_ended = make_client("OPEN", report={"electricLevel": 20}, pack_data=PACK_1920WH)
    aggregator = Aggregator([capped, open_ended])

    await aggregator.write_properties({"inputLimit": 100})

    assert capped.written == {"inputLimit": 40.0}
    assert open_ended.written == {"inputLimit": 60.0}


@pytest.mark.asyncio
async def test_write_properties_leaves_shortfall_when_combined_caps_are_insufficient() -> None:
    a = make_client("A", report={"electricLevel": 50, "inverseMaxPower": 30}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 50, "inverseMaxPower": 30}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 100})

    assert a.written == {"outputLimit": 30.0}
    assert b.written == {"outputLimit": 30.0}


@pytest.mark.asyncio
async def test_write_properties_falls_back_to_even_split_when_state_unknown() -> None:
    a = make_client("A", report={})
    b = make_client("B", report={})
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 100})

    assert a.written == {"outputLimit": 50.0}
    assert b.written == {"outputLimit": 50.0}


@pytest.mark.asyncio
async def test_write_properties_skips_unreachable_devices() -> None:
    ok = make_client("OK1", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    broken = make_client("BROKEN1", fail=True)
    aggregator = Aggregator([ok, broken])

    await aggregator.write_properties({"outputLimit": 100})

    assert ok.written == {"outputLimit": 100.0}
