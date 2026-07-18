import asyncio
from typing import Any

from loguru import logger

from zenproxy.device_client import DeviceClient, Properties

# Properties that represent a power flow, a combined limit, or a count:
# summing across devices gives the virtual device's total, matching what a
# real device would report for its own combined pack output.
SUM_PROPERTIES = frozenset(
    {
        "outputHomePower",
        "outputPackPower",
        "packInputPower",
        "gridInputPower",
        "solarInputPower",
        "solarPower1",
        "solarPower2",
        "outputLimit",
        "inputLimit",
        "inverseMaxPower",
        "chargeMaxLimit",
        "packNum",
    }
)

# Properties that represent a state of charge: averaged, weighted by each
# device's capacity, so a small nearly-full device doesn't skew the result
# as much as a large one.
CAPACITY_WEIGHTED_AVERAGE_PROPERTIES = frozenset({"electricLevel"})

# Properties averaged plainly across reachable devices.
AVERAGE_PROPERTIES = frozenset({"BatVolt", "remainOutTime"})

# Properties where the worst case (highest value) across devices should win.
MAX_PROPERTIES = frozenset({"hyperTmp", "is_error", "minSoc"})

# Properties where the most conservative (lowest value) across devices should win.
MIN_PROPERTIES = frozenset({"rssi", "socSet", "socStatus", "gridState"})

_HANDLED_PROPERTIES = (
    SUM_PROPERTIES
    | CAPACITY_WEIGHTED_AVERAGE_PROPERTIES
    | AVERAGE_PROPERTIES
    | MAX_PROPERTIES
    | MIN_PROPERTIES
    | {"socLimit"}
)

OUTPUT_SPLIT_PROPERTY = "outputLimit"
INPUT_SPLIT_PROPERTY = "inputLimit"

# These are hardware ceilings rather than live power flow, but still split
# using the same SoC-weighted headroom as outputLimit/inputLimit: a device
# already at its socSet/minSoc floor contributes no weight, so its sibling
# can claim the full ceiling instead of a static, unusable half. Splitting
# them at all (rather than broadcasting the total to every device) keeps the
# aggregated read-back equal to what was written -- confirmed against real
# hardware: broadcasting caused the read-back sum to double the requested
# total, which kept a control-loop automation retrying indefinitely. And
# splitting evenly by capacity alone (ignoring SoC) reintroduced a milder
# version of the same problem: an excluded device still hogged half the
# ceiling that only its sibling could actually use.
CHARGE_CAP_PROPERTY = "chargeMaxLimit"
DISCHARGE_CAP_PROPERTY = "inverseMaxPower"

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


def _numeric_values(states: dict[DeviceClient, Properties], name: str) -> list[float]:
    return [value for state in states.values() if isinstance(value := state.get(name), int | float)]


def _aggregate_properties(
    clients: list[DeviceClient], states: dict[DeviceClient, Properties]
) -> Properties:
    aggregated: Properties = {}

    for name in SUM_PROPERTIES:
        values = _numeric_values(states, name)
        if values:
            aggregated[name] = sum(values)

    for name in AVERAGE_PROPERTIES:
        values = _numeric_values(states, name)
        if values:
            aggregated[name] = sum(values) / len(values)

    for name in MAX_PROPERTIES:
        values = _numeric_values(states, name)
        if values:
            aggregated[name] = max(values)

    for name in MIN_PROPERTIES:
        values = _numeric_values(states, name)
        if values:
            aggregated[name] = min(values)

    for name in CAPACITY_WEIGHTED_AVERAGE_PROPERTIES:
        weighted_sum = 0.0
        weight_total = 0.0
        for client, state in states.items():
            value = state.get(name)
            if not isinstance(value, int | float):
                continue
            weight = _device_capacity_wh(client) or 1.0
            weighted_sum += value * weight
            weight_total += weight
        if weight_total > 0:
            aggregated[name] = weighted_sum / weight_total

    soc_limits = _numeric_values(states, "socLimit")
    if soc_limits:
        if all(v == 1 for v in soc_limits):
            aggregated["socLimit"] = 1
        elif all(v == 2 for v in soc_limits):
            aggregated["socLimit"] = 2
        else:
            aggregated["socLimit"] = 0

    # Anything not explicitly aggregated above (config/status flags like
    # acMode, gridStandard, faultLevel, ...) is passed through unchanged
    # from the first reachable device, so nothing is silently dropped.
    for client in clients:
        fallback_state = states.get(client)
        if fallback_state is None:
            continue
        for name, value in fallback_state.items():
            if name not in _HANDLED_PROPERTIES and name not in aggregated:
                aggregated[name] = value
        break

    return aggregated


def _merged_pack_data(states: dict[DeviceClient, Properties]) -> list[dict[str, Any]]:
    pack_data: list[dict[str, Any]] = []
    for client in states:
        pack_data.extend(client.pack_data)
    return pack_data


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
                logger.warning("device {} unreachable: {}", client.label, result)
                continue
            reports[client.label] = result
        return reports

    async def get_aggregated_report(self) -> tuple[Properties, list[dict[str, Any]]]:
        states = await self._gather_states()
        return _aggregate_properties(self.clients, states), _merged_pack_data(states)

    async def write_properties(self, properties: Properties) -> bool:
        logger.info("write requested: {}", properties)

        per_device: dict[DeviceClient, Properties] = {
            client: dict(properties) for client in self.clients
        }

        needs_state = (
            OUTPUT_SPLIT_PROPERTY in properties
            or INPUT_SPLIT_PROPERTY in properties
            or CHARGE_CAP_PROPERTY in properties
            or DISCHARGE_CAP_PROPERTY in properties
        )
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
            if CHARGE_CAP_PROPERTY in properties:
                total = properties[CHARGE_CAP_PROPERTY]
                assert isinstance(total, int | float)
                self._apply_cap_split(total, states, per_device, CHARGE_CAP_PROPERTY, charging=True)
            if DISCHARGE_CAP_PROPERTY in properties:
                total = properties[DISCHARGE_CAP_PROPERTY]
                assert isinstance(total, int | float)
                self._apply_cap_split(
                    total, states, per_device, DISCHARGE_CAP_PROPERTY, charging=False
                )

        results = await asyncio.gather(
            *(client.write_properties(per_device[client]) for client in self.clients),
            return_exceptions=True,
        )
        all_ok = True
        for client, result in zip(self.clients, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("device {} write failed: {}", client.label, result)
                all_ok = False
            elif not result.get("success", True):
                logger.warning("device {} reported write failure: {}", client.label, result)
                all_ok = False
            else:
                logger.info("device {} write ok: {}", client.label, per_device[client])
        return all_ok

    async def _gather_states(self) -> dict[DeviceClient, Properties]:
        results = await asyncio.gather(
            *(client.get_report() for client in self.clients),
            return_exceptions=True,
        )
        states: dict[DeviceClient, Properties] = {}
        for client, result in zip(self.clients, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning("device {} unreachable: {}", client.label, result)
                continue
            states[client] = result
        return states

    def _soc_weighted_headroom(
        self,
        states: dict[DeviceClient, Properties],
        property_name: str,
        *,
        charging: bool,
    ) -> dict[DeviceClient, float]:
        """Capacity-weighted headroom per device, excluding those at their SoC floor/ceiling."""
        weights: dict[DeviceClient, float] = {}
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
                    logger.info(
                        "device {} excluded from {}: electricLevel {} >= socSet {}",
                        client.label,
                        property_name,
                        electric_level,
                        raw_soc_set / SOC_SCALE,
                    )
                    continue
                headroom = (100 - electric_level) / 100
            else:
                raw_min_soc = state.get("minSoc", DEFAULT_MIN_SOC * SOC_SCALE)
                if not isinstance(raw_min_soc, int | float):
                    continue
                if electric_level <= raw_min_soc / SOC_SCALE:
                    logger.info(
                        "device {} excluded from {}: electricLevel {} <= minSoc {}",
                        client.label,
                        property_name,
                        electric_level,
                        raw_min_soc / SOC_SCALE,
                    )
                    continue
                headroom = electric_level / 100

            weight = headroom * _device_capacity_wh(client)
            if weight > 0:
                weights[client] = weight
        return weights

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
        weights = self._soc_weighted_headroom(states, property_name, charging=charging)
        caps: dict[DeviceClient, float] = {}
        for client in weights:
            state = states[client]
            cap = state.get(cap_property)
            caps[client] = cap if isinstance(cap, int | float) and cap >= 0 else float("inf")

        total_weight = sum(weights.values())
        if total_weight <= 0:
            share = total / len(self.clients)
            logger.info(
                "{} split: no device state/headroom available, falling back to even split of {}",
                property_name,
                total,
            )
            for client in self.clients:
                per_device[client][property_name] = share
            return

        shares = _distribute_with_caps(total, weights, caps)
        distributed = sum(shares.values())
        if distributed < total - 1e-6:
            logger.warning(
                "{} request of {} exceeds combined device capacity; only {} distributed",
                property_name,
                total,
                distributed,
            )
        logger.info(
            "{} split of {}: {}",
            property_name,
            total,
            {client.label: shares.get(client, 0.0) for client in self.clients},
        )
        for client in self.clients:
            per_device[client][property_name] = shares.get(client, 0.0)

    def _apply_cap_split(
        self,
        total: float,
        states: dict[DeviceClient, Properties],
        per_device: dict[DeviceClient, Properties],
        property_name: str,
        *,
        charging: bool,
    ) -> None:
        """Split a chargeMaxLimit/inverseMaxPower write the same way as the matching power
        flow (outputLimit/inputLimit): weighted by SoC headroom and capacity, excluding
        devices at their floor/ceiling. Unlike the flow split, there's no further cap to
        water-fill against here -- these fields *are* the ceiling.
        """
        weights = self._soc_weighted_headroom(states, property_name, charging=charging)
        total_weight = sum(weights.values())
        if total_weight <= 0:
            share = total / len(self.clients)
            logger.info(
                "{} split: no device state/headroom available, falling back to even split of {}",
                property_name,
                total,
            )
            for client in self.clients:
                per_device[client][property_name] = share
            return

        shares = {client: total * weight / total_weight for client, weight in weights.items()}
        logger.info(
            "{} split of {}: {}",
            property_name,
            total,
            {client.label: share for client, share in shares.items()},
        )
        for client in self.clients:
            per_device[client][property_name] = shares.get(client, 0.0)
