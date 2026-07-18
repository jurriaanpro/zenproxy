import asyncio
import logging

from zenproxy.device_client import DeviceClient, Properties

logger = logging.getLogger(__name__)

OUTPUT_SPLIT_PROPERTY = "outputLimit"
INPUT_SPLIT_PROPERTY = "inputLimit"

DEFAULT_MIN_SOC = 0
DEFAULT_SOC_SET = 100

# minSoc/socSet are reported in tenths of a percent (e.g. 800 == 80.0%),
# while electricLevel is a plain 0-100 percent. Confirmed against real
# hardware: a device reporting electricLevel=65 also reported minSoc=100
# (10%) and socSet=800 (80%).
SOC_SCALE = 10

# Wh per pack, keyed by the packData "packType" field. Zendure's API doesn't
# expose capacity directly; these come from the pack type IDs used by the
# community Zendure-HA-zenSDK integration.
PACK_CAPACITY_WH: dict[int, float] = {
    5: 2880.0,
    70: 1920.0,
    250: 960.0,
    300: 1920.0,
    350: 2880.0,
    500: 2400.0,
}


def _device_capacity_wh(client: DeviceClient) -> float:
    capacity = 0.0
    for pack in client.pack_data:
        pack_type = pack.get("packType")
        if isinstance(pack_type, int):
            capacity += PACK_CAPACITY_WH.get(pack_type, 0.0)
    return capacity


def _distribute_with_caps(
    total: float,
    weights: dict[DeviceClient, float],
    caps: dict[DeviceClient, float],
) -> dict[DeviceClient, float]:
    """Proportional split of `total` by `weights`, without exceeding each client's cap.

    Any amount a capped client can't absorb is redistributed, proportionally,
    among the remaining uncapped clients (water-filling).
    """
    free = dict(weights)
    shares: dict[DeviceClient, float] = {}
    remaining_total = total

    while free:
        weight_sum = sum(free.values())
        if weight_sum <= 0:
            break

        overflowing = {
            client: caps[client]
            for client, weight in free.items()
            if remaining_total * (weight / weight_sum) > caps[client]
        }
        if not overflowing:
            for client, weight in free.items():
                shares[client] = remaining_total * (weight / weight_sum)
            break

        for client, cap in overflowing.items():
            shares[client] = cap
            remaining_total -= cap
            del free[client]

    return shares


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
                logger.warning("device %s unreachable: %s", client.label, result)
                continue
            reports[client.label] = result
        return reports

    async def write_properties(self, properties: Properties) -> None:
        per_device: dict[DeviceClient, Properties] = {
            client: dict(properties) for client in self.clients
        }

        needs_state = OUTPUT_SPLIT_PROPERTY in properties or INPUT_SPLIT_PROPERTY in properties
        if needs_state and self.clients:
            states = await self._gather_states()
            if OUTPUT_SPLIT_PROPERTY in properties:
                total = properties[OUTPUT_SPLIT_PROPERTY]
                assert isinstance(total, int | float)
                self._apply_split(total, states, per_device, OUTPUT_SPLIT_PROPERTY, charging=False)
            if INPUT_SPLIT_PROPERTY in properties:
                total = properties[INPUT_SPLIT_PROPERTY]
                assert isinstance(total, int | float)
                self._apply_split(total, states, per_device, INPUT_SPLIT_PROPERTY, charging=True)

        results = await asyncio.gather(
            *(client.write_properties(per_device[client]) for client in self.clients),
            return_exceptions=True,
        )
        for client, result in zip(self.clients, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("device %s write failed: %s", client.label, result)

    async def _gather_states(self) -> dict[DeviceClient, Properties]:
        results = await asyncio.gather(
            *(client.get_report() for client in self.clients),
            return_exceptions=True,
        )
        states: dict[DeviceClient, Properties] = {}
        for client, result in zip(self.clients, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("device %s unreachable: %s", client.label, result)
                continue
            states[client] = result
        return states

    def _apply_split(
        self,
        total: float,
        states: dict[DeviceClient, Properties],
        per_device: dict[DeviceClient, Properties],
        property_name: str,
        *,
        charging: bool,
    ) -> None:
        cap_property = "chargeMaxLimit" if charging else "inverseMaxPower"
        weights: dict[DeviceClient, float] = {}
        caps: dict[DeviceClient, float] = {}
        for client in self.clients:
            state = states.get(client)
            if state is None:
                continue
            electric_level = state.get("electricLevel")
            if not isinstance(electric_level, int | float):
                continue

            if charging:
                raw_soc_set = state.get("socSet", DEFAULT_SOC_SET * SOC_SCALE)
                if not isinstance(raw_soc_set, int | float):
                    continue
                if electric_level >= raw_soc_set / SOC_SCALE:
                    continue
                headroom = (100 - electric_level) / 100
            else:
                raw_min_soc = state.get("minSoc", DEFAULT_MIN_SOC * SOC_SCALE)
                if not isinstance(raw_min_soc, int | float):
                    continue
                if electric_level <= raw_min_soc / SOC_SCALE:
                    continue
                headroom = electric_level / 100

            weight = headroom * _device_capacity_wh(client)
            if weight > 0:
                weights[client] = weight
                cap = state.get(cap_property)
                caps[client] = cap if isinstance(cap, int | float) and cap >= 0 else float("inf")

        total_weight = sum(weights.values())
        if total_weight <= 0:
            share = total / len(self.clients)
            for client in self.clients:
                per_device[client][property_name] = share
            return

        shares = _distribute_with_caps(total, weights, caps)
        distributed = sum(shares.values())
        if distributed < total - 1e-6:
            logger.warning(
                "%s request of %s exceeds combined device capacity; only %s distributed",
                property_name,
                total,
                distributed,
            )
        for client in self.clients:
            per_device[client][property_name] = shares.get(client, 0.0)
