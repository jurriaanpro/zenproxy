# zenproxy

Proxy that unifies one or more Zendure home battery devices behind a single
virtual device, using the same local HTTP API shape as a real Zendure device
(`GET /properties/report`, `POST /properties/write`).

## Usage

```bash
mise install
uv run zenproxy --config config.yaml
```

Copy [`config.example.yaml`](config.example.yaml) to `config.yaml` and fill in
the host of each real device. Serial numbers are learned automatically from
each device's `/properties/report` response — no need to specify them.

## Configuration

Every section below except `devices` is optional and falls back to the
defaults shown if omitted. Where an option also exists as a Home Assistant
addon option, its name is noted — the addon exposes options as flat keys
(e.g. `leader_rotation_soc_delta_percent`) since the Supervisor UI doesn't
support nested config.

### Core

| Option | Default | Purpose |
| --- | --- | --- |
| `virtual_sn` | — | Serial number the virtual (aggregated) device reports. |
| `virtual_product` | — | Product ID the virtual device reports, e.g. `solarFlow800Plus`. |
| `devices` | — | List of `{host, port}` for each real Zendure device to unify. `port` defaults to `80`. |

### `server`

| Option | Default | Purpose |
| --- | --- | --- |
| `host` | `0.0.0.0` | Interface the HTTP API binds to. |
| `port` | `8080` | Port the HTTP API listens on. Addon option: `port`. |
| `poll_interval_seconds` | `5.0` | How often the MQTT publisher (if enabled) polls devices for state. |

### `leader_rotation` — spread charge/discharge wear across devices

A request small enough to fit comfortably on one device is concentrated on a
single "leader" rather than split into inefficient trickles (see
[Design decisions](#design-decisions)). Left alone, that leader would
drain/charge all the way to its floor or ceiling before another device ever
gets a turn, cycling that pack harder than its siblings. `leader_rotation`
hands leadership to the next eligible device once the current leader has
moved far enough, even though it's still eligible.

| Option | Default | Purpose |
| --- | --- | --- |
| `enabled` | `true` | Turn rotation on/off. Addon option: `leader_rotation_enabled`. Disabling restores the old behavior: one device leads until it's actually excluded (hits its floor/ceiling). |
| `soc_delta_percent` | `10.0` | How many `electricLevel` percentage points the leader may drain/charge before handing off to the next device. Addon option: `leader_rotation_soc_delta_percent`. |

### `mqtt` — per-device visibility in Home Assistant (optional)

The HTTP API only exposes the virtual/aggregated device, by design — `mqtt`
is how you additionally get read-only visibility into each *real* device
(e.g. each battery's own charge level), published as Home Assistant MQTT
discovery sensors. Omit this section entirely to leave MQTT publishing off.

| Option | Default | Purpose |
| --- | --- | --- |
| `host` | — | Broker hostname/IP. Required to enable MQTT. |
| `port` | `1883` | Broker port. |
| `username` / `password` | `null` | Broker credentials, if required. |
| `discovery_prefix` | `homeassistant` | HA discovery topic prefix. |

In the Home Assistant addon, this is a single **Enable MQTT** toggle
(`mqtt_enabled`) instead — see [Home Assistant addon](#home-assistant-addon)
for how it fills in the rest automatically.

## Development

```bash
uv run ruff check .
uv run mypy .
uv run pytest
```

## Home Assistant addon

The addon lives in [`ha_addon/zenproxy`](ha_addon/zenproxy) and is built from a
separate Dockerfile, not the repo root. Home Assistant Supervisor builds each
addon using only that addon's own subfolder as the Docker build context, so
the `zenproxy` package (which lives at the repo root) can't be `COPY`ed in
directly. Instead the Dockerfile `git clone`s this repo during the build —
the same pattern used by official HA addons that wrap external source. This
means addon builds always pull from `main` (or `ZENPROXY_REF`), so pushes to
`main` are effectively releases as far as the addon is concerned.

Version bumps in [`config.yaml`](ha_addon/zenproxy/config.yaml) get a matching
entry in [`CHANGELOG.md`](ha_addon/zenproxy/CHANGELOG.md), which Supervisor
renders in the addon's info tab.

At container runtime, `run.sh` renders `/data/options.json` (the addon's
config, in HA's format) into zenproxy's own `config.yaml` shape, then execs
`/app/.venv/bin/zenproxy` directly rather than `uv run zenproxy`. `uv run`
re-syncs the environment — including dev dependencies like `mypy`/`ruff` —
on every invocation, which is wasted work on every container start; calling
the venv binary directly skips that since `uv sync --frozen --no-dev` already
ran once at build time.

Turning on the addon's **Enable MQTT** option (`mqtt_enabled`) doesn't require
filling in broker details by hand: the addon requests the `mqtt` Supervisor
service, and if a broker (e.g. the Mosquitto broker addon) is available, uses
its connection details automatically. It's off by default, even when a
broker is available, so installing or updating the addon never changes
existing behavior on its own. Only a curated set of properties is published
(see `MQTT_SENSORS` in [`src/zenproxy/mqtt.py`](src/zenproxy/mqtt.py)), not
every raw device field — this keeps each sensor mapped to the right Home
Assistant `device_class` and unit instead of showing up as an unlabeled
number.

## Design decisions

- **Split, don't broadcast, `chargeMaxLimit`/`inverseMaxPower`.** These are
  hardware ceilings rather than live power flow, but a naive implementation
  could just write the same total to every device (as a reference proxy
  implementation does) and rely on the aggregated read-back to sum them,
  with a warning on mismatch. That doesn't work for automations that treat
  the property as a virtual total: confirmed against real hardware,
  broadcasting caused the aggregated read-back to double the requested
  total, which kept a control-loop automation retrying indefinitely. So
  these fields are split across devices the same way as `outputLimit`/
  `inputLimit`, keeping read-back equal to what was written.
- **Split by SoC-weighted headroom, not raw capacity.** An earlier version
  split `chargeMaxLimit`/`inverseMaxPower` evenly by capacity alone,
  ignoring state of charge. That reintroduced a milder version of the same
  bug: a device already at its `socSet`/`minSoc` floor still hogged half the
  ceiling that only its sibling could actually use. The fix excludes
  devices at their floor/ceiling entirely and weights the rest by
  `headroom × capacity`, shared via one `_soc_weighted_headroom()` helper
  between the ceiling split and the power-flow split (the latter is
  additionally filled against each device's own cap — see the next point).
  See `src/zenproxy/aggregator.py`.
- **Concentrate small splits on one device; divide evenly once a request is
  large enough, not proportionally by SoC/capacity.** A naive proportional
  split turns any small request into inefficient trickles on every device —
  e.g. 200W across two devices becomes two 100W requests when one device
  alone could easily handle it. Instead, `total` is divided evenly across
  the *largest* group of devices whose even share still clears
  `PER_DEVICE_MIN_WATTS` (hardcoded at 200W): a 400W request across two
  devices splits 200/200, but 399W goes entirely to one device. Devices are
  brought into the group in SoC-weighted-headroom priority order and
  water-filled against each device's own cap; if the chosen group's combined
  cap can't actually cover `total`, the next-priority device is added
  regardless of the even-split threshold — a genuine capacity shortfall
  always takes precedence over avoiding a thin split. See
  `_priority_split()` in `src/zenproxy/aggregator.py`.
- **Priority order is sticky, not re-ranked on every write.** Early testing
  against real hardware showed that ranking devices by live SoC-weighted
  headroom on every single write caused the active device to flip as soon
  as its SoC dipped a hair below its sibling's — flapping the relay and
  pulling both packs' SoC together instead of draining one before the next.
  `Aggregator._stable_priority()` keeps the previous leader(s) in place as
  long as they're still eligible at all, only re-ranking when a device
  actually drops out (hits its floor/ceiling) or a new one becomes
  eligible. This state lives in memory on the `Aggregator` instance and
  resets on restart.
- **Leadership rotates once the leader has drained/charged past a SoC
  delta, even while it's still eligible.** The sticky priority above means
  a request small enough to concentrate on one device (see the previous
  point) would otherwise stay on that same device all the way to its
  floor/ceiling, wearing that pack's cycle count faster than its siblings.
  `Aggregator._rotate_leader_if_drained()` compares the leader's current
  `electricLevel` to the value it had when it took the lead and, once that
  gap is large enough, moves it to the back of the priority order so the
  next-ranked device takes over. Configurable — see
  [`leader_rotation`](#leader_rotation--spread-chargedischarge-wear-across-devices)
  above. See `src/zenproxy/aggregator.py`.
- **Write responses mimic the real device's ack shape**, not an echo of the
  submitted properties: `{timestamp, messageId, success, code, sn}`. An
  earlier version echoed back `{"sn": ..., "properties": ...}`, which looked
  reasonable but didn't match what real Zendure hardware returns, and threw
  off automations expecting the real shape.
- **Real device writes need ~1-3s before a read-back reflects them.**
  Not something the proxy compensates for — automations polling at typical
  intervals (~5s) never notice — but worth knowing if you're scripting
  writes followed immediately by a read during testing.
- **MQTT publishes per-device state, not the aggregated device.** The HTTP
  API already fully exposes the virtual/aggregated device; the only thing it
  doesn't expose is the individual real devices (that's the point of the
  proxy for automations). So MQTT is the other direction: per-device
  visibility only, read-only, for dashboards rather than automations.
  Configurable — see [`mqtt`](#mqtt--per-device-visibility-in-home-assistant-optional)
  above. See `src/zenproxy/mqtt.py`.
