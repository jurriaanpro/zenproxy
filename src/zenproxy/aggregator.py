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

# Real Zendure devices shouldn't be asked to charge/discharge at a very low
# rate just because a request happens to get split evenly: e.g. a 300W
# request across two devices is better handled by one device alone than as
# two 150W trickles. Zendure doesn't expose a real per-device minimum, so
# this is a conservative, hardcoded value applied uniformly to all devices:
# a request is spread across as many devices as keep each device's even
# share at or above this value.
PER_DEVICE_MIN_WATTS = 200.0

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


def _priority_split(
    total: float,
    priority: list[DeviceClient],
    caps: dict[DeviceClient, float],
    per_device_min: float = PER_DEVICE_MIN_WATTS,
) -> dict[DeviceClient, float]:
    """Evenly split `total` across as many devices as keep each one's even share
    at or above `per_device_min`, water-filled against each device's own cap.

    Devices are brought in, in the given `priority` order, starting from the
    largest group size whose even share clears the threshold. If that group's
    combined cap can't actually cover `total`, the next-priority device is
    added regardless of the threshold -- a genuine capacity shortfall always
    takes precedence over avoiding a thin split.
    """
    n = len(priority)
    if n == 0 or total <= 0:
        return {}

    k = min(n, max(1, int((total + 1e-9) // per_device_min)))
    shares: dict[DeviceClient, float] = {}
    while k <= n:
        active = priority[:k]
        even_weights = dict.fromkeys(active, 1.0)
        active_caps = {client: caps.get(client, float("inf")) for client in active}
        shares = _distribute_with_caps(total, even_weights, active_caps)
        if sum(shares.values()) >= total - 1e-6:
            break
        k += 1
    return shares


class Aggregator:
    def __init__(self, clients: list[DeviceClient]) -> None:
        self.clients = clients
        # Which device leads a charge/discharge split, kept sticky across
        # calls (keyed by charging direction) so that whichever device is
        # currently active keeps draining/charging until it's actually
        # excluded (hits its floor/ceiling), rather than handing off to
        # whichever device has marginally more headroom on every single
        # write -- that flapped devices on and off in lockstep and kept
        # both packs converging on the same SoC instead of one depleting
        # before the next kicks in.
        self._priority_order: dict[bool, list[DeviceClient]] = {}

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

    def _stable_priority(
        self, charging: bool, weights: dict[DeviceClient, float]
    ) -> list[DeviceClient]:
        """Order eligible devices for a split, keeping the previous leader(s) in
        place as long as they're still eligible.

        Devices that dropped out of `weights` (hit their floor/ceiling) are
        dropped from the order too; newly-eligible devices are appended,
        ranked by current SoC-weighted headroom. This is what makes the split
        "drain one device, then the next" instead of re-ranking by the
        tiniest SoC difference on every call.
        """
        previous = self._priority_order.get(charging, [])
        kept = [client for client in previous if client in weights]
        newcomers = sorted(
            (client for client in weights if client not in kept),
            key=lambda c: weights[c],
            reverse=True,
        )
        order = kept + newcomers
        self._priority_order[charging] = order
        return order

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

        priority = self._stable_priority(charging, weights)
        shares = _priority_split(total, priority, caps)
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
        flow (outputLimit/inputLimit): a sticky priority order (see `_stable_priority`),
        excluding devices at their floor/ceiling, evenly split once the group's per-device
        share clears `PER_DEVICE_MIN_WATTS` (see `_priority_split`) rather than spread thin
        across all of them. Unlike the flow split, there's no further per-device cap to fill
        against here -- these fields *are* the ceiling -- so within the active group a device
        either gets its even share of the total or nothing. Note a device zeroed out this way
        isn't locked out permanently: it's only until it's excluded by `_soc_weighted_headroom`
        or the sticky order hands off to it.
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

        caps = dict.fromkeys(weights, float("inf"))
        priority = self._stable_priority(charging, weights)
        shares = _priority_split(total, priority, caps)
        logger.info(
            "{} split of {}: {}",
            property_name,
            total,
            {client.label: share for client, share in shares.items()},
        )
        for client in self.clients:
            per_device[client][property_name] = shares.get(client, 0.0)
