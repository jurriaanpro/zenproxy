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

At container runtime, `run.sh` renders `/data/options.json` (the addon's
config, in HA's format) into zenproxy's own `config.yaml` shape, then execs
`/app/.venv/bin/zenproxy` directly rather than `uv run zenproxy`. `uv run`
re-syncs the environment — including dev dependencies like `mypy`/`ruff` —
on every invocation, which is wasted work on every container start; calling
the venv binary directly skips that since `uv sync --frozen --no-dev` already
ran once at build time.

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
- **Write responses mimic the real device's ack shape**, not an echo of the
  submitted properties: `{timestamp, messageId, success, code, sn}`. An
  earlier version echoed back `{"sn": ..., "properties": ...}`, which looked
  reasonable but didn't match what real Zendure hardware returns, and threw
  off automations expecting the real shape.
- **Real device writes need ~1-3s before a read-back reflects them.**
  Not something the proxy compensates for — automations polling at typical
  intervals (~5s) never notice — but worth knowing if you're scripting
  writes followed immediately by a read during testing.
