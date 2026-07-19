from typing import Any

import pytest

from zenproxy.aggregator import Aggregator
from zenproxy.config import RealDevice
from zenproxy.device_client import DeviceClient, Properties

PACK_1920WH = [{"packType": 70}]
PACK_960WH = [{"packType": 250}]


class FakeDeviceClient(DeviceClient):
    def __init__(
        self,
        sn: str,
        report: Properties | None = None,
        fail: bool = False,
        pack_data: list[dict[str, Any]] | None = None,
        write_result: dict[str, Any] | None = None,
    ) -> None:
        self.device = RealDevice(host="10.0.0.1")
        self.sn = sn
        self._report = report or {}
        self._fail = fail
        self.pack_data = pack_data or []
        self._write_result = write_result or {"success": True, "code": 200}
        self.written: Properties | None = None

    async def get_report(self) -> Properties:
        if self._fail:
            raise ConnectionError("device unreachable")
        return self._report

    async def write_properties(self, properties: Properties) -> dict[str, Any]:
        if self._fail:
            raise ConnectionError("device unreachable")
        self.written = properties
        return self._write_result


def make_client(
    sn: str,
    report: Properties | None = None,
    fail: bool = False,
    pack_data: list[dict[str, Any]] | None = None,
    write_result: dict[str, Any] | None = None,
) -> FakeDeviceClient:
    return FakeDeviceClient(
        sn, report=report, fail=fail, pack_data=pack_data, write_result=write_result
    )


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
async def test_get_aggregated_report_applies_min_max_and_average_rules() -> None:
    a = make_client(
        "A",
        report={"hyperTmp": 3000, "rssi": -70, "minSoc": 50, "socSet": 900, "BatVolt": 5000},
        pack_data=PACK_1920WH,
    )
    b = make_client(
        "B",
        report={"hyperTmp": 3200, "rssi": -60, "minSoc": 100, "socSet": 850, "BatVolt": 5100},
        pack_data=PACK_1920WH,
    )
    aggregator = Aggregator([a, b])

    properties, _ = await aggregator.get_aggregated_report()

    assert properties == {
        "hyperTmp": 3200,  # max: worst-case temperature
        "rssi": -70,  # min: weakest signal
        "minSoc": 100,  # max: strictest floor
        "socSet": 850,  # min: most conservative target
        "BatVolt": 5050.0,  # plain average
    }


@pytest.mark.asyncio
async def test_get_aggregated_report_socLimit_consensus() -> None:
    both_discharge_limited = Aggregator(
        [
            make_client("A", report={"socLimit": 2}, pack_data=PACK_1920WH),
            make_client("B", report={"socLimit": 2}, pack_data=PACK_1920WH),
        ]
    )
    mixed = Aggregator(
        [
            make_client("A", report={"socLimit": 2}, pack_data=PACK_1920WH),
            make_client("B", report={"socLimit": 0}, pack_data=PACK_1920WH),
        ]
    )

    both_properties, _ = await both_discharge_limited.get_aggregated_report()
    mixed_properties, _ = await mixed.get_aggregated_report()

    assert both_properties["socLimit"] == 2
    assert mixed_properties["socLimit"] == 0


@pytest.mark.asyncio
async def test_get_aggregated_report_falls_back_to_first_device_for_unhandled_fields() -> None:
    a = make_client("A", report={"acMode": 2, "faultLevel": 0}, pack_data=PACK_1920WH)
    b = make_client("B", report={"acMode": 1, "faultLevel": 1}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    properties, _ = await aggregator.get_aggregated_report()

    assert properties == {"acMode": 2, "faultLevel": 0}


@pytest.mark.asyncio
async def test_write_properties_concentrates_output_limit_on_highest_priority_device() -> None:
    # A single device can absorb the whole 100W request on its own, so it
    # takes all of it rather than splitting a trickle across both devices.
    a = make_client("A", report={"electricLevel": 80}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 20}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 100})

    assert a.written == {"outputLimit": 100.0}
    assert b.written == {"outputLimit": 0.0}


@pytest.mark.asyncio
async def test_write_properties_concentrates_small_request_on_a_single_device() -> None:
    # The canonical case this behavior targets: a 200W request shouldn't
    # become two 100W trickles when one device could handle it alone.
    a = make_client("A", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 200})

    assert a.written == {"outputLimit": 200.0}
    assert b.written == {"outputLimit": 0.0}


@pytest.mark.asyncio
async def test_write_properties_keeps_the_same_device_active_despite_small_soc_swings() -> None:
    # A stays the leader (higher electricLevel) on the first call. Even once
    # a bit of discharge nudges it just below B, it should keep discharging
    # -- re-ranking by the tiniest SoC difference on every call is what
    # caused both packs to drain in lockstep and the relay to flap.
    a = make_client("A", report={"electricLevel": 50, "minSoc": 100}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 49, "minSoc": 100}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 100})
    assert a.written == {"outputLimit": 100.0}
    assert b.written == {"outputLimit": 0.0}

    a._report["electricLevel"] = 48
    await aggregator.write_properties({"outputLimit": 100})
    assert a.written == {"outputLimit": 100.0}
    assert b.written == {"outputLimit": 0.0}


@pytest.mark.asyncio
async def test_write_properties_hands_off_once_the_active_device_hits_its_floor() -> None:
    # Once A actually reaches its minSoc floor and is excluded, B takes over
    # -- the hand-off happens on genuine exclusion, not a ranking swap.
    a = make_client("A", report={"electricLevel": 50, "minSoc": 100}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 49, "minSoc": 100}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 100})
    assert a.written == {"outputLimit": 100.0}

    a._report["electricLevel"] = 10
    await aggregator.write_properties({"outputLimit": 100})
    assert a.written == {"outputLimit": 0.0}
    assert b.written == {"outputLimit": 100.0}


@pytest.mark.asyncio
async def test_write_properties_evenly_divides_once_the_per_device_threshold_is_met() -> None:
    # 300W across 2 devices would be 150W each -- at, not below, the 200W
    # per-device minimum -- but A can only take 150W anyway (its own cap),
    # so both land on 150W regardless: A is capped, and B's even share
    # exactly matches A's cap here.
    a = make_client(
        "A", report={"electricLevel": 50, "inverseMaxPower": 150}, pack_data=PACK_1920WH
    )
    b = make_client("B", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 300})

    assert a.written == {"outputLimit": 150.0}
    assert b.written == {"outputLimit": 150.0}


@pytest.mark.asyncio
async def test_write_properties_splits_evenly_across_two_devices_at_400w() -> None:
    # The canonical "evenly divided" example: 400W across two 200W-threshold
    # devices means 200W each clears the per-device minimum, so both
    # participate rather than concentrating on one.
    a = make_client("A", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 400})

    assert a.written == {"outputLimit": 200.0}
    assert b.written == {"outputLimit": 200.0}


@pytest.mark.asyncio
async def test_write_properties_keeps_single_device_just_under_the_even_split_total() -> None:
    # One watt under the 400W even-split point (2 devices x 200W threshold):
    # still concentrated entirely on a single device.
    a = make_client("A", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 399})

    assert a.written == {"outputLimit": 399.0}
    assert b.written == {"outputLimit": 0.0}


@pytest.mark.asyncio
async def test_write_properties_uses_only_as_many_devices_as_the_threshold_allows() -> None:
    # 450W across 3 equally-weighted devices: splitting across all 3 would
    # give 150W each, under the 200W minimum, so only the top 2 by priority
    # participate (225W each) and the third is left untouched.
    a = make_client("A", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    c = make_client("C", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b, c])

    await aggregator.write_properties({"outputLimit": 450})

    assert a.written == {"outputLimit": 225.0}
    assert b.written == {"outputLimit": 225.0}
    assert c.written == {"outputLimit": 0.0}


@pytest.mark.asyncio
async def test_write_properties_passes_through_non_split_properties() -> None:
    a = make_client("A", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"outputLimit": 100, "acMode": 2})

    assert a.written == {"outputLimit": 100.0, "acMode": 2}
    assert b.written == {"outputLimit": 0.0, "acMode": 2}


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
async def test_write_properties_caps_a_device_and_gives_the_rest_to_its_sibling() -> None:
    # At k=1 CAPPED (tied for top priority) can only supply 30W of the 100W
    # request, so the group expands to both devices: CAPPED is water-filled
    # up to its 30W cap, and OPEN absorbs the remaining 70W.
    capped = make_client(
        "CAPPED", report={"electricLevel": 50, "inverseMaxPower": 30}, pack_data=PACK_1920WH
    )
    open_ended = make_client("OPEN", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([capped, open_ended])

    await aggregator.write_properties({"outputLimit": 100})

    assert capped.written == {"outputLimit": 30.0}
    assert open_ended.written == {"outputLimit": 70.0}


@pytest.mark.asyncio
async def test_write_properties_splits_evenly_water_filled_by_individual_caps() -> None:
    # 1040W across 3 devices clears the even-split threshold for all three
    # (1040/3 > 200), so all three participate from the start; C is
    # water-filled up to its 50W cap, and the remaining 990W splits evenly
    # between A and B (495W each).
    a = make_client(
        "A", report={"electricLevel": 50, "inverseMaxPower": 500}, pack_data=PACK_1920WH
    )
    b = make_client(
        "B", report={"electricLevel": 50, "inverseMaxPower": 500}, pack_data=PACK_1920WH
    )
    c = make_client(
        "C", report={"electricLevel": 50, "inverseMaxPower": 50}, pack_data=PACK_1920WH
    )
    aggregator = Aggregator([a, b, c])

    await aggregator.write_properties({"outputLimit": 1040})

    assert a.written == {"outputLimit": 495.0}
    assert b.written == {"outputLimit": 495.0}
    assert c.written == {"outputLimit": 50.0}


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


@pytest.mark.asyncio
async def test_write_properties_returns_true_when_all_devices_succeed() -> None:
    a = make_client("A", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    b = make_client("B", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    success = await aggregator.write_properties({"outputLimit": 100})

    assert success is True


@pytest.mark.asyncio
async def test_write_properties_returns_false_when_a_device_is_unreachable() -> None:
    ok = make_client("OK1", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    broken = make_client("BROKEN1", fail=True)
    aggregator = Aggregator([ok, broken])

    success = await aggregator.write_properties({"acMode": 2})

    assert success is False


@pytest.mark.asyncio
async def test_write_properties_returns_false_when_a_device_reports_failure() -> None:
    ok = make_client("OK1", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    rejected = make_client(
        "REJECTED1",
        report={"electricLevel": 50},
        pack_data=PACK_1920WH,
        write_result={"success": False, "code": 400},
    )
    aggregator = Aggregator([ok, rejected])

    success = await aggregator.write_properties({"acMode": 2})

    assert success is False


@pytest.mark.asyncio
async def test_write_properties_splits_charge_max_limit_evenly_by_equal_capacity() -> None:
    a = make_client("A", pack_data=PACK_1920WH)
    b = make_client("B", pack_data=PACK_1920WH)
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"chargeMaxLimit": 800})

    assert a.written == {"chargeMaxLimit": 400.0}
    assert b.written == {"chargeMaxLimit": 400.0}


@pytest.mark.asyncio
async def test_write_properties_splits_inverse_max_power_evenly_once_threshold_is_met() -> None:
    # 900W clears the 2-device even-split threshold (900/2 > 200), so both
    # participate -- LARGE's greater capacity-weighted headroom only
    # affects priority ordering (irrelevant here, since both are used), not
    # the amount each receives: this ceiling field has no per-device cap, so
    # the split is a plain 450/450 regardless of capacity.
    small = make_client("SMALL", report={"electricLevel": 50}, pack_data=PACK_960WH)
    large = make_client("LARGE", report={"electricLevel": 50}, pack_data=PACK_1920WH)
    aggregator = Aggregator([small, large])

    await aggregator.write_properties({"inverseMaxPower": 900})

    assert small.written == {"inverseMaxPower": 450.0}
    assert large.written == {"inverseMaxPower": 450.0}


@pytest.mark.asyncio
async def test_write_properties_cap_split_excludes_device_at_soc_limit() -> None:
    # Real-hardware scenario: a device that has already reached its socSet
    # target must not keep half the charge ceiling to itself -- its sibling,
    # which still has headroom, should be able to claim the full total.
    full = make_client("FULL", report={"electricLevel": 100}, pack_data=PACK_1920WH)
    empty = make_client("EMPTY", report={"electricLevel": 0}, pack_data=PACK_1920WH)
    aggregator = Aggregator([full, empty])

    await aggregator.write_properties({"chargeMaxLimit": 800})

    assert full.written == {"chargeMaxLimit": 0.0}
    assert empty.written == {"chargeMaxLimit": 800.0}


@pytest.mark.asyncio
async def test_write_properties_falls_back_to_even_split_for_cap_when_capacity_unknown() -> None:
    a = make_client("A")
    b = make_client("B")
    aggregator = Aggregator([a, b])

    await aggregator.write_properties({"inverseMaxPower": 800})

    assert a.written == {"inverseMaxPower": 400.0}
    assert b.written == {"inverseMaxPower": 400.0}
